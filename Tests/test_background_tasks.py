import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import discord
from discord.ext import commands
import time
import os

# Adjust import path as needed
from cogs.background_tasks import refresh_live_top_games, BackgroundTasks

@pytest.fixture
def mock_bot():
    bot = AsyncMock(spec=commands.Bot)
    return bot

@pytest.fixture
def bg_cog(mock_bot):
    return BackgroundTasks(mock_bot)

@pytest.fixture
def mock_db_connect():
    """Mocks aiosqlite connection and dual-purpose execute statements."""
    with patch('cogs.background_tasks.aiosqlite.connect') as mock_connect:
        mock_db = AsyncMock()
        mock_cursor = AsyncMock()

        class DualPurposeExecuteResult:
            def __await__(self):
                async def _awaitable():
                    return mock_cursor
                return _awaitable().__await__()

            async def __aenter__(self):
                return mock_cursor

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

        mock_db.execute = MagicMock(return_value=DualPurposeExecuteResult())
        # Add executemany for the IGDB sync
        mock_db.executemany = AsyncMock()
        mock_db.commit = AsyncMock()

        mock_connect.return_value.__aenter__.return_value = mock_db
        yield mock_db, mock_cursor

@pytest.fixture
def mock_igdb_client():
    """Mock the shared transport; authentication is covered by client tests."""
    with patch('cogs.background_tasks.igdb.post') as mock_post:
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=[])
        mock_context = AsyncMock()
        mock_context.__aenter__.return_value = mock_response
        mock_post.return_value = mock_context
        yield mock_post, mock_response

@pytest.mark.asyncio
async def test_refresh_live_top_games_success(mock_bot, mock_db_connect):
    mock_db, mock_cursor = mock_db_connect

    # Setup DB Returns
    # 1. Tracked boards: message_id, guild_id, channel_id, entry_limit, update_interval, last_updated
    mock_cursor.fetchall.side_effect = [
        [(100, 200, 300, 10, 1, 0)],
        # 2. Subscriptions: user_id, game_name
        [(1, "Elden Ring"), (2, "Elden Ring"), (3, "Hades")]
    ]

    # Setup Discord Object Hierarchy
    mock_guild = MagicMock(spec=discord.Guild)
    mock_guild.name = "Test Guild"
    mock_bot.get_guild.return_value = mock_guild

    mock_channel = AsyncMock(spec=discord.TextChannel)
    mock_guild.get_channel.return_value = mock_channel

    mock_message = AsyncMock(spec=discord.Message)
    mock_channel.fetch_message.return_value = mock_message

    # Mock members existing in guild
    mock_guild.get_member.return_value = True

    await refresh_live_top_games(mock_bot, force=True)

    # Ensure message is updated
    mock_message.edit.assert_awaited_once()

    # Verify embed contents
    embed = mock_message.edit.call_args.kwargs['embed']
    assert "Live Top 10 Games in Test Guild" in embed.title
    assert "Elden Ring" in embed.description
    assert "2 subscribers" in embed.description

    # Verify DB update timestamp
    db_calls = mock_db.execute.call_args_list
    update_call = next(call for call in db_calls if "UPDATE live_top_games" in call.args[0])
    assert update_call.args[1][1] == 100 # message_id parameter

@pytest.mark.asyncio
async def test_refresh_live_top_games_deleted_message(mock_bot, mock_db_connect):
    mock_db, mock_cursor = mock_db_connect
    mock_cursor.fetchall.side_effect = [
        [(100, 200, 300, 10, 1, 0)],
        []
    ]

    mock_guild = MagicMock()
    mock_bot.get_guild.return_value = mock_guild
    mock_channel = AsyncMock()
    mock_guild.get_channel.return_value = mock_channel

    # Simulate message deleted
    mock_channel.fetch_message.side_effect = discord.NotFound(response=MagicMock(), message="Not Found")

    await refresh_live_top_games(mock_bot, force=True)

    # Board should be deleted from DB
    db_calls = mock_db.execute.call_args_list
    delete_call = next(call for call in db_calls if "DELETE FROM live_top_games" in call.args[0])
    assert delete_call.args[1] == (100,)
    mock_db.commit.assert_awaited()

@pytest.mark.asyncio
async def test_refresh_live_top_games_skips_if_recent(mock_bot, mock_db_connect):
    mock_db, mock_cursor = mock_db_connect

    # Set last_updated to right now (interval is 1 hour, so it should skip)
    now = int(time.time())
    mock_cursor.fetchall.side_effect = [
        [(100, 200, 300, 10, 1, now)],
        []
    ]

    await refresh_live_top_games(mock_bot, force=False)

    # Ensure bot.get_guild is never called, meaning it skipped processing
    mock_bot.get_guild.assert_not_called()

@pytest.mark.asyncio
@patch.dict(os.environ, clear=True)
async def test_igdb_sync_missing_creds(bg_cog):
    # Testing the actual coroutine attached to the task loop
    await bg_cog.update_igdb_catalog.coro(bg_cog)
    # If no exception is raised, and it exits cleanly, the missing cred check worked.

@pytest.mark.asyncio
@patch.dict(os.environ, {"TWITCH_CLIENT_ID": "test_id", "TWITCH_SECRET": "test_token"})
@patch('cogs.background_tasks.asyncio.sleep')  # Prevent tests from actually sleeping
async def test_igdb_sync_success(mock_sleep, bg_cog, mock_db_connect, mock_igdb_client):
    mock_db, mock_cursor = mock_db_connect
    mock_post, mock_response = mock_igdb_client

    # Mock current max ID
    mock_cursor.fetchone.return_value = (0,)

    # Mock one page of results, then an empty page to break the loop
    mock_response.json.side_effect = [
        [
            {"id": 1, "name": "Game One", "category": 0},
            {"id": 2, "name": "Game Two", "category": 0, "parent_game": {"id": 99}}, # Should skip due to parent
        ],
        [] # Stop pagination
    ]

    await bg_cog.update_igdb_catalog.coro(bg_cog)

    # Ensure executemany was called for upserting
    mock_db.executemany.assert_awaited_once()

    # Check that only Game One was inserted (Game Two had a parent game)
    query, params = mock_db.executemany.call_args.args
    assert "INSERT INTO igdb_games" in query
    assert len(params) == 1
    assert params[0][1] == "Game One"

    mock_db.commit.assert_awaited()

def test_cog_load_and_unload(bg_cog):
    # Mock the loop properties
    bg_cog.update_igdb_catalog = MagicMock()
    bg_cog.update_live_top_games_loop = MagicMock()

    bg_cog.update_igdb_catalog.is_running.return_value = False
    bg_cog.update_live_top_games_loop.is_running.return_value = False

    # Test load
    bg_cog.cog_load()
    bg_cog.update_igdb_catalog.start.assert_called_once()
    bg_cog.update_live_top_games_loop.start.assert_called_once()

    # Test unload
    bg_cog.cog_unload()
    bg_cog.update_igdb_catalog.cancel.assert_called_once()
    bg_cog.update_live_top_games_loop.cancel.assert_called_once()

@pytest.mark.asyncio
async def test_igdb_sync_failure_keeps_scheduled_loop_alive(bg_cog):
    """Exhausted retries are logged without escaping the weekly loop."""
    from igdb_client import IGDBError
    with patch.object(bg_cog, "_sync_igdb_catalog", new_callable=AsyncMock) as sync:
        sync.side_effect = IGDBError("IGDB unavailable after bounded retries")
        await bg_cog.update_igdb_catalog.coro(bg_cog)
        sync.assert_awaited_once()
