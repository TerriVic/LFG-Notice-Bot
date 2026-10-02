import pytest
import aiosqlite
from unittest.mock import AsyncMock, patch

# Import functions to test from database.py
from database import init_db, ensure_columns, auto_migrate_multiplayer_flags

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

    # Verify key tables exist
    assert "igdb_games" in tables
    assert "subscriptions" in tables
    assert "user_settings" in tables
    assert "guild_lfg_settings" in tables


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
@patch("aiohttp.ClientSession.post")
async def test_auto_migrate_multiplayer_flags_and_cleanup(mock_post, mock_getenv, tmp_path, monkeypatch):
    """Test IGDB multiplayer flag updating and pruning of single-player games."""
    test_db_path = tmp_path / "test_cleanup.sqlite"
    monkeypatch.setattr("database.DB_PATH", str(test_db_path))

    # 1. Setup mock IGDB API credentials
    mock_getenv.side_effect = lambda key, default=None: {
        "TWITCH_CLIENT_ID": "fake_client_id",
        "TWITCH_ACCESS_TOKEN": "fake_token",
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