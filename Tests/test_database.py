import pytest
import aiosqlite
from unittest.mock import AsyncMock, patch

# Import functions to test from database.py
from database import (
    init_db,
    ensure_columns,
    auto_migrate_multiplayer_flags,
    claim_game_ping_cooldown,
    get_guild_ping_role_ids,
)

# -----------------------------------------------------------------------------
# 1. Tests for Schema Initialization & Tables
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_init_db_creates_all_tables(tmp_path, monkeypatch):
    """Verify that init_db creates all required tables and indexes."""
    test_db_path = tmp_path / "test_db.sqlite"

    # Point DB_PATH in database module to temporary file
    monkeypatch.setattr("database.DB_PATH", str(test_db_path))

    await init_db()

    async with aiosqlite.connect(test_db_path) as db:
        async with db.execute("SELECT name FROM sqlite_master WHERE type='table';") as cursor:
            tables = [row[0] for row in await cursor.fetchall()]

    # Verify all required tables exist
    assert "igdb_games" in tables
    assert "subscriptions" in tables
    assert "user_settings" in tables
    assert "user_guild_settings" in tables
    assert "guild_lfg_settings" in tables
    assert "guild_game_ping_state" in tables
    assert "guild_ping_roles" in tables
    assert "quickstart_messages" in tables
    assert "live_top_games" in tables

# -----------------------------------------------------------------------------
# 2. Tests for Dynamic Column Migration (`ensure_columns`)
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ensure_columns_adds_missing_columns(tmp_path):
    """Ensure missing columns are added dynamically without breaking schema."""
    test_db_path = tmp_path / "test_migration.sqlite"

    async with aiosqlite.connect(test_db_path) as db:
        # Create initial legacy table with missing column
        await db.execute("CREATE TABLE test_table (id INTEGER PRIMARY KEY);")
        await db.commit()

        # Run column check helper
        await ensure_columns(db, "test_table", {"new_col": "TEXT DEFAULT 'test'"})
        await db.commit()

        # Inspect table schema
        async with db.execute("PRAGMA table_info(test_table);") as cursor:
            columns = [col[1] for col in await cursor.fetchall()]

    assert "new_col" in columns


# -----------------------------------------------------------------------------
# 3. Tests for Composite Primary Key Migration (`user_settings`)
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_user_settings_migration(tmp_path, monkeypatch):
    """Test automatic migration from single PRIMARY KEY to composite PRIMARY KEY."""
    test_db_path = tmp_path / "test_user_settings.sqlite"
    monkeypatch.setattr("database.DB_PATH", str(test_db_path))

    # Pre-populate database with legacy single-column primary key table
    async with aiosqlite.connect(test_db_path) as db:
        await db.execute("""
                         CREATE TABLE user_settings (
                                                        user_id INTEGER PRIMARY KEY,
                                                        quiet_mode INTEGER DEFAULT 0,
                                                        ping_template TEXT,
                                                        sub_indicator TEXT DEFAULT '⭐'
                         );
                         """)
        await db.execute(
            "INSERT INTO user_settings (user_id, quiet_mode) VALUES (12345, 1);"
        )
        await db.commit()

    # Run init_db to trigger automatic schema migration
    await init_db()

    async with aiosqlite.connect(test_db_path) as db:
        # Verify row data was safely migrated
        async with db.execute("SELECT user_id, guild_id, quiet_mode FROM user_settings WHERE user_id = 12345;") as cursor:
            row = await cursor.fetchone()

        assert row is not None
        assert row[0] == 12345  # user_id
        assert row[1] == 0      # guild_id default migration value
        assert row[2] == 1      # quiet_mode preserved


# -----------------------------------------------------------------------------
# 4. Tests for Multiplayer Auto-Migration & Cleanup
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
@patch("os.getenv")
@patch("database.igdb.post")
async def test_auto_migrate_multiplayer_flags_and_cleanup(mock_post, mock_getenv, tmp_path, monkeypatch):
    """Test IGDB multiplayer flag updating and pruning of single-player games."""
    test_db_path = tmp_path / "test_cleanup.sqlite"
    monkeypatch.setattr("database.DB_PATH", str(test_db_path))

    # 1. Setup mock IGDB API credentials
    mock_getenv.side_effect = lambda key, default=None: {
        "TWITCH_CLIENT_ID": "fake_client_id",
        "TWITCH_SECRET": "fake_token",
    }.get(key, default)

    # 2. Seed test database with unclassified games and subscription
    async with aiosqlite.connect(test_db_path) as db:
        await db.execute("""
                         CREATE TABLE igdb_games (
                                                     id INTEGER PRIMARY KEY,
                                                     name TEXT,
                                                     is_multiplayer INTEGER DEFAULT 0,
                                                     parent_game_id INTEGER DEFAULT NULL,
                                                     version_parent_id INTEGER DEFAULT NULL,
                                                     category INTEGER DEFAULT 0
                         );
                         """)
        await db.execute("CREATE TABLE subscriptions (user_id INTEGER, game_id INTEGER);")

        # Game 101: Unclassified multiplayer game
        await db.execute("INSERT INTO igdb_games (id, name, is_multiplayer) VALUES (101, 'Multiplayer Game', 0);")
        # Game 202: Single-player unassigned game (should be purged during cleanup)
        await db.execute("INSERT INTO igdb_games (id, name, is_multiplayer) VALUES (202, 'Singleplayer Game', 0);")

        await db.commit()

    # 3. Mock IGDB API return payload identifying game 101 as Multiplayer (mode 2)
    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=[
        {"id": 101, "game_modes": [{"id": 2}]}
    ])
    mock_post.return_value.__aenter__.return_value = mock_response

    # 4. Execute backfill and auto-cleanup
    await auto_migrate_multiplayer_flags()

    # 5. Assert game 101 updated to is_multiplayer=1 and game 202 pruned
    async with aiosqlite.connect(test_db_path) as db:
        async with db.execute("SELECT id, is_multiplayer FROM igdb_games;") as cursor:
            remaining_games = await cursor.fetchall()

    remaining_ids = [row[0] for row in remaining_games]
    assert 101 in remaining_ids
    assert 202 not in remaining_ids  # Pruned during cleanup phase
    assert remaining_games[0][1] == 1  # Flag updated to 1

@pytest.mark.asyncio
@patch("os.getenv")
@patch("database.igdb.post")
async def test_init_db_repairs_orphan_subscriptions(mock_post, mock_getenv, tmp_path, monkeypatch):
    """Test that init_db detects orphan subscriptions and fetches missing game data from IGDB."""
    test_db_path = tmp_path / "test_orphan_repair.sqlite"
    monkeypatch.setattr("database.DB_PATH", str(test_db_path))

    # Mock IGDB API credentials
    mock_getenv.side_effect = lambda key, default=None: {
        "TWITCH_CLIENT_ID": "fake_client_id",
        "TWITCH_SECRET": "fake_token",
    }.get(key, default)

    # Pre-populate database with tables and an orphan subscription (game_id 999 has no entry in igdb_games)
    async with aiosqlite.connect(test_db_path) as db:
        await db.execute("""
                         CREATE TABLE igdb_games (
                                                     id INTEGER PRIMARY KEY,
                                                     name TEXT NOT NULL,
                                                     updated_at INTEGER NOT NULL,
                                                     first_release_date INTEGER DEFAULT NULL,
                                                     category INTEGER DEFAULT 0,
                                                     parent_game_id INTEGER DEFAULT NULL,
                                                     version_parent_id INTEGER DEFAULT NULL,
                                                     is_multiplayer INTEGER DEFAULT 0
                         );
                         """)
        await db.execute("""
                         CREATE TABLE subscriptions (
                                                        user_id INTEGER NOT NULL,
                                                        game_id INTEGER NOT NULL,
                                                        PRIMARY KEY (user_id, game_id)
                         );
                         """)
        await db.execute("INSERT INTO subscriptions (user_id, game_id) VALUES (123, 999);")
        await db.commit()

    # Mock IGDB API return payload for the missing game ID (999)
    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=[
        {"id": 999, "name": "Orphaned Game", "category": 0, "first_release_date": 1600000000}
    ])
    mock_post.return_value.__aenter__.return_value = mock_response

    # Run init_db which will trigger table creation, migrations, and the orphan repair step
    await init_db()

    # Verify that the missing game was successfully fetched from the mocked API and inserted into igdb_games
    async with aiosqlite.connect(test_db_path) as db:
        async with db.execute("SELECT id, name, first_release_date FROM igdb_games WHERE id = 999;") as cursor:
            row = await cursor.fetchone()

    assert row is not None
    assert row[0] == 999
    assert row[1] == "Orphaned Game"
    assert row[2] == 1600000000

@pytest.mark.asyncio
async def test_init_db_creates_current_schema(tmp_path, monkeypatch):
    """Verify a brand-new database contains all columns required by the current bot."""
    test_db_path = tmp_path / "test_current_schema.sqlite"
    monkeypatch.setattr("database.DB_PATH", str(test_db_path))

    await init_db()

    async with aiosqlite.connect(test_db_path) as db:
        async with db.execute("PRAGMA table_info(igdb_games);") as cursor:
            igdb_columns = {row[1] for row in await cursor.fetchall()}

        async with db.execute("PRAGMA table_info(guild_lfg_settings);") as cursor:
            lfg_columns = {row[1] for row in await cursor.fetchall()}

        async with db.execute("PRAGMA table_info(live_top_games);") as cursor:
            live_columns = {row[1] for row in await cursor.fetchall()}

        async with db.execute("PRAGMA table_info(guild_game_ping_state);") as cursor:
            ping_state_columns = {row[1]: row[5] for row in await cursor.fetchall()}

        async with db.execute("PRAGMA table_info(guild_ping_roles);") as cursor:
            ping_role_columns = {row[1]: row[5] for row in await cursor.fetchall()}

    assert "total_rating_count" in igdb_columns

    assert "mode" in lfg_columns
    assert "whitelisted_channels" in lfg_columns
    assert "game_ping_cooldown_seconds" in lfg_columns

    assert ping_state_columns["guild_id"] == 1
    assert ping_state_columns["game_id"] == 2
    assert "last_ping_at" in ping_state_columns

    assert ping_role_columns["guild_id"] == 1
    assert ping_role_columns["role_id"] == 2

    assert {
        "message_id",
        "guild_id",
        "channel_id",
        "entry_limit",
        "update_interval",
        "last_updated",
        "created_at",
    }.issubset(live_columns)

@pytest.mark.asyncio
async def test_init_db_migrates_legacy_schema(tmp_path, monkeypatch):
    """Verify init_db upgrades older bot schemas without deleting existing data."""
    test_db_path = tmp_path / "test_legacy_schema.sqlite"
    monkeypatch.setattr("database.DB_PATH", str(test_db_path))

    async with aiosqlite.connect(test_db_path) as db:
        await db.execute("""
                         CREATE TABLE igdb_games (
                                                     id INTEGER PRIMARY KEY,
                                                     name TEXT NOT NULL,
                                                     updated_at INTEGER NOT NULL,
                                                     is_multiplayer INTEGER DEFAULT 0
                         );
                         """)

        await db.execute("""
                         CREATE TABLE guild_lfg_settings (
                                                             guild_id INTEGER PRIMARY KEY,
                                                             lfg_channel_id INTEGER DEFAULT NULL
                         );
                         """)

        await db.execute(
            "INSERT INTO guild_lfg_settings (guild_id, lfg_channel_id) VALUES (?, ?)",
            (12345, 67890)
        )

        await db.commit()

    await init_db()

    async with aiosqlite.connect(test_db_path) as db:
        async with db.execute("PRAGMA table_info(igdb_games);") as cursor:
            igdb_columns = {row[1] for row in await cursor.fetchall()}

        async with db.execute("PRAGMA table_info(guild_lfg_settings);") as cursor:
            lfg_columns = {row[1] for row in await cursor.fetchall()}

        async with db.execute(
                "SELECT guild_id FROM guild_lfg_settings WHERE guild_id = ?",
                (12345,)
        ) as cursor:
            preserved_row = await cursor.fetchone()

    assert "total_rating_count" in igdb_columns
    assert "mode" in lfg_columns
    assert "whitelisted_channels" in lfg_columns
    assert preserved_row == (12345,)

# -----------------------------------------------------------------------------
# 5. Tests for LFG Ping Anti-Spam State
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_claim_game_ping_cooldown_blocks_until_expiry(tmp_path, monkeypatch):
    test_db_path = tmp_path / "test_ping_cooldown.sqlite"
    monkeypatch.setattr("database.DB_PATH", str(test_db_path))
    await init_db()

    async with aiosqlite.connect(test_db_path) as db:
        await db.execute(
            "INSERT INTO igdb_games (id, name, updated_at) VALUES (?, ?, ?)",
            (1, "Portal 2", 1600000000)
        )
        await db.execute(
            "INSERT INTO guild_lfg_settings (guild_id, game_ping_cooldown_seconds) VALUES (?, ?)",
            (999, 600)
        )
        await db.commit()

    allowed, retry_after = await claim_game_ping_cooldown(999, 1, now=1000)
    assert allowed is True
    assert retry_after == 0

    allowed, retry_after = await claim_game_ping_cooldown(999, 1, now=1100)
    assert allowed is False
    assert retry_after == 500

    allowed, retry_after = await claim_game_ping_cooldown(999, 1, now=1600)
    assert allowed is True
    assert retry_after == 0

    async with aiosqlite.connect(test_db_path) as db:
        async with db.execute(
            "SELECT last_ping_at FROM guild_game_ping_state WHERE guild_id = ? AND game_id = ?",
            (999, 1)
        ) as cursor:
            row = await cursor.fetchone()

    assert row == (1600,)


@pytest.mark.asyncio
async def test_claim_game_ping_cooldown_disabled_does_not_create_state(
        tmp_path,
        monkeypatch
):
    test_db_path = tmp_path / "test_ping_cooldown_disabled.sqlite"
    monkeypatch.setattr("database.DB_PATH", str(test_db_path))

    await init_db()

    async with aiosqlite.connect(test_db_path) as db:
        await db.execute(
            "INSERT INTO igdb_games (id, name, updated_at) VALUES (?, ?, ?)",
            (1, "Portal 2", 1600000000)
        )

        # Explicitly disable the per-game cooldown for this guild.
        await db.execute(
            """
            INSERT INTO guild_lfg_settings (
                guild_id,
                game_ping_cooldown_seconds
            )
            VALUES (?, ?)
            """,
            (999, 0)
        )

        await db.commit()

    assert await claim_game_ping_cooldown(
        999,
        1,
        now=1000
    ) == (True, 0)

    assert await claim_game_ping_cooldown(
        999,
        1,
        now=1001
    ) == (True, 0)

    # Disabled cooldown should not create any persistent cooldown state.
    async with aiosqlite.connect(test_db_path) as db:
        async with db.execute(
                """
                SELECT COUNT(*)
                FROM guild_game_ping_state
                WHERE guild_id = ?
                  AND game_id = ?
                """,
                (999, 1)
        ) as cursor:
            count = (await cursor.fetchone())[0]

    assert count == 0

@pytest.mark.asyncio
async def test_claim_game_ping_cooldown_unconfigured_uses_30_second_default(
        tmp_path,
        monkeypatch
):
    test_db_path = tmp_path / "test_ping_cooldown_default.sqlite"
    monkeypatch.setattr("database.DB_PATH", str(test_db_path))

    await init_db()

    async with aiosqlite.connect(test_db_path) as db:
        await db.execute(
            "INSERT INTO igdb_games (id, name, updated_at) VALUES (?, ?, ?)",
            (1, "Portal 2", 1600000000)
        )
        await db.commit()

    # First ping succeeds.
    assert await claim_game_ping_cooldown(
        999,
        1,
        now=1000
    ) == (True, 0)

    # One second later, 29 seconds remain.
    assert await claim_game_ping_cooldown(
        999,
        1,
        now=1001
    ) == (False, 29)

    # At exactly 30 seconds, it is available again.
    assert await claim_game_ping_cooldown(
        999,
        1,
        now=1030
    ) == (True, 0)

@pytest.mark.asyncio
async def test_get_guild_ping_role_ids(tmp_path, monkeypatch):
    test_db_path = tmp_path / "test_ping_roles.sqlite"
    monkeypatch.setattr("database.DB_PATH", str(test_db_path))
    await init_db()

    async with aiosqlite.connect(test_db_path) as db:
        await db.executemany(
            "INSERT INTO guild_ping_roles (guild_id, role_id) VALUES (?, ?)",
            [(999, 30), (999, 10), (123, 20)]
        )
        await db.commit()

    assert await get_guild_ping_role_ids(999) == [10, 30]


@pytest.mark.asyncio
@patch("database.igdb.post")
async def test_backfill_failure_preserves_games_before_cleanup(mock_post, tmp_path, monkeypatch):
    """An exhausted API request must not purge unclassified cached games."""
    from igdb_client import IGDBError
    path = str(tmp_path / "failed_backfill.sqlite")
    monkeypatch.setattr("database.DB_PATH", path)
    monkeypatch.setenv("TWITCH_CLIENT_ID", "test_id")
    monkeypatch.setenv("TWITCH_SECRET", "test_secret")
    await init_db()
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "INSERT INTO igdb_games (id, name, updated_at) VALUES (?, ?, ?)",
            (101, "Unclassified Game", 1),
        )
        await db.commit()
    mock_post.side_effect = IGDBError("IGDB unavailable after bounded retries")
    await auto_migrate_multiplayer_flags()
    mock_post.assert_called_once()
    async with aiosqlite.connect(path) as db:
        async with db.execute("SELECT id FROM igdb_games") as cursor:
            assert await cursor.fetchall() == [(101,)]
