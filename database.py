import asyncio
import os
import time

import aiohttp
import aiosqlite

# Setup persistent data path
DATA_DIR = os.getenv("DATA_DIR", ".")
DB_PATH = os.path.join(DATA_DIR, "db.sqlite")

# Ensure the data directory exists before opening SQLite
os.makedirs(DATA_DIR, exist_ok=True)

async def ensure_columns(db: aiosqlite.Connection, table_name: str, expected_columns: dict):
    """Helper to safely add missing columns to an existing table without breaking schema."""
    async with db.execute(f"PRAGMA table_info({table_name});") as cursor:
        existing_columns = [col[1] for col in await cursor.fetchall()]

    for col_name, col_type in expected_columns.items():
        if col_name not in existing_columns:
            await db.execute(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_type};")


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        # 1. Global User Settings Table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                ping_template TEXT,
                sub_indicator TEXT DEFAULT '⭐',
                quiet_mode INTEGER NOT NULL DEFAULT 0
            );
        """)

        # 2. Server-Specific Overrides Table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_guild_settings (
                user_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                quiet_mode INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, guild_id)
            );
        """)

        # Migration helper: Check if old user_settings has guild_id column
        async with db.execute("PRAGMA table_info(user_settings);") as cursor:
            cols = [col[1] for col in await cursor.fetchall()]

        if "guild_id" in cols:
            print("[DB Setup] Migrating old user_settings to normalized schema...")

            # Step A: Create temporary table for new user_settings format
            await db.execute("""
                CREATE TABLE IF NOT EXISTS user_settings_v2 (
                    user_id INTEGER PRIMARY KEY,
                    ping_template TEXT,
                    sub_indicator TEXT DEFAULT '⭐',
                    quiet_mode INTEGER NOT NULL DEFAULT 0
                );
            """)

            # Step B: Copy global rows (guild_id = 0) into user_settings_v2
            await db.execute("""
                INSERT OR IGNORE INTO user_settings_v2 (user_id, ping_template, sub_indicator, quiet_mode)
                SELECT user_id, ping_template, COALESCE(sub_indicator, '⭐'), quiet_mode
                FROM user_settings
                WHERE guild_id = 0;
            """)

            # Step C: Copy server-scoped rows (guild_id != 0) into user_guild_settings
            await db.execute("""
                INSERT OR IGNORE INTO user_guild_settings (user_id, guild_id, quiet_mode)
                SELECT user_id, guild_id, quiet_mode
                FROM user_settings
                WHERE guild_id != 0;
            """)

            # Step D: Swap tables
            await db.execute("DROP TABLE user_settings;")
            await db.execute("ALTER TABLE user_settings_v2 RENAME TO user_settings;")
            print("[DB Setup] Migration completed successfully!")

        await db.commit()

        # 6. Repair missing igdb_games entries for unsubscribe autocomplete
        async with db.execute(
            """
            SELECT DISTINCT s.game_id 
            FROM subscriptions s 
            LEFT JOIN igdb_games g ON s.game_id = g.id 
            WHERE g.id IS NULL
            """
        ) as cursor:
            missing_ids = [row[0] for row in await cursor.fetchall()]

        client_id = os.getenv("TWITCH_CLIENT_ID")
        access_token = os.getenv("TWITCH_ACCESS_TOKEN")

        if missing_ids and client_id and access_token:
            headers = {
                "Client-ID": client_id,
                "Authorization": f"Bearer {access_token}"
            }
            chunk_size = 500
            for i in range(0, len(missing_ids), chunk_size):
                chunk = missing_ids[i:i + chunk_size]
                ids_str = ",".join(str(gid) for gid in chunk)
                body = f"fields id, name, category, first_release_date; where id = ({ids_str}); limit {chunk_size};"

                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.post("https://api.igdb.com/v4/games", headers=headers, data=body) as resp:
                            if resp.status == 200:
                                games = await resp.json()
                                updates = [
                                    (
                                        g["id"],
                                        g["name"],
                                        int(time.time()),
                                        g.get("first_release_date"),
                                        g.get("category", 0)
                                    )
                                    for g in games
                                ]
                                await db.executemany(
                                    """
                                    INSERT OR IGNORE INTO igdb_games (id, name, updated_at, first_release_date, category)
                                    VALUES (?, ?, ?, ?, ?)
                                    """,
                                    updates
                                )
                                await db.commit()
                except Exception as e:
                    print(f"[DB Setup] Failed to repair orphan subscription: {e}")

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
            WHERE id NOT IN (SELECT DISTINCT game_id FROM subscriptions)
              AND (
                is_multiplayer = 0
                    OR parent_game_id IS NOT NULL
                    OR version_parent_id IS NOT NULL
                    OR category NOT IN (0, 8, 9, 10, 11)
                )
        """
        cursor = await db.execute(cleanup_query)
        deleted_count = cursor.rowcount
        await db.commit()

        # Reclaim disk space if entries were deleted
        if deleted_count > 0:
            print("[Auto-Migration] Reclaiming disk space via VACUUM...")
            await db.execute("VACUUM")

    print(f"[Auto-Migration] Successfully pruned {deleted_count} unassigned single-player games from local database.")