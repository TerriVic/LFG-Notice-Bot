import difflib
import os
import re
import time

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

async def fetch_and_cache_game_from_igdb(game_name: str, allow_dlc: bool = False) -> tuple[int, str] | None:
    client_id = os.getenv("TWITCH_CLIENT_ID")
    access_token = os.getenv("TWITCH_ACCESS_TOKEN")

    if not client_id or not access_token:
        return None

    headers = {"Client-ID": client_id, "Authorization": f"Bearer {access_token}"}
    clean_search = normalize_game_name(game_name)
    body = f'fields id, name, category, first_release_date, version_parent, parent_game, game_modes, total_rating_count; search "{clean_search}"; limit 10;'

    async with aiohttp.ClientSession() as session:
        async with session.post("https://api.igdb.com/v4/games", headers=headers, data=body) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()

    if not data:
        return None

    # Find the first entry that is a true base game
    selected = None
    for item in data:
        p_game = extract_id(item.get("parent_game"))
        v_parent = extract_id(item.get("version_parent"))
        g_name = item.get("name", "")

        # Ignore DLC items unless explicitly permitted by search
        if not allow_dlc and GENERIC_DLC_PATTERN.search(g_name):
            continue

        if p_game is None and v_parent is None:
            selected = item
            break

    if not selected:
        selected = data[0]

    game_id = selected["id"]
    official_name = selected["name"]
    category = selected.get("category", 0)
    first_release_date = selected.get("first_release_date")
    vp = extract_id(selected.get("version_parent"))
    p_game = extract_id(selected.get("parent_game"))
    rating_count = selected.get("total_rating_count", 0)

    # Check if IGDB game_modes contains any multiplayer mode (2=Multiplayer, 3=Co-op, 4=Split screen, 5=MMO, 6=Battle Royale)
    modes = selected.get("game_modes", [])
    raw_mode_ids = set()
    for m in modes or []:
        if isinstance(m, dict):
            raw_mode_ids.add(m.get("id"))
        elif isinstance(m, int):
            raw_mode_ids.add(m)

    is_mp = 1 if raw_mode_ids.intersection({2, 3, 4, 5, 6}) else 0

    # Save to SQLite with total_rating_count passed properly in the tuple
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO igdb_games
            (id, name, updated_at, first_release_date, category, parent_game_id, version_parent_id, is_multiplayer, total_rating_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                                       updated_at = excluded.updated_at,
                                       first_release_date = excluded.first_release_date,
                                       category = excluded.category,
                                       parent_game_id = excluded.parent_game_id,
                                       version_parent_id = excluded.version_parent_id,
                                       is_multiplayer = excluded.is_multiplayer,
                                       total_rating_count = excluded.total_rating_count
            """,
            (game_id, official_name, int(time.time()), first_release_date, category, p_game, vp, is_mp, rating_count)
        )
        await db.commit()

    return game_id, official_name

async def resolve_to_base_game(game_id: int, official_name: str) -> tuple[int, str]:
    """Recursively resolves special edition IDs to their true base game entry asynchronously."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT version_parent_id FROM igdb_games WHERE id = ?", (game_id,)) as cursor:
            row = await cursor.fetchone()

        if row and row[0]:
            parent_id = row[0]
            async with db.execute("SELECT id, name FROM igdb_games WHERE id = ?", (parent_id,)) as cursor:
                parent_row = await cursor.fetchone()
            if parent_row:
                return await resolve_to_base_game(parent_row[0], parent_row[1])

    return game_id, official_name

async def autocomplete_games(_interaction: discord.Interaction, current: str):
    try:
        clean_current = current.strip().replace("’", "'").replace("‘", "'").replace('"', '')

        # 1. Default empty input handling
        if not clean_current:
            async with aiosqlite.connect(DB_PATH) as db:
                query_default = """
                                SELECT name
                                FROM igdb_games
                                WHERE is_multiplayer = 1
                                  AND (parent_game_id IS NULL OR parent_game_id = 0)
                                  AND (version_parent_id IS NULL OR version_parent_id = 0)
                                ORDER BY total_rating_count DESC, id
                                    LIMIT 25
                                """
                async with db.execute(query_default) as cursor:
                    rows = await cursor.fetchall()
                    return [app_commands.Choice(name=r[0][:100], value=r[0][:100]) for r in rows if not GENERIC_DLC_PATTERN.search(r[0])]

        # Detect DLC intent before stripping trailing punctuation
        user_searching_dlc = any(char in clean_current for char in [":", "-", "–", "—"]) or \
                             any(k in clean_current.lower() for k in ["dlc", "expansion", "pack", "add-on"])

        # Strip trailing punctuation/spaces (e.g., "abiotic factor:" -> "abiotic factor")
        search_term = re.sub(r'[:\-–—\s]+$', '', clean_current)
        if not search_term:
            search_term = clean_current

        # Sanitize for IGDB API to prevent 400 Bad Request on colons/dashes
        igdb_search_query = re.sub(r'[:\-–—]', ' ', search_term).strip()
        igdb_search_query = re.sub(r'\s+', ' ', igdb_search_query)

        choice_candidates = []
        seen_names = set()

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

            ratio = difflib.SequenceMatcher(None, q_low, t_low).ratio()
            return 3, ratio  # Fuzzy Match

        def get_sort_score(inner_match_tier: int, inner_is_mp: int) -> int:
            """
            This guarantees:
            1. Multiplayer Exact/Sequels (Portal 2)
            2. Singleplayer Exact/Sequels (Portal 1, Slay the Spire)
            3. Multiplayer Prefix/Fuzzy (Portal Knights)
            ...ensuring single-player exact matches NEVER get buried by irrelevant multiplayer games.
            """
            if inner_match_tier == 0 and inner_is_mp == 1: return 0  # MP Exact
            if inner_match_tier == 1 and inner_is_mp == 1: return 1  # MP Sequel
            if inner_match_tier == 0 and inner_is_mp == 0: return 2  # SP Exact
            if inner_match_tier == 1 and inner_is_mp == 0: return 3  # SP Sequel
            if inner_match_tier == 2 and inner_is_mp == 1: return 4  # MP Prefix
            if inner_match_tier == 2 and inner_is_mp == 0: return 5  # SP Prefix
            if inner_match_tier == 3 and inner_is_mp == 1: return 6  # MP Fuzzy
            return 7                                     # SP Fuzzy

        def should_keep_candidate(inner_name: str, inner_rating_cnt: int, inner_match_tier: int, inner_similarity: float) -> bool:
            name_low = inner_name.lower()

            # Filter unwanted keywords unless user searched for them explicitly
            if not user_wants_noise and any(k in name_low for k in NOISE_KEYWORDS):
                return False

            # Drop weak fuzzy matches
            if inner_match_tier == 3 and inner_similarity < 0.60:
                return False

            # Drop unrated shovelware/fan projects unless it's a direct prefix/exact match
            if (not inner_rating_cnt or inner_rating_cnt == 0) and inner_match_tier == 3:
                return False

            return True

        # 2. LOCAL SEARCH
        async with aiosqlite.connect(DB_PATH) as db:
            if user_searching_dlc:
                query_t1 = """
                           SELECT name, is_multiplayer, total_rating_count FROM igdb_games
                           WHERE (name LIKE ? OR name LIKE ?)
                           ORDER BY is_multiplayer DESC, total_rating_count DESC
                               LIMIT 35
                           """
                params_t1 = (f"{search_term}%", f"%{search_term}%")
            else:
                query_t1 = """
                           SELECT name, is_multiplayer, total_rating_count FROM igdb_games
                           WHERE (category IN (0, 8, 9, 10, 11) OR category IS NULL)
                             AND (parent_game_id IS NULL OR parent_game_id = 0)
                             AND (version_parent_id IS NULL OR version_parent_id = 0)
                             AND (name LIKE ? OR name LIKE ?)
                           ORDER BY is_multiplayer DESC, total_rating_count DESC
                               LIMIT 35 \
                           """
                params_t1 = (f"{search_term}%", f"%{search_term}%")

            async with db.execute(query_t1, params_t1) as cursor:
                rows_t1 = await cursor.fetchall()
                for name, is_mp, rating_cnt in rows_t1:
                    if (user_searching_dlc or not GENERIC_DLC_PATTERN.search(name)) and name.lower() not in seen_names:
                        match_tier, similarity = compute_match_score(name, search_term)
                        if should_keep_candidate(name, rating_cnt, match_tier, similarity):
                            sort_score = get_sort_score(match_tier, is_mp)
                            choice_candidates.append((sort_score, -similarity, -(rating_cnt or 0), name, is_mp))
                            seen_names.add(name.lower())

            # 2b. EXPLICIT DB FETCH FOR EXACT MATCH
            if search_term.lower() not in seen_names:
                query_exact = """
                              SELECT name, is_multiplayer, total_rating_count FROM igdb_games
                              WHERE name = ? COLLATE NOCASE
                                  LIMIT 1
                              """
                async with db.execute(query_exact, (search_term,)) as cursor:
                    exact_row = await cursor.fetchone()
                    if exact_row:
                        name, is_mp, rating_cnt = exact_row
                        match_tier, similarity = compute_match_score(name, search_term)
                        sort_score = get_sort_score(match_tier, is_mp)
                        choice_candidates.append((sort_score, -similarity, -(rating_cnt or 0), name, is_mp))
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

                                    def get_id(val):
                                        return val.get("id") if isinstance(val, dict) else val

                                    p_game = get_id(item.get("parent_game"))
                                    v_parent = get_id(item.get("version_parent"))

                                    if not user_searching_dlc:
                                        if cat not in {0, 8, 9, 10, 11} or p_game is not None or v_parent is not None:
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
                                    if user_searching_dlc or not GENERIC_DLC_PATTERN.search(g_name):
                                        match_tier, similarity = compute_match_score(g_name, search_term)
                                        if should_keep_candidate(g_name, r_cnt, match_tier, similarity):
                                            sort_score = get_sort_score(match_tier, is_mp)
                                            choice_candidates.append((sort_score, -similarity, -(r_cnt or 0), g_name, is_mp))
                                            seen_names.add(g_name.lower())
                except Exception as e:
                    print(f"IGDB Autocomplete Exception: {e}")
                    pass

        # 4. SORTING & DISCORD CHOICE GENERATION
        choice_candidates.sort(key=lambda x: (x[0], x[1], x[2], len(x[3])))

        choices = []
        for item in choice_candidates[:25]:
            g_name = item[3]
            is_mp = item[4]

            # Add visual indicator ONLY if single-player
            display_name = f"{g_name} [SP 👤]" if is_mp == 0 else g_name

            # name = what appears in the dropdown, value = raw title sent to command
            choices.append(app_commands.Choice(name=display_name[:100], value=g_name[:100]))

        return choices

    except (discord.NotFound, discord.HTTPException):
        return []

class ConfirmDataDeletionView(discord.ui.View):
    def __init__(self, target_user_id: int, author_id: int = None):
        super().__init__(timeout=30.0)
        self.target_user_id = target_user_id
        # Author is the person allowed to interact with buttons (defaults to target user)
        self.author_id = author_id if author_id is not None else target_user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Ensures only the user who ran the command can press the buttons
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("You cannot use these buttons.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm Deletion", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM subscriptions WHERE user_id = ?", (self.target_user_id,))
            await db.commit()

        # Stop listening to further events and remove the buttons from the message
        self.stop()
        await interaction.response.edit_message(
            content=f"**All subscription data for <@{self.target_user_id}> has been permanently deleted.**",
            view=None
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button):
        # Stop listening to further events and remove the buttons from the message
        self.stop()
        await interaction.response.edit_message(
            content="Action canceled. Your data gets to live another day.",
            view=None
        )