import difflib
import os
import re
import time
from typing import Callable, Awaitable, Optional

import aiohttp
import aiosqlite
import discord
from discord import app_commands

from database import DB_PATH

# Regex patterns
EDITION_PATTERN = re.compile(
    r'[:\-–—\s]*\b(Game of the Year|GOTY|Ultimate|Deluxe|Collector\'s|Enhanced|Definitive|Gold|Complete|Special|Remastered|Edition|Director\'s Cut)\b.*$',
    re.IGNORECASE
)

# GENERIC DLC REGEX: Catches any title with a colon/dash followed by common DLC/expansion keywords
GENERIC_DLC_PATTERN = re.compile(
    r'[:\-–—]\s*.*?\b(DLC|Expansion|Pack|Add-on|Season Pass|Bonus Content|Soundtrack|Artbook|Episode|Chapter|Mission|Quest|Map Pack|Skin|Bundle)\b',
    re.IGNORECASE
)

NOISE_KEYWORDS = ["demake", "nokia", "flash", "collection", "bundle", "prototype", "edition"]


def normalize_game_name(name: str) -> str:
    """Strips edition suffixes, standardizes mobile quotes, and removes trailing punctuation."""
    clean_name = name.replace("’", "'").replace("‘", "'")
    clean_name = EDITION_PATTERN.sub('', clean_name)
    clean_name = re.sub(r'[:\-–—\s]+$', '', clean_name)
    return clean_name.strip()

def extract_id(val):
    """Utility to safely extract ID integer whether IGDB returns int or dict."""
    if isinstance(val, dict):
        return val.get("id")
    return val

async def fetch_game_details_from_igdb(game_name: str) -> dict | None:
    client_id = os.getenv("TWITCH_CLIENT_ID")
    access_token = os.getenv("TWITCH_ACCESS_TOKEN")

    if not client_id or not access_token:
        return None

    headers = {
        "Client-ID": client_id,
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json"
    }

    # Escape double quotes to prevent API crashes on games like Marvel's Spider-Man
    clean_search = normalize_game_name(game_name).replace('"', '\\"')

    # Use exact match (=) rather than wildcard (~ *)
    body = (
        f'fields name, summary, rating, cover.image_id, genres.name, platforms.name, first_release_date, url; '
        f'where name = "{clean_search}"; '
        f'limit 1;'
    )

    async with aiohttp.ClientSession() as session:
        async with session.post("https://api.igdb.com/v4/games", headers=headers, data=body) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            return data[0] if data else None

async def fetch_and_cache_game_from_igdb(game_name: str) -> tuple[int, str] | None:
    client_id = os.getenv("TWITCH_CLIENT_ID")
    access_token = os.getenv("TWITCH_ACCESS_TOKEN")

    if not client_id or not access_token:
        return None

    headers = {
        "Client-ID": client_id,
        "Authorization": f"Bearer {access_token}"
    }

    clean_search = normalize_game_name(game_name)
    body = f'fields id, name, category, version_parent, parent_game; search "{clean_search}"; limit 10;'

    async with aiohttp.ClientSession() as session:
        async with session.post("https://api.igdb.com/v4/games", headers=headers, data=body) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()

    if not data:
        return None

    selected = None

    # Priority 1: Exact case-insensitive title match
    for item in data:
        if item.get("name", "").lower() == game_name.lower():
            selected = item
            break

    # Priority 2: Main Game (category 0)
    if not selected:
        for item in data:
            if item.get("category", 0) == 0:
                selected = item
                break

    # Fallback: First returned IGDB result
    if not selected:
        selected = data[0]

    game_id = selected["id"]
    official_name = selected["name"]
    category = selected.get("category", 0)
    vp = selected.get("version_parent")
    p_game = selected.get("parent_game")

    version_parent_id = vp.get("id") if isinstance(vp, dict) else vp
    parent_game_id = p_game.get("id") if isinstance(p_game, dict) else p_game

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT OR IGNORE INTO igdb_games
                   (id, name, updated_at, category, parent_game_id, version_parent_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (game_id, official_name, int(time.time()), category, parent_game_id, version_parent_id)
        )
        await db.commit()

    return game_id, official_name

async def resolve_to_base_game(game_id: int, game_name: str):
    """
    Only resolve to parent game if the item is explicitly a DLC, expansion,
    or standalone expansion (category in {1, 2}). Standalone/jam entries,
    remasters, or version parents are preserved as-is.
    """
    client_id = os.getenv("TWITCH_CLIENT_ID")
    access_token = os.getenv("TWITCH_ACCESS_TOKEN")

    if not client_id or not access_token:
        return game_id, game_name

    headers = {
        "Client-ID": client_id,
        "Authorization": f"Bearer {access_token}"
    }

    body = f'fields name, category, parent_game, version_parent; where id = {game_id};'

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post("https://api.igdb.com/v4/games", headers=headers, data=body) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data:
                        item = data[0]
                        category = item.get("category", 0)

                        # Only follow parent_game if it is explicitly an Expansion (1) or DLC (2)
                        if category in {1, 2}:
                            parent = item.get("parent_game")
                            if parent:
                                p_id = parent.get("id") if isinstance(parent, dict) else parent
                                # Recursively fetch parent
                                p_body = f'fields id, name; where id = {p_id};'
                                async with session.post("https://api.igdb.com/v4/games", headers=headers, data=p_body) as p_resp:
                                    if p_resp.status == 200:
                                        p_data = await p_resp.json()
                                        if p_data:
                                            return p_data[0]["id"], p_data[0]["name"]

                        # Otherwise keep the exact game name/ID selected
                        return game_id, item.get("name", game_name)
    except Exception as e:
        print(f"Error in resolve_to_base_game: {e}")

    return game_id, game_name

async def autocomplete_games(interaction: discord.Interaction, current: str):
    try:
        clean_current = current.strip().replace("’", "'").replace("‘", "'").replace('"', '')

        # Safely resolve target user if passed via command interaction namespace
        user_id = interaction.user.id
        if hasattr(interaction, "namespace") and getattr(interaction.namespace, "user", None):
            target_user = interaction.namespace.user
            if hasattr(target_user, "id"):
                user_id = target_user.id

        # Only boost subscribed games to the top if the command is /ping_game
        boost_subscribed = bool(
            interaction.command and interaction.command.name == "ping_game"
        )

        # Retrieve user custom subscribed indicator preference
        sub_icon = "⭐ "
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT sub_indicator FROM user_settings WHERE user_id = ?", (user_id,)) as cursor:
                row = await cursor.fetchone()
                if row and row[0]:
                    sub_icon = f"{row[0]} "

        # 1. Default empty input handling
        if not clean_current:
            async with aiosqlite.connect(DB_PATH) as db:
                server_member_ids = [m.id for m in interaction.guild.members] if interaction.guild else []

                if server_member_ids:
                    placeholders = ",".join("?" for _ in server_member_ids)
                    if boost_subscribed:
                        query_default = f"""
                            SELECT g.name, g.is_multiplayer, 
                                   CASE WHEN s_user.user_id IS NOT NULL THEN 1 ELSE 0 END as is_sub,
                                   COUNT(s_all.user_id) as server_sub_count
                            FROM igdb_games g
                            LEFT JOIN subscriptions s_user ON g.id = s_user.game_id AND s_user.user_id = ?
                            LEFT JOIN subscriptions s_all ON g.id = s_all.game_id AND s_all.user_id IN ({placeholders})
                            WHERE (s_user.user_id IS NOT NULL OR (
                                g.is_multiplayer = 1
                                AND (g.parent_game_id IS NULL OR g.parent_game_id = 0)
                                AND (g.version_parent_id IS NULL OR g.version_parent_id = 0)
                            ))
                            GROUP BY g.id
                            ORDER BY is_sub DESC, server_sub_count DESC, g.total_rating_count DESC, g.id
                            LIMIT 25
                        """
                    else:
                        query_default = f"""
                            SELECT g.name, g.is_multiplayer, 
                                   CASE WHEN s_user.user_id IS NOT NULL THEN 1 ELSE 0 END as is_sub,
                                   COUNT(s_all.user_id) as server_sub_count
                            FROM igdb_games g
                            LEFT JOIN subscriptions s_user ON g.id = s_user.game_id AND s_user.user_id = ?
                            LEFT JOIN subscriptions s_all ON g.id = s_all.game_id AND s_all.user_id IN ({placeholders})
                            WHERE g.is_multiplayer = 1
                              AND (g.parent_game_id IS NULL OR g.parent_game_id = 0)
                              AND (g.version_parent_id IS NULL OR g.version_parent_id = 0)
                            GROUP BY g.id
                            ORDER BY server_sub_count DESC, g.total_rating_count DESC, g.id
                            LIMIT 25
                        """
                    params = (user_id, *server_member_ids)
                else:
                    if boost_subscribed:
                        query_default = """
                                        SELECT g.name, g.is_multiplayer,
                                               CASE WHEN s_user.user_id IS NOT NULL THEN 1 ELSE 0 END as is_sub,
                                               COUNT(s_all.user_id) as server_sub_count
                                        FROM igdb_games g
                                                 LEFT JOIN subscriptions s_user ON g.id = s_user.game_id AND s_user.user_id = ?
                                                 LEFT JOIN subscriptions s_all ON g.id = s_all.game_id
                                        WHERE (s_user.user_id IS NOT NULL OR (
                                            g.is_multiplayer = 1
                                                AND (g.parent_game_id IS NULL OR g.parent_game_id = 0)
                                                AND (g.version_parent_id IS NULL OR g.version_parent_id = 0)
                                            ))
                                        GROUP BY g.id
                                        ORDER BY is_sub DESC, server_sub_count DESC, g.total_rating_count DESC, g.id
                                        LIMIT 25
                                        """
                    else:
                        query_default = """
                                        SELECT g.name, g.is_multiplayer,
                                               CASE WHEN s_user.user_id IS NOT NULL THEN 1 ELSE 0 END as is_sub,
                                               COUNT(s_all.user_id) as server_sub_count
                                        FROM igdb_games g
                                                 LEFT JOIN subscriptions s_user ON g.id = s_user.game_id AND s_user.user_id = ?
                                                 LEFT JOIN subscriptions s_all ON g.id = s_all.game_id
                                        WHERE g.is_multiplayer = 1
                                          AND (g.parent_game_id IS NULL OR g.parent_game_id = 0)
                                          AND (g.version_parent_id IS NULL OR g.version_parent_id = 0)
                                        GROUP BY g.id
                                        ORDER BY server_sub_count DESC, g.total_rating_count DESC, g.id
                                        LIMIT 25
                                        """
                    params = (user_id,)

                async with db.execute(query_default, params) as cursor:
                    rows = await cursor.fetchall()
                    choices = []
                    for name, is_mp, is_sub, _sub_count in rows:
                        has_delimiter = any(c in name for c in [":", " - ", " – ", " — "])
                        if is_sub or (not GENERIC_DLC_PATTERN.search(name) and not has_delimiter):
                            sp_tag = " [SP 👤]" if is_mp == 0 else ""
                            sub_tag = sub_icon if is_sub else ""
                            choices.append(
                                app_commands.Choice(
                                    name=f"{sub_tag}{name}{sp_tag}"[:100],
                                    value=name[:100]
                                )
                            )
                    return choices

        # Detect DLC intent before stripping trailing punctuation
        user_searching_dlc = any(char in clean_current for char in [":", "-", "–", "—"]) or \
                             any(k in clean_current.lower() for k in ["dlc", "expansion", "pack", "add-on"])

        # Strip trailing punctuation/spaces
        search_term = re.sub(r'[:\-–—\s]+$', '', clean_current)
        if not search_term:
            search_term = clean_current

        # Sanitize for IGDB API
        igdb_search_query = re.sub(r'[:\-–—]', ' ', search_term).strip()
        igdb_search_query = re.sub(r'\s+', ' ', igdb_search_query)

        choice_candidates = []
        seen_names = set()
        user_subscribed_names = set()

        user_wants_noise = any(k in search_term.lower() for k in NOISE_KEYWORDS)

        def compute_match_score(title: str, query: str) -> tuple[int, float]:
            t_low = title.lower().strip()
            q_low = query.lower().strip()

            if t_low == q_low:
                return 0, 1.0  # Exact Match

            clean_root = re.sub(r'\s*\b(1|2|3|4|5|i|ii|iii|iv|v)\b$', '', t_low).strip()
            if clean_root == q_low:
                return 1, 0.95  # Sequel / Root Match

            if t_low.startswith(q_low):
                ratio = difflib.SequenceMatcher(None, q_low, t_low).ratio()
                return 2, ratio  # Prefix Match

            if q_low in t_low:
                ratio = difflib.SequenceMatcher(None, q_low, t_low).ratio()
                return 2, ratio  # Substring Match

            ratio = difflib.SequenceMatcher(None, q_low, t_low).ratio()
            return 3, ratio  # Fuzzy Match

        def get_sort_score(inner_match_tier: int, inner_is_mp: int, inner_is_sub: int) -> int:
            base_score = 7
            if inner_match_tier == 0 and inner_is_mp == 1: base_score = 0
            elif inner_match_tier == 1 and inner_is_mp == 1: base_score = 1
            elif inner_match_tier == 0 and inner_is_mp == 0: base_score = 2
            elif inner_match_tier == 1 and inner_is_mp == 0: base_score = 3
            elif inner_match_tier == 2 and inner_is_mp == 1: base_score = 4
            elif inner_match_tier == 2 and inner_is_mp == 0: base_score = 5
            elif inner_match_tier == 3 and inner_is_mp == 1: base_score = 6

            # Only shift score to top when boost_subscribed is True (/ping_game)
            return base_score - (10 if (boost_subscribed and inner_is_sub) else 0)

        def should_keep_candidate(inner_name: str, inner_rating_cnt: int, inner_match_tier: int, inner_similarity: float, inner_is_sub: bool = False) -> bool:
            # Subscribed games ALWAYS bypass DLC, delimiter, and noise filters
            if inner_is_sub:
                return True

            name_low = inner_name.lower()

            # 1. Filter noise keywords unless explicitly requested in query
            if not user_wants_noise and any(k in name_low for k in NOISE_KEYWORDS):
                return False

            # 2. Filter explicit DLC keywords when not searching for DLCs
            if not user_searching_dlc and GENERIC_DLC_PATTERN.search(inner_name):
                return False

            # 3. Suppress subtitle delimiters (colons/dashes) when user isn't searching for DLCs
            # (Allows exact matches, but blocks prefix matches like "Game Title: Subtitle Expansion")
            if not user_searching_dlc and any(c in inner_name for c in [":", " - ", " – ", " — "]):
                if inner_match_tier > 1:
                    return False

            # 4. Drop weak fuzzy matches
            if inner_match_tier == 3 and inner_similarity < 0.60:
                return False

            # Allow exact/prefix/sequel matches (tiers 0, 1, 2) even if unrated (0 rating count)
            if (not inner_rating_cnt or inner_rating_cnt == 0) and inner_match_tier == 3:
                return False

            return True

        # 2. LOCAL SEARCH & USER SUBSCRIPTION PREFETCH
        async with aiosqlite.connect(DB_PATH) as db:
            # Fetch user's subscribed game names so live IGDB queries preserve subscription status
            async with db.execute(
                """
                SELECT LOWER(g.name)
                FROM subscriptions s
                JOIN igdb_games g ON s.game_id = g.id
                WHERE s.user_id = ?
                """,
                (user_id,)
            ) as cursor:
                user_subscribed_names = {row[0] for row in await cursor.fetchall()}

            server_sub_counts = {}
            if interaction.guild:
                server_member_ids = [m.id for m in interaction.guild.members]
                if server_member_ids:
                    placeholders = ",".join("?" for _ in server_member_ids)
                    async with db.execute(
                        f"""
                        SELECT LOWER(g.name), COUNT(s.user_id)
                        FROM subscriptions s
                        JOIN igdb_games g ON s.game_id = g.id
                        WHERE s.user_id IN ({placeholders})
                        GROUP BY g.id
                        """,
                        server_member_ids
                    ) as cursor:
                        for g_name_low, cnt in await cursor.fetchall():
                            server_sub_counts[g_name_low] = cnt
            else:
                # Fallback for DMs: count global subscriptions
                async with db.execute(
                    """
                    SELECT LOWER(g.name), COUNT(s.user_id)
                    FROM subscriptions s
                    JOIN igdb_games g ON s.game_id = g.id
                    GROUP BY g.id
                    """
                ) as cursor:
                    for g_name_low, cnt in await cursor.fetchall():
                        server_sub_counts[g_name_low] = cnt

            if user_searching_dlc:
                query_t1 = """
                           SELECT g.name, g.is_multiplayer, g.total_rating_count,
                                  CASE WHEN s.user_id IS NOT NULL THEN 1 ELSE 0 END as is_sub
                           FROM igdb_games g
                                    LEFT JOIN subscriptions s ON g.id = s.game_id AND s.user_id = ?
                           WHERE (g.name LIKE ? OR g.name LIKE ?)
                           ORDER BY g.is_multiplayer DESC, g.total_rating_count DESC
                           LIMIT 35
                           """
                params_t1 = (user_id, f"{search_term}%", f"%{search_term}%")
            else:
                query_t1 = """
                           SELECT g.name, g.is_multiplayer, g.total_rating_count,
                                  CASE WHEN s.user_id IS NOT NULL THEN 1 ELSE 0 END as is_sub
                           FROM igdb_games g
                                    LEFT JOIN subscriptions s ON g.id = s.game_id AND s.user_id = ?
                           WHERE (
                               s.user_id IS NOT NULL
                                   OR (
                                   (g.category IN (0, 8, 9, 10, 11) OR g.category IS NULL)
                                       AND (g.parent_game_id IS NULL OR g.parent_game_id = 0)
                                       AND (g.version_parent_id IS NULL OR g.version_parent_id = 0)
                                   )
                               )
                             AND (g.name LIKE ? OR g.name LIKE ?)
                           ORDER BY g.is_multiplayer DESC, g.total_rating_count DESC
                           LIMIT 35
                           """
                params_t1 = (user_id, f"{search_term}%", f"%{search_term}%")

            async with db.execute(query_t1, params_t1) as cursor:
                rows_t1 = await cursor.fetchall()
                for name, is_mp, rating_cnt, is_sub in rows_t1:
                    if name.lower() not in seen_names:
                        match_tier, similarity = compute_match_score(name, search_term)
                        if should_keep_candidate(name, rating_cnt, match_tier, similarity, inner_is_sub=bool(is_sub)):
                            sort_score = get_sort_score(match_tier, is_mp, is_sub)
                            sub_cnt = server_sub_counts.get(name.lower(), 0)
                            # Tuple structure: (sort_score, -similarity, -sub_cnt, -rating_cnt, name, is_mp, is_sub)
                            choice_candidates.append((sort_score, -similarity, -sub_cnt, -(rating_cnt or 0), name, is_mp, is_sub))
                            seen_names.add(name.lower())

            # 2b. EXPLICIT DB FETCH FOR EXACT MATCH
            if search_term.lower() not in seen_names:
                query_exact = """
                              SELECT g.name, g.is_multiplayer, g.total_rating_count,
                                     CASE WHEN s.user_id IS NOT NULL THEN 1 ELSE 0 END as is_sub
                              FROM igdb_games g
                                       LEFT JOIN subscriptions s ON g.id = s.game_id AND s.user_id = ?
                              WHERE g.name = ? COLLATE NOCASE
                              LIMIT 1
                              """
                async with db.execute(query_exact, (user_id, search_term)) as cursor:
                    exact_row = await cursor.fetchone()
                    if exact_row:
                        name, is_mp, rating_cnt, is_sub = exact_row
                        match_tier, similarity = compute_match_score(name, search_term)
                        sort_score = get_sort_score(match_tier, is_mp, is_sub)
                        sub_cnt = server_sub_counts.get(name.lower(), 0)
                        choice_candidates.append((sort_score, -similarity, -sub_cnt, -(rating_cnt or 0), name, is_mp, is_sub))
                        seen_names.add(name.lower())

        # 3. LIVE IGDB SEARCH
        if len(search_term) >= 2:
            client_id = os.getenv("TWITCH_CLIENT_ID")
            access_token = os.getenv("TWITCH_ACCESS_TOKEN")

            if client_id and access_token:
                headers = {"Client-ID": client_id, "Authorization": f"Bearer {access_token}"}
                body = f'fields name, total_rating_count, game_modes, category, parent_game, version_parent; search "{igdb_search_query}"; limit 35;'

                try:
                    timeout = aiohttp.ClientTimeout(total=2.5)
                    async with aiohttp.ClientSession(timeout=timeout) as session:
                        async with session.post("https://api.igdb.com/v4/games", headers=headers, data=body) as resp:
                            if resp.status == 200:
                                igdb_results = await resp.json()
                                for item in igdb_results:
                                    g_name = item.get("name", "")
                                    r_cnt = item.get("total_rating_count", 0)
                                    cat = item.get("category", 0)
                                    modes = item.get("game_modes", [])


                                    # Only filter out non-main categories if user isn't searching for DLCs
                                    # We allow parent_game / version_parent for standalone indie games
                                    if not user_searching_dlc:
                                        if cat is not None and cat not in {0, 8, 9, 10, 11}:
                                            continue

                                    raw_mode_ids = set()
                                    for m in modes or []:
                                        if isinstance(m, dict):
                                            raw_mode_ids.add(m.get("id"))
                                        elif isinstance(m, int):
                                            raw_mode_ids.add(m)

                                    is_mp = 1 if raw_mode_ids.intersection({2, 3, 4, 5, 6}) else 0

                                    if g_name.lower() in seen_names:
                                        continue

                                    # Check user's subscription status for live IGDB games
                                    is_sub = 1 if g_name.lower() in user_subscribed_names else 0

                                    match_tier, similarity = compute_match_score(g_name, search_term)
                                    if should_keep_candidate(g_name, r_cnt, match_tier, similarity, inner_is_sub=bool(is_sub)):
                                        sort_score = get_sort_score(match_tier, is_mp, is_sub)
                                        sub_cnt = server_sub_counts.get(g_name.lower(), 0)
                                        choice_candidates.append((sort_score, -similarity, -sub_cnt, -(r_cnt or 0), g_name, is_mp, is_sub))
                                        seen_names.add(g_name.lower())
                except Exception as e:
                    print(f"IGDB Autocomplete Exception: {e}")
                    pass

        # 4. SORTING & DISCORD CHOICE GENERATION
        choice_candidates.sort(key=lambda x: (x[0], x[2], x[3], x[1], len(x[4])))

        choices = []
        for item in choice_candidates[:25]:
            g_name = item[4]
            is_mp = item[5]
            is_sub = item[6]

            sp_tag = " [SP 👤]" if is_mp == 0 else ""
            sub_tag = sub_icon if is_sub else ""

            display_name = f"{sub_tag}{g_name}{sp_tag}"

            choices.append(app_commands.Choice(name=display_name[:100], value=g_name[:100]))

        return choices

    except (discord.NotFound, discord.HTTPException):
        return []

def format_interval(hours: float) -> str:
    """Helper to cleanly format update intervals into minutes or hours."""
    if hours < 1:
        mins = int(hours * 60)
        return f"every {mins} minute" if mins == 1 else f"every {mins} minutes"
    elif hours == 1:
        return "every 1 hour"
    else:
        display_val = int(hours) if hours.is_integer() else hours
        return f"every {display_val} hours"

def get_bot_owner_id() -> int:
    raw_owner_id = os.getenv("BOT_OWNER_ID")
    return int(raw_owner_id) if raw_owner_id and raw_owner_id.isdigit() else 0

# noinspection PyTypeChecker
class GenericConfirmationView(discord.ui.View):
    def __init__(
            self,
            author_id: int,
            confirm_label: str = "Confirm",
            confirm_message: str = "Action completed.",
            cancel_message: str = "Action canceled.",
            on_confirm: Optional[Callable[[discord.Interaction], Awaitable[None]]] = None,
            timeout: float = 30.0
    ):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.confirm_message = confirm_message
        self.cancel_message = cancel_message
        self.on_confirm = on_confirm

        # Dynamically set button label
        self.confirm_button.label = confirm_label

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("You cannot use these buttons.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm_button(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if self.on_confirm:
            await self.on_confirm(interaction)
        self.stop()
        await interaction.response.edit_message(content=self.confirm_message, view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, _button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content=self.cancel_message, view=None)