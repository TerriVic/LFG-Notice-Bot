import asyncio
import os

import aiohttp
import aiosqlite

# Setup persistent data path
DATA_DIR = os.getenv("DATA_DIR", ".")
DB_PATH = os.path.join(DATA_DIR, "db.sqlite")

# Ensure the data directory exists before opening SQLite
os.makedirs(DATA_DIR, exist_ok=True)

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

        async with db.execute("PRAGMA table_info(live_top_games);") as cursor:
            columns = [col[1] for col in await cursor.fetchall()]

        if columns:
            if "update_interval" not in columns:
                await db.execute("ALTER TABLE live_top_games ADD COLUMN update_interval INTEGER DEFAULT 1;")
            if "last_updated" not in columns:
                await db.execute("ALTER TABLE live_top_games ADD COLUMN last_updated INTEGER DEFAULT 0;")

        await db.execute("""
                         CREATE TABLE IF NOT EXISTS live_top_games (
                                                                       message_id INTEGER PRIMARY KEY,
                                                                       guild_id INTEGER NOT NULL,
                                                                       channel_id INTEGER NOT NULL,
                                                                       entry_limit INTEGER DEFAULT 10,
                                                                       update_interval INTEGER DEFAULT 1,
                                                                       last_updated INTEGER DEFAULT 0,
                                                                       created_at INTEGER DEFAULT (strftime('%s', 'now'))
                         );
                         """)

        await db.execute("""
                         CREATE TABLE IF NOT EXISTS user_settings (
                                                                      user_id INTEGER PRIMARY KEY,
                                                                      ping_template TEXT
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
        multiplayer_mode_ids = {2, 3, 4, 5, 6}

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
                                if mode_ids.intersection(multiplayer_mode_ids):
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
                          AND id NOT IN (SELECT DISTINCT game_id FROM subscriptions) \
                        """
        cursor = await db.execute(cleanup_query)
        deleted_count = cursor.rowcount
        await db.commit()

        # Reclaim disk space if entries were deleted
        if deleted_count > 0:
            print("[Auto-Migration] Reclaiming disk space via VACUUM...")
            await db.execute("VACUUM")

    print(f"[Auto-Migration] Successfully pruned {deleted_count} unassigned single-player games from local database.")
