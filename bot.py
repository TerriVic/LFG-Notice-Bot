import os
import time
import asyncio
import re
import aiohttp
import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
import difflib

# Load environment variables
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

# Setup persistent data path
DATA_DIR = os.getenv("DATA_DIR", ".")
DB_PATH = os.path.join(DATA_DIR, "db.sqlite")
BOT_OWNER_ID = int(os.getenv("BOT_OWNER_ID"))

# Ensure the data directory exists before opening SQLite
os.makedirs(DATA_DIR, exist_ok=True)

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

# Regex patterns
EDITION_PATTERN = re.compile(
    r'[\:\-\–\—\s]*\b(Game of the Year|GOTY|Ultimate|Deluxe|Collector\'s|Enhanced|Definitive|Gold|Complete|Special|Remastered|Edition|Director\'s Cut)\b.*$',
    re.IGNORECASE
)

# GENERIC DLC REGEX: Catches any title with a colon/dash followed by common DLC/expansion keywords
GENERIC_DLC_PATTERN = re.compile(
    r'[\:\-\–\—]\s*.*?\b(DLC|Expansion|Pack|Add-on|Season Pass|Bonus Content|Soundtrack|Artbook|Episode|Chapter|Mission|Quest|Map Pack|Skin|Bundle)\b',
    re.IGNORECASE
)

# Cache for command IDs used by clickable help mentions
COMMAND_IDS = {}

NOISE_KEYWORDS = ["demake", "nokia", "flash", "collection", "bundle", "prototype", "edition"]

def normalize_game_name(name: str) -> str:
    """Strips edition suffixes, standardizes mobile quotes, and removes trailing punctuation."""
    clean_name = name.replace("’", "'").replace("‘", "'")
    clean_name = EDITION_PATTERN.sub('', clean_name)
    clean_name = re.sub(r'[\:\-\–\—\s]+$', '', clean_name)
    return clean_name.strip()

def extract_id(val):
    """Utility to safely extract ID integer whether IGDB returns int or dict."""
    if isinstance(val, dict):
        return val.get("id")
    return val

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
                         CREATE TABLE IF NOT EXISTS igdb_games (
                                                                   id INTEGER PRIMARY KEY,
                                                                   name TEXT NOT NULL,
                                                                   updated_at INTEGER,
                                                                   first_release_date INTEGER DEFAULT NULL,
                                                                   category INTEGER DEFAULT 0,
                                                                   parent_game_id INTEGER DEFAULT NULL,
                                                                   version_parent_id INTEGER DEFAULT NULL,
                                                                   is_multiplayer INTEGER DEFAULT 0,
                                                                   total_rating_count INTEGER DEFAULT 0
                         );
                         """)

        # Dynamic Migration: Fetch existing column names
        async with db.execute("PRAGMA table_info(igdb_games);") as cursor:
            columns = [column[1] for column in await cursor.fetchall()]

        # Ensure all modern columns exist on legacy databases
        if "first_release_date" not in columns:
            await db.execute("ALTER TABLE igdb_games ADD COLUMN first_release_date INTEGER DEFAULT NULL;")
        if "is_multiplayer" not in columns:
            await db.execute("ALTER TABLE igdb_games ADD COLUMN is_multiplayer INTEGER DEFAULT 0;")
        if "category" not in columns:
            await db.execute("ALTER TABLE igdb_games ADD COLUMN category INTEGER DEFAULT 0;")
        if "parent_game_id" not in columns:
            await db.execute("ALTER TABLE igdb_games ADD COLUMN parent_game_id INTEGER DEFAULT NULL;")
        if "version_parent_id" not in columns:
            await db.execute("ALTER TABLE igdb_games ADD COLUMN version_parent_id INTEGER DEFAULT NULL;")
        if "total_rating_count" not in columns:
            await db.execute("ALTER TABLE igdb_games ADD COLUMN total_rating_count INTEGER DEFAULT 0;")

        await db.execute("CREATE INDEX IF NOT EXISTS idx_igdb_multiplayer_name ON igdb_games(is_multiplayer, name);")

        await db.execute("""
                         CREATE TABLE IF NOT EXISTS subscriptions (
                                                                      user_id INTEGER NOT NULL,
                                                                      game_id INTEGER NOT NULL,
                                                                      PRIMARY KEY (user_id, game_id)
                         );
                         """)

        await db.commit()
    print("[DB Setup] Schema checked and migrated safely.")

async def auto_migrate_multiplayer_flags():
    client_id = os.getenv("TWITCH_CLIENT_ID")
    access_token = os.getenv("TWITCH_ACCESS_TOKEN")

    if not client_id or not access_token:
        print("[Auto-Migration] Missing IGDB API credentials. Skipping backfill.")
        return

    # Query unclassified games (where is_multiplayer is still 0)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id FROM igdb_games WHERE is_multiplayer = 0") as cursor:
            rows = await cursor.fetchall()
            unclassified_ids = [row[0] for row in rows]

    total_to_fix = len(unclassified_ids)
    if total_to_fix > 0:
        print(f"[Auto-Migration] Found {total_to_fix} unclassified games. Starting background backfill...")

        headers = {
            "Client-ID": client_id,
            "Authorization": f"Bearer {access_token}"
        }

        chunk_size = 500
        updated_mp = 0

        # Valid multiplayer IGDB game mode IDs: 2 (Multiplayer), 3 (Co-op), 4 (Split screen), 5 (MMO), 6 (Battle Royale)
        MULTIPLAYER_MODE_IDS = {2, 3, 4, 5, 6}

        async with aiohttp.ClientSession() as session:
            for i in range(0, total_to_fix, chunk_size):
                chunk = unclassified_ids[i:i + chunk_size]
                ids_str = ",".join(str(gid) for gid in chunk)
                body = f"fields id, game_modes; where id = ({ids_str}); limit {chunk_size};"

                try:
                    async with session.post("https://api.igdb.com/v4/games", headers=headers, data=body) as resp:
                        if resp.status == 200:
                            results = await resp.json()
                            updates = []

                            for game in results:
                                gid = game["id"]
                                raw_modes = game.get("game_modes") or []

                                # Safely extract mode IDs whether IGDB returns ints or dicts
                                mode_ids = set()
                                for m in raw_modes:
                                    if isinstance(m, dict):
                                        mode_ids.add(m.get("id"))
                                    elif isinstance(m, int):
                                        mode_ids.add(m)

                                # Check if any multiplayer mode ID exists in the game's modes
                                if mode_ids.intersection(MULTIPLAYER_MODE_IDS):
                                    updated_mp += 1
                                    updates.append((1, gid))

                            if updates:
                                async with aiosqlite.connect(DB_PATH) as db:
                                    await db.executemany(
                                        "UPDATE igdb_games SET is_multiplayer = ? WHERE id = ?",
                                        updates
                                    )
                                    await db.commit()

                        elif resp.status == 429:
                            print("[Auto-Migration] Rate limited. Waiting 2s...")
                            await asyncio.sleep(2.0)
                            continue
                        else:
                            print(f"[Auto-Migration] API error {resp.status}: {await resp.text()}")

                except Exception as e:
                    print(f"[Auto-Migration] Batch error: {e}")

                if (i // chunk_size) % 10 == 0:
                    print(f"[Auto-Migration] Progress: {i}/{total_to_fix} games processed ({updated_mp} multiplayer found)...")

                await asyncio.sleep(0.25)

            print(f"[Auto-Migration] Completed backfill! Flagged {updated_mp} games as multiplayer.")
    else:
        print("[Auto-Migration] All database entries are already classified.")

    # --- AUTOMATIC CLEANUP PHASE ---
    print("[Auto-Migration] Running database purge of unassigned single-player games...")
    async with aiosqlite.connect(DB_PATH) as db:
        cleanup_query = """
            DELETE FROM igdb_games
            WHERE is_multiplayer = 0
              AND id NOT IN (SELECT DISTINCT game_id FROM subscriptions)
        """
        cursor = await db.execute(cleanup_query)
        deleted_count = cursor.rowcount
        await db.commit()

        # Reclaim disk space if entries were deleted
        if deleted_count > 0:
            print("[Auto-Migration] Reclaiming disk space via VACUUM...")
            await db.execute("VACUUM")

    print(f"[Auto-Migration] Successfully pruned {deleted_count} unassigned single-player games from local database.")

# Custom Bot subclass to cleanly initialize tasks and command syncs
class GameBot(commands.Bot):
    async def setup_hook(self):
        await init_db()

        # Launch automatic DB classification and purge in the background
        asyncio.create_task(auto_migrate_multiplayer_flags())

        if not update_igdb_catalog.is_running():
            update_igdb_catalog.start()

        synced = await self.tree.sync()
        print(f"Synced {len(synced)} global command(s)")


client = GameBot(command_prefix="!", intents=intents)
tree = client.tree

@tasks.loop(hours=168)
async def update_igdb_catalog():
    print("[IGDB Sync] Starting catalog sync...")

    client_id = os.getenv("TWITCH_CLIENT_ID")
    access_token = os.getenv("TWITCH_ACCESS_TOKEN")

    if not client_id or not access_token:
        print("[IGDB Sync] Error: Missing TWITCH_CLIENT_ID or TWITCH_ACCESS_TOKEN.")
        return

    headers = {
        "Client-ID": client_id,
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json"
    }

    async with aiosqlite.connect(DB_PATH) as db_async:
        async with db_async.execute("SELECT MAX(id) FROM igdb_games") as async_cursor:
            row = await async_cursor.fetchone()
            last_id = row[0] if (row and row[0] is not None) else 0

        limit = 500
        total_upserted = 0
        page_count = 0

        async with aiohttp.ClientSession() as session:
            while True:
                # Filter BOTH parent_game AND version_parent directly in the API query
                body = (
                    f"fields id, name, updated_at, first_release_date, category, version_parent, parent_game, game_modes, total_rating_count; "
                    f"where id > {last_id} & version_parent = null & parent_game = null & game_modes = (2, 3, 4, 5, 6); "
                    f"sort id asc; "
                    f"limit {limit};"
                )

                async with session.post(
                        "https://api.igdb.com/v4/games",
                        headers=headers,
                        data=body
                ) as response:
                    if response.status == 429:
                        print("[IGDB Sync] Rate limited. Waiting 2 seconds...")
                        await asyncio.sleep(2.0)
                        continue
                    elif response.status != 200:
                        err_text = await response.text()
                        print(f"[IGDB Sync] API Error {response.status}: {err_text}")
                        break

                    games = await response.json()

                if not isinstance(games, list) or len(games) == 0:
                    break

                last_id = games[-1]["id"]
                page_count += 1

                # Allow Main Game (0), Remaster/Remake (8, 9, 10, 11)
                valid_categories = {0, 8, 9, 10, 11}
                filtered_games = []

                for g in games:
                    cat = g.get("category")
                    p_game = extract_id(g.get("parent_game"))
                    v_parent = extract_id(g.get("version_parent"))
                    g_name = g.get("name", "")

                    if (cat in valid_categories or cat is None) and p_game is None and v_parent is None:
                        # Generic Regex Check: Skip titles matching DLC/Expansion keywords in subtitles
                        if GENERIC_DLC_PATTERN.search(g_name):
                            continue
                        filtered_games.append((g, p_game, v_parent))

                if not filtered_games:
                    await asyncio.sleep(0.3)
                    continue

                records = []
                for g, p_game, v_parent in filtered_games:
                    records.append((
                        g["id"],
                        g["name"],
                        g.get("updated_at", int(time.time())),
                        g.get("first_release_date"),
                        g.get("category", 0),
                        p_game,
                        v_parent,
                        1,
                        g.get("total_rating_count", 0)
                    ))

                await db_async.executemany(
                    """
                    INSERT INTO igdb_games (id, name, updated_at, first_release_date, category, parent_game_id, version_parent_id, is_multiplayer, total_rating_count)
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
                    records
                )
                await db_async.commit()

                total_upserted += len(records)

                # Periodic progress logging (every 5 batches / 2,500 evaluated items)
                if page_count % 5 == 0:
                    print(f"[IGDB Sync] Batch {page_count}: Upserted {len(records)} games (Total added/updated: {total_upserted}). Last ID: {last_id}")

                await asyncio.sleep(0.3)

        print(f"[IGDB Sync] Finished catalog sync. Added/Updated {total_upserted} games across {page_count} batches.")

@update_igdb_catalog.before_loop
async def before_catalog_update():
    await client.wait_until_ready()

async def autocomplete_games(interaction: discord.Interaction, current: str):
    try:
        clean_current = current.strip().replace("’", "'").replace("‘", "'").replace('"', '')

        # 1. Default empty input handling
        if not clean_current:
            async with aiosqlite.connect(DB_PATH) as db:
                query_default = """
                                SELECT name FROM igdb_games
                                WHERE is_multiplayer = 1
                                  AND (parent_game_id IS NULL OR parent_game_id = 0)
                                  AND (version_parent_id IS NULL OR version_parent_id = 0)
                                ORDER BY total_rating_count DESC, id ASC
                                LIMIT 25
                                """
                async with db.execute(query_default) as cursor:
                    rows = await cursor.fetchall()
                    return [app_commands.Choice(name=r[0][:100], value=r[0][:100]) for r in rows if not GENERIC_DLC_PATTERN.search(r[0])]

        # Detect DLC intent before stripping trailing punctuation
        user_searching_dlc = any(char in clean_current for char in [":", "-", "–", "—"]) or \
                             any(k in clean_current.lower() for k in ["dlc", "expansion", "pack", "add-on"])

        # Strip trailing punctuation/spaces (e.g., "abiotic factor:" -> "abiotic factor")
        search_term = re.sub(r'[:\-\–\—\s]+$', '', clean_current)
        if not search_term:
            search_term = clean_current

        # Sanitize for IGDB API to prevent 400 Bad Request on colons/dashes
        igdb_search_query = re.sub(r'[:\-\–\—]', ' ', search_term).strip()
        igdb_search_query = re.sub(r'\s+', ' ', igdb_search_query)

        choice_candidates = []
        seen_names = set()

        user_wants_noise = any(k in search_term.lower() for k in NOISE_KEYWORDS)

        def compute_match_score(title: str, query: str) -> tuple[int, float]:
            t_low = title.lower().strip()
            q_low = query.lower().strip()

            if t_low == q_low:
                return (0, 1.0) # Exact Match

            clean_root = re.sub(r'\s*\b(1|2|3|4|5|i|ii|iii|iv|v)\b$', '', t_low).strip()
            if clean_root == q_low:
                return (1, 0.95) # Sequel / Root Match

            if t_low.startswith(q_low):
                ratio = difflib.SequenceMatcher(None, q_low, t_low).ratio()
                return (2, ratio) # Prefix Match

            ratio = difflib.SequenceMatcher(None, q_low, t_low).ratio()
            return (3, ratio) # Fuzzy Match

        def get_sort_score(match_tier: int, is_mp: int) -> int:
            """
            This guarantees:
            1. Multiplayer Exact/Sequels (Portal 2)
            2. Singleplayer Exact/Sequels (Portal 1, Slay the Spire)
            3. Multiplayer Prefix/Fuzzy (Portal Knights)
            ...ensuring single-player exact matches NEVER get buried by irrelevant multiplayer games.
            """
            if match_tier == 0 and is_mp == 1: return 0  # MP Exact
            if match_tier == 1 and is_mp == 1: return 1  # MP Sequel
            if match_tier == 0 and is_mp == 0: return 2  # SP Exact
            if match_tier == 1 and is_mp == 0: return 3  # SP Sequel
            if match_tier == 2 and is_mp == 1: return 4  # MP Prefix
            if match_tier == 2 and is_mp == 0: return 5  # SP Prefix
            if match_tier == 3 and is_mp == 1: return 6  # MP Fuzzy
            return 7                                     # SP Fuzzy

        def should_keep_candidate(name: str, rating_cnt: int, match_tier: int, similarity: float) -> bool:
            name_low = name.lower()

            # Filter unwanted keywords unless user searched for them explicitly
            if not user_wants_noise and any(k in name_low for k in NOISE_KEYWORDS):
                return False

            # Drop weak fuzzy matches
            if match_tier == 3 and similarity < 0.60:
                return False

            # Drop unrated shovelware/fan projects unless it's a direct prefix/exact match
            if (not rating_cnt or rating_cnt == 0) and match_tier == 3:
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
                           LIMIT 35
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

async def autocomplete_admin_unsubscribe(interaction: discord.Interaction, current: str):
    clean_current = current.strip().replace("’", "'").replace("‘", "'")

    # Pull the target user directly from interaction namespace
    target_user = interaction.namespace.user

    # If the admin hasn't selected a user in the UI yet
    if not target_user:
        return [app_commands.Choice(name="Select a user first...", value="__NONE__")]

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
                """
                SELECT g.name
                FROM subscriptions s
                         JOIN igdb_games g ON s.game_id = g.id
                WHERE s.user_id = ? AND g.name LIKE ?
                ORDER BY g.name ASC
                LIMIT 25
                """,
                (target_user.id, f"%{clean_current}%")
        ) as cursor:
            rows = await cursor.fetchall()

    if not rows:
        return [app_commands.Choice(name="User has no active subscriptions", value="__NONE__")]

    return [
        app_commands.Choice(name=row[0][:100], value=row[0][:100])
        for row in rows
    ]

# ---------------------------------------------------------
# Admin Command Group
# ---------------------------------------------------------

def is_bot_owner():
    async def predicate(interaction: discord.Interaction) -> bool:
        return interaction.user.id == BOT_OWNER_ID
    return app_commands.check(predicate)

admin_group = app_commands.Group(name="admin", description="Admin-only bot management commands")

@admin_group.command(name="subscribe_user", description="Force-subscribe a user to a game")
@is_bot_owner()
async def admin_subscribe(interaction: discord.Interaction, user: discord.User, game: str):
    await interaction.response.defer(ephemeral=True)
    clean_game = game.strip().replace("’", "'").replace("‘", "'")

    raw_id = None
    raw_name = None

    async with aiosqlite.connect(DB_PATH) as db:
        # 1. Check local catalog
        async with db.execute("SELECT id, name FROM igdb_games WHERE name = ? COLLATE NOCASE", (clean_game,)) as cursor:
            row = await cursor.fetchone()

        if row:
            raw_id, raw_name = row[0], row[1]

    # 2. Live IGDB Fallback (Handles single-player games & DLCs via delimiters)
    if not raw_id:
            user_searching_dlc = any(char in clean_game for char in [":", "-", "–", "—"])
            result = await fetch_and_cache_game_from_igdb(clean_game, allow_dlc=user_searching_dlc)
            if not result:
                await interaction.followup.send(
                f"Could not find **{clean_game}** in local catalog or on IGDB.",
                    ephemeral=True
                )
                return
            raw_id, raw_name = result

    # 3. Normalize special editions to base game
    game_id, official_name = await resolve_to_base_game(raw_id, raw_name)

    # 4. Insert subscription within a clean connection context
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO subscriptions (user_id, game_id) VALUES (?, ?)",
            (user.id, game_id)
        )
        await db.commit()

    await interaction.followup.send(
        f"✅ Subscribed {user.mention} to **{official_name}**.",
        ephemeral=True
    )

@admin_group.command(name="unsubscribe_user", description="Force-unsubscribe a user from a game")
@is_bot_owner()
async def admin_unsubscribe(interaction: discord.Interaction, user: discord.User, game: str):
    if game == "__NONE__":
        await interaction.response.send_message(
            f"**{user.display_name}** has no active subscriptions to remove.",
            ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    clean_game = game.strip().replace("’", "'").replace("‘", "'")

    raw_id = None
    raw_name = None

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, name FROM igdb_games WHERE name = ? COLLATE NOCASE", (clean_game,)) as cursor:
            row = await cursor.fetchone()

        if row:
            raw_id, raw_name = row[0], row[1]

    if not raw_id:
        user_searching_dlc = any(char in clean_game for char in [":", "-", "–", "—"])
        result = await fetch_and_cache_game_from_igdb(clean_game, allow_dlc=user_searching_dlc)
        if not result:
            await interaction.followup.send(
                f"Could not find **{clean_game}** in local catalog or on IGDB.",
                ephemeral=True
            )
            return
        raw_id, raw_name = result

    game_id, official_name = await resolve_to_base_game(raw_id, raw_name)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM subscriptions WHERE user_id = ? AND game_id = ?", (user.id, game_id))
        await db.commit()

    await interaction.followup.send(
        f"🗑️ Unsubscribed {user.mention} from **{official_name}**.",
        ephemeral=True
    )

@admin_group.command(name="delete_user_data", description="Permanently delete all subscription data for a specific user")
@is_bot_owner()
async def admin_delete_user_data(interaction: discord.Interaction, user: discord.User):
    view = ConfirmDataDeletionView(target_user_id=user.id, author_id=interaction.user.id)
    await interaction.response.send_message(
        f"⚠️ **Are you sure you want to delete all data for {user.mention}?**\nThis will permanently purge all their active subscriptions.",
        view=view,
        ephemeral=True
    )

# Bind the subscriber-specific autocomplete function
admin_subscribe.autocomplete('game')(autocomplete_games)
admin_unsubscribe.autocomplete('game')(autocomplete_admin_unsubscribe)

# REGISTRATION: Add the admin command group to the tree
tree.add_command(admin_group)

async def autocomplete_user_subscriptions(interaction: discord.Interaction, current: str):
    clean_current = current.strip().replace("’", "'").replace("‘", "'")
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
        """
        SELECT g.name
        FROM subscriptions s
                 JOIN igdb_games g ON s.game_id = g.id
        WHERE s.user_id = ? AND g.name LIKE ?
        ORDER BY g.name ASC
        LIMIT 25
        """,
        (interaction.user.id, f"%{clean_current}%")
        ) as cursor:
            rows = await cursor.fetchall()

    if not rows:
        return [
            app_commands.Choice(
                name="No subscribed games found",
                value="__NONE__"
            )
        ]

    return [
        app_commands.Choice(
            name=row[0][:100],
            value=row[0][:100]
        )
        for row in rows
    ]

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
            content="Action canceled. Subscription data was preserved.",
            view=None
        )

    async def on_timeout(self):
        # Optional: Removes buttons automatically if the user takes longer than 30s to respond
        pass

@tree.command(name="top_games", description="Show the top games on the server by subscription count")
@app_commands.describe(limit="Number of top games to display (default: 10, max: 25)", public="Set to True to share publicly (default: False)")
async def top_games(interaction: discord.Interaction, limit: int = 10, public: bool = False):
    # Guild check
    if not interaction.guild:
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return

    # Cap the maximum limit to prevent huge wall-of-text messages
    limit = max(1, min(limit, 25))

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT s.user_id, g.name FROM subscriptions s JOIN igdb_games g ON s.game_id = g.id") as cursor:
            rows = await cursor.fetchall()

    if not rows:
        await interaction.response.send_message("There are no active subscriptions in this server.", ephemeral=not public)
        return

    # Count subscriptions per game for members present in this guild
    game_counts = {}
    for user_id, game_name in rows:
        if interaction.guild.get_member(user_id):
            game_counts[game_name] = game_counts.get(game_name, 0) + 1

    if not game_counts:
        await interaction.response.send_message("No members in this server are currently subscribed to any games.", ephemeral=not public)
        return

    # Sort games by subscriber count in descending order
    sorted_games = sorted(game_counts.items(), key=lambda x: x[1], reverse=True)[:limit]
    formatted_list = [f"`#{idx + 1}` **{gname}** — {cnt} {'subscriber' if cnt == 1 else 'subscribers'}" for idx, (gname, cnt) in enumerate(sorted_games)]

    embed = discord.Embed(
        title=f"Top {len(sorted_games)} Games in {interaction.guild.name}",
        description="\n".join(formatted_list),
        color=discord.Color.gold()
    )

    await interaction.response.send_message(embed=embed, ephemeral=not public)

@tree.command(name="subscribe", description="Subscribe to a game")
@app_commands.autocomplete(game=autocomplete_games)
async def subscribe(interaction: discord.Interaction, game: str):
    await interaction.response.defer(ephemeral=True)
    clean_game = game.strip().replace("’", "'").replace("‘", "'")

    raw_id = None
    raw_name = None

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, name FROM igdb_games WHERE name = ?", (clean_game,)) as cursor:
            row = await cursor.fetchone()
    if row:
        raw_id, raw_name = row[0], row[1]

    # If game wasn't found in local multiplayer cache, fetch and store it from IGDB live
    if not raw_id:
        user_searching_dlc = any(char in clean_game for char in [":", "-", "–", "—"])
        result = await fetch_and_cache_game_from_igdb(clean_game, allow_dlc=user_searching_dlc)
        if not result:
            await interaction.followup.send(
                f"Could not find **{clean_game}** in local cache or on IGDB.",
                ephemeral=True
            )
            return

        raw_id, raw_name = result

    game_id, official_name = await resolve_to_base_game(raw_id, raw_name)

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
                "SELECT 1 FROM subscriptions WHERE user_id = ? AND game_id = ?",
                (interaction.user.id, game_id)
        ) as cursor:
            if await cursor.fetchone():
                await interaction.followup.send(f"You are already subscribed to **{official_name}**!", ephemeral=True)
                return

        await db.execute(
            "INSERT INTO subscriptions (user_id, game_id) VALUES (?, ?)",
            (interaction.user.id, game_id)
        )
        await db.commit()

    await interaction.followup.send(f"You subscribed to **{official_name}**!", ephemeral=True)

@tree.command(name="unsubscribe", description="Unsubscribe from a game")
@app_commands.autocomplete(game=autocomplete_user_subscriptions)
async def unsubscribe(interaction: discord.Interaction, game: str):
    if game == "__NONE__":
        await interaction.response.send_message(
            "You are not subscribed to any games.",
            ephemeral=True
        )
        return

    clean_game = game.strip().replace("’", "'").replace("‘", "'")
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, name FROM igdb_games WHERE name = ?", (clean_game,)) as cursor:
            row = await cursor.fetchone()

    # Handle missing game
    if not row:
        await interaction.response.send_message("Game not found.", ephemeral=True)
        return

    game_id, official_name = await resolve_to_base_game(row[0], row[1])

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM subscriptions WHERE user_id = ? AND game_id = ?",
            (interaction.user.id, game_id)
        )
        await db.commit()

    await interaction.response.send_message(f"You unsubscribed from **{official_name}**", ephemeral=True)

@tree.command(name="ping_game", description="Ping all users subscribed to a game")
@app_commands.checks.cooldown(1, 30.0)
@app_commands.autocomplete(game=autocomplete_games)
async def ping_game(interaction: discord.Interaction, game: str):
    # 1. Immediately tell Discord we are working on it to prevent timeouts
    await interaction.response.defer()

    clean_game = game.strip().replace("’", "'").replace("‘", "'")

    async with aiosqlite.connect(DB_PATH) as db:
        # 2. Look up game ID (case-insensitive)
        async with db.execute("SELECT id, name FROM igdb_games WHERE name = ? COLLATE NOCASE", (clean_game,)) as cursor:
            row = await cursor.fetchone()

        if not row:
            await interaction.followup.send("Game not found.")
            return

        # 3. Resolve base game entry
        game_id, official_name = await resolve_to_base_game(row[0], row[1])

        # 4. Fetch subscribers
        async with db.execute("SELECT user_id FROM subscriptions WHERE game_id = ?", (game_id,)) as cursor:
            users = await cursor.fetchall()

    if not users:
        await interaction.followup.send(f"No subscribers for **{official_name}**.")
        return

    mentions = " ".join(f"<@{u[0]}>" for u in users)
    await interaction.followup.send(f"Players interested in **{official_name}**: {mentions}")

@tree.command(name="user_subscriptions", description="View subscriptions for yourself or another user.")
async def user_subscriptions(interaction: discord.Interaction, user: discord.User = None, public: bool = False):
    target_user = user or interaction.user

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT g.name FROM subscriptions s JOIN igdb_games g ON s.game_id = g.id WHERE s.user_id = ? ORDER BY g.name ASC",
        (target_user.id,)
        ) as cursor:
            rows = await cursor.fetchall()

    header = "Your subscribed games" if target_user.id == interaction.user.id else f"Subscribed games for **{target_user.display_name}**"
    if not rows:
        await interaction.response.send_message("No active subscriptions found.", ephemeral=not public)
        return

    game_list = "\n".join(f"• {row[0]}" for row in rows)

    await interaction.response.send_message(
        f"**{header}** ({len(rows)}):\n{game_list}",
        ephemeral=not public
    )

@tree.command(name="show_subscribers", description="View all users in this server subscribed to a game without pinging them")
@app_commands.autocomplete(game=autocomplete_games)
async def show_subscribers(interaction: discord.Interaction, game: str, public: bool = False):
    if not interaction.guild:
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=not public)
    clean_game = game.strip().replace("’", "'").replace("‘", "'")

    raw_id = None
    raw_name = None

    # Keep database operations cleanly scoped or re-opened
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, name FROM igdb_games WHERE name = ?", (clean_game,)) as cursor:
            row = await cursor.fetchone()

    if row:
        raw_id, raw_name = row[0], row[1]

    # If game wasn't found in local multiplayer cache, fetch and store it from IGDB live
    if not raw_id:
        user_searching_dlc = any(char in clean_game for char in [":", "-", "–", "—"])
        result = await fetch_and_cache_game_from_igdb(clean_game, allow_dlc=user_searching_dlc)
        if not result:
            await interaction.followup.send(
                f"Could not find **{clean_game}** in local cache or on IGDB.",
                ephemeral=not public
            )
            return

        raw_id, raw_name = result

    game_id, official_name = await resolve_to_base_game(raw_id, raw_name)

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM subscriptions WHERE game_id = ?", (game_id,)) as cursor:
            users = await cursor.fetchall()

    if not users:
        await interaction.followup.send(f"No subscribers for **{official_name}**.", ephemeral=not public)
        return

    local_mentions = [f"<@{u[0]}>" for u in users if interaction.guild.get_member(u[0])]
    if not local_mentions:
        await interaction.followup.send(
            f"No users in **{interaction.guild.name}** are subscribed to **{official_name}**.",
            ephemeral=not public
        )
        return

    await interaction.followup.send(
        f"Players in this server interested in **{official_name}** ({len(local_mentions)}):\n{' '.join(local_mentions)}",
        ephemeral=not public,
        allowed_mentions=discord.AllowedMentions.none()
    )


@tree.command(name="delete_my_data", description="Permanently delete all your subscription data from the bot")
async def delete_my_data(interaction: discord.Interaction):
    view = ConfirmDataDeletionView(target_user_id=interaction.user.id)
    await interaction.response.send_message(
        "⚠️ **Are you sure you want to delete all your data?**\nThis will immediately remove all your active game subscriptions.",
        view=view,
        ephemeral=True
    )


@tree.command(name="help", description="Show information about available bot commands")
async def help_command(interaction: discord.Interaction):
    # Lookup helper: returns </name:id> if found, or standard `/name` backticks as fallback
    def fmt(name: str) -> str:
        cid = COMMAND_IDS.get(name)
        return f"</{name}:{cid}>" if cid else f"`/{name}`"

    embed = discord.Embed(
        title="Bot Command Guide",
        description="Here is everything you can do with the bot:",
        color=discord.Color.blue()
    )

    embed.add_field(
        name="Managing Subscriptions",
        value=f"• {fmt('subscribe')} `<game>` — Subscribe to LFG notifications.\n• {fmt('unsubscribe')} `<game>` — Unsubscribe.\n• {fmt('user_subscriptions')} — View active subscriptions.",
        inline=False
    )

    embed.add_field(
        name="Server Activity & Social",
        value=f"• {fmt('ping_game')} `<game>` — Ping subscribers.\n• {fmt('top_games')} — Popular games.\n• {fmt('show_subscribers')} `<game>` — List subscribers.",
        inline=False
    )
    embed.add_field(name="Data & Privacy", value=f"• {fmt('delete_my_data')} — Delete your data.", inline=False)
    embed.set_footer(text="Looking for a missing game or DLC? Type its full name in /subscribe!")

    await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        msg = "⛔ Only the bot owner can use administrative commands."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
        return

    if isinstance(error, app_commands.CommandOnCooldown):
        seconds = round(error.retry_after)
        msg = f"⏳ Easy there! You can ping a game again in **{seconds} seconds**."

        # Check if the interaction was already responded to or deferred
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

@client.event
async def on_ready():
    print(f"Logged in as {client.user}")

    # Populate command IDs for clickable help mentions
    try:
        fetched_cmds = await client.tree.fetch_commands()
        global COMMAND_IDS
        COMMAND_IDS = {cmd.name: cmd.id for cmd in fetched_cmds}
        print(f"[Help] Cached {len(COMMAND_IDS)} command IDs.")
    except Exception as e:
        print(f"[Help] Failed to fetch command IDs: {e}")

client.run(TOKEN)


#-------for test server---------

## GUILD_ID = os.getenv("GUILD_ID")
##@client.event
##async def on_ready():
##    guild = discord.Object(id=GUILD_ID)
##
##    tree.copy_global_to(guild=guild)  # Copies definitions to the guild
##
##    synced = await tree.sync(guild=guild)
##    print(f"Synced {len(synced)} commands to guild {GUILD_ID}")
##    print(f"Logged in as {client.user}")

## client.run(TOKEN)