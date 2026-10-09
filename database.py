import asyncio
import os
import time

from igdb_client import igdb
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
        # ---------------------------------------------------------------------
        # STEP 1: Create all tables first before running any queries/migrations
        # ---------------------------------------------------------------------

        # 1. IGDB Games Table
        await db.execute("""
                         CREATE TABLE IF NOT EXISTS igdb_games (
                                                                   id INTEGER PRIMARY KEY,
                                                                   name TEXT NOT NULL,
                                                                   updated_at INTEGER NOT NULL,
                                                                   first_release_date INTEGER DEFAULT NULL,
                                                                   category INTEGER DEFAULT 0,
                                                                   parent_game_id INTEGER DEFAULT NULL,
                                                                   version_parent_id INTEGER DEFAULT NULL,
                                                                   is_multiplayer INTEGER DEFAULT 0,
                                                                   total_rating_count INTEGER DEFAULT 0
                         );
                         """)

        # 2. Subscriptions Table
        await db.execute("""
                         CREATE TABLE IF NOT EXISTS subscriptions (
                                                                      user_id INTEGER NOT NULL,
                                                                      game_id INTEGER NOT NULL,
                                                                      PRIMARY KEY (user_id, game_id),
                                                                      FOREIGN KEY (game_id) REFERENCES igdb_games (id) ON DELETE CASCADE
                         );
                         """)

        # 3. Guild LFG Settings Table
        await db.execute("""
                         CREATE TABLE IF NOT EXISTS guild_lfg_settings (
                                                                           guild_id INTEGER PRIMARY KEY,
                                                                           mode TEXT NOT NULL DEFAULT 'all',
                                                                           whitelisted_channels TEXT NOT NULL DEFAULT '',
                                                                           game_ping_cooldown_seconds INTEGER NOT NULL DEFAULT 30
                         );
                         """)

        # 4. Per-Guild / Per-Game Ping Cooldown State
        await db.execute("""
                         CREATE TABLE IF NOT EXISTS guild_game_ping_state (
                                                                             guild_id INTEGER NOT NULL,
                                                                             game_id INTEGER NOT NULL,
                                                                             last_ping_at INTEGER NOT NULL DEFAULT 0,
                                                                             PRIMARY KEY (guild_id, game_id),
                                                                             FOREIGN KEY (game_id) REFERENCES igdb_games (id) ON DELETE CASCADE
                         );
                         """)

        # 5. Roles Allowed to Use LFG Pings
        await db.execute("""
                         CREATE TABLE IF NOT EXISTS guild_ping_roles (
                                                                        guild_id INTEGER NOT NULL,
                                                                        role_id INTEGER NOT NULL,
                                                                        PRIMARY KEY (guild_id, role_id)
                         );
                         """)

        # 6. Quickstart Messages Table
        # Quickstart Messages Table
        await db.execute("""
                         CREATE TABLE IF NOT EXISTS quickstart_messages (
                                                                            message_id INTEGER PRIMARY KEY,
                                                                            guild_id INTEGER NOT NULL,
                                                                            channel_id INTEGER NOT NULL,
                                                                            created_at INTEGER NOT NULL
                         );
                         """)

        # 7. Live Top Games Messages Table
        await db.execute("""
                 CREATE TABLE IF NOT EXISTS live_top_games (
                                                               message_id INTEGER PRIMARY KEY,
                                                               guild_id INTEGER NOT NULL,
                                                               channel_id INTEGER NOT NULL,
                                                               entry_limit INTEGER NOT NULL DEFAULT 10,
                                                               update_interval REAL NOT NULL DEFAULT 1.0,
                                                               last_updated INTEGER NOT NULL DEFAULT 0,
                                                               created_at INTEGER NOT NULL DEFAULT 0
                 );
                 """)
        # Commit initial table creations so they exist for subsequent checks/migrations
        await db.commit()

        # ---------------------------------------------------------------------
        # STEP 2: Safe User Settings Composite Primary Key Creation & Migration
        # ---------------------------------------------------------------------

        # 1. Create user_settings with composite PRIMARY KEY if it doesn't exist
        await db.execute("""
                         CREATE TABLE IF NOT EXISTS user_settings (
                                                                      user_id INTEGER NOT NULL,
                                                                      guild_id INTEGER NOT NULL DEFAULT 0,
                                                                      quiet_mode INTEGER DEFAULT 0,
                                                                      ping_template TEXT,
                                                                      sub_indicator TEXT DEFAULT '⭐',
                                                                      PRIMARY KEY (user_id, guild_id)
                         );
                         """)
        await db.commit()

        # 2. Check Primary Key column count on existing table
        async with db.execute("PRAGMA table_info(user_settings);") as cursor:
            table_info = await cursor.fetchall()

        # col[5] in PRAGMA table_info indicates if column is part of PK (> 0)
        pk_count = sum(1 for col in table_info if col[5] > 0)

        # 3. If schema was created with single PK (user_id only), rebuild with composite PK
        if pk_count < 2:
            await db.execute("""
                             CREATE TABLE user_settings_dg_tmp (
                                                                   user_id INTEGER NOT NULL,
                                                                   guild_id INTEGER NOT NULL DEFAULT 0,
                                                                   quiet_mode INTEGER DEFAULT 0,
                                                                   ping_template TEXT,
                                                                   sub_indicator TEXT DEFAULT '⭐',
                                                                   PRIMARY KEY (user_id, guild_id)
                             );
                             """)
            await db.execute("""
                             INSERT OR IGNORE INTO user_settings_dg_tmp (user_id, guild_id, quiet_mode, ping_template, sub_indicator)
                             SELECT user_id, 0, quiet_mode, ping_template, sub_indicator FROM user_settings;
                             """)
            await db.execute("DROP TABLE user_settings;")
            await db.execute("ALTER TABLE user_settings_dg_tmp RENAME TO user_settings;")
            await db.commit()

        await db.execute("""
                         CREATE TABLE IF NOT EXISTS user_guild_settings (
                                                                            user_id INTEGER NOT NULL,
                                                                            guild_id INTEGER NOT NULL,
                                                                            quiet_mode INTEGER DEFAULT 0,
                                                                            PRIMARY KEY (user_id, guild_id)
                         );
                         """)
        await db.commit()

        # ---------------------------------------------------------------------
        # STEP 3: Ensure missing columns & indexes exist
        # ---------------------------------------------------------------------
        await ensure_columns(db, "igdb_games", {
            "first_release_date": "INTEGER DEFAULT NULL",
            "is_multiplayer": "INTEGER DEFAULT 0",
            "parent_game_id": "INTEGER DEFAULT NULL",
            "version_parent_id": "INTEGER DEFAULT NULL",
            "total_rating_count": "INTEGER DEFAULT 0"
        })

        await ensure_columns(db, "guild_lfg_settings", {
            "mode": "TEXT NOT NULL DEFAULT 'all'",
            "whitelisted_channels": "TEXT NOT NULL DEFAULT ''",
            "game_ping_cooldown_seconds": "INTEGER NOT NULL DEFAULT 30"
        })

        await ensure_columns(db, "live_top_games", {
            "guild_id": "INTEGER",
            "channel_id": "INTEGER",
            "entry_limit": "INTEGER NOT NULL DEFAULT 10",
            "update_interval": "REAL NOT NULL DEFAULT 1.0",
            "last_updated": "INTEGER NOT NULL DEFAULT 0",
            "created_at": "INTEGER NOT NULL DEFAULT 0"
        })

        await db.execute(
            "CREATE INDEX IF NOT EXISTS "
            "idx_igdb_multiplayer_name ON igdb_games(is_multiplayer, name);"
        )

        await db.execute(
            "CREATE INDEX IF NOT EXISTS "
            "idx_live_top_games_guild ON live_top_games(guild_id);"
        )

        await db.commit()

        # ---------------------------------------------------------------------
        # STEP 4: Repair Orphan Subscriptions / Fetch Missing Games from IGDB
        # ---------------------------------------------------------------------
        async with db.execute("""
                              SELECT DISTINCT s.game_id
                              FROM subscriptions s
                                       LEFT JOIN igdb_games g ON s.game_id = g.id
                              WHERE g.id IS NULL
                              """) as cursor:
            missing_ids = [row[0] for row in await cursor.fetchall()]

        if missing_ids and igdb.configured:
            chunk_size = 500
            for i in range(0, len(missing_ids), chunk_size):
                chunk = missing_ids[i:i + chunk_size]
                ids_str = ",".join(str(gid) for gid in chunk)
                body = f"fields id, name, category, first_release_date; where id = ({ids_str}); limit {chunk_size};"

                try:
                    async with igdb.post("https://api.igdb.com/v4/games", data=body) as resp:
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

async def get_guild_ping_role_ids(guild_id: int) -> list[int]:
    """Return configured role IDs allowed to call /ping_game in a guild."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
                "SELECT role_id FROM guild_ping_roles WHERE guild_id = ? ORDER BY role_id",
                (guild_id,)
        ) as cursor:
            rows = await cursor.fetchall()

    return [row[0] for row in rows]

async def claim_game_ping_cooldown(
        guild_id: int,
        game_id: int,
        now: int | None = None
) -> tuple[bool, int]:
    """
    Atomically claim the per-guild/per-game ping cooldown slot.

    Returns (allowed, retry_after_seconds). When the configured cooldown is 0
    or absent, the claim is always allowed and no state row is written.
    """
    current_time = int(time.time()) if now is None else int(now)

    async with aiosqlite.connect(DB_PATH) as db:
        # Serialize competing writers so two simultaneous users cannot both pass
        # the read-before-write cooldown check.
        await db.execute("BEGIN IMMEDIATE")

        try:
            async with db.execute(
                    "SELECT game_ping_cooldown_seconds FROM guild_lfg_settings WHERE guild_id = ?",
                    (guild_id,)
            ) as cursor:
                row = await cursor.fetchone()

            cooldown_seconds = max(0, int(row[0])) if row and row[0] is not None else 30

            if cooldown_seconds == 0:
                await db.commit()
                return True, 0

            async with db.execute(
                    "SELECT last_ping_at FROM guild_game_ping_state WHERE guild_id = ? AND game_id = ?",
                    (guild_id, game_id)
            ) as cursor:
                state_row = await cursor.fetchone()

            if state_row:
                elapsed = max(0, current_time - int(state_row[0]))
                if elapsed < cooldown_seconds:
                    await db.commit()
                    return False, cooldown_seconds - elapsed

            await db.execute(
                """
                INSERT INTO guild_game_ping_state (guild_id, game_id, last_ping_at)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id, game_id) DO UPDATE SET
                    last_ping_at = excluded.last_ping_at
                """,
                (guild_id, game_id, current_time)
            )
            await db.commit()
            return True, 0

        except Exception:
            await db.rollback()
            raise

async def auto_migrate_multiplayer_flags():

    if not igdb.configured:
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

        chunk_size = 500
        updated_mp = 0

        # Valid multiplayer IGDB game mode IDs: 2 (Multiplayer), 3 (Co-op), 4 (Split screen), 5 (MMO), 6 (Battle Royale)
        multiplayer_mode_ids = {2, 3, 4, 5, 6}

        for i in range(0, total_to_fix, chunk_size):
            chunk = unclassified_ids[i:i + chunk_size]
            ids_str = ",".join(str(gid) for gid in chunk)
            body = f"fields id, game_modes; where id = ({ids_str}); limit {chunk_size};"

            try:
                async with igdb.post("https://api.igdb.com/v4/games", data=body) as resp:
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

                    else:
                        print(f"[Auto-Migration] API error {resp.status}; aborting backfill before cleanup.")
                        return

            except Exception as e:
                print(f"[Auto-Migration] Batch error: {e}; skipping cleanup.")
                return

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