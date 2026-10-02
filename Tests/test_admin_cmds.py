from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from discord import app_commands

# Adjust the import path to match your actual project structure
from cogs.admin_cmds import (
    AdminCommands
)


@pytest.fixture
def mock_bot():
    bot = AsyncMock()
    return bot

@pytest.fixture
def admin_cog(mock_bot):
    return AdminCommands(mock_bot)

@pytest.fixture
def mock_interaction():
    interaction = AsyncMock(spec=discord.Interaction)
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.id = 12345
    interaction.guild = MagicMock(spec=discord.Guild)
    interaction.guild.id = 67890
    interaction.guild.name = "Test Guild"
    interaction.channel = AsyncMock(spec=discord.TextChannel)
    interaction.channel.id = 111222

    # Setup response and followup as AsyncMocks
    interaction.response = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction

@pytest.fixture
def mock_db_connect():
    """Mocks the aiosqlite.connect context manager and its cursor."""
    with patch('cogs.admin_cmds.aiosqlite.connect') as mock_connect:
        mock_db = AsyncMock()
        mock_cursor = AsyncMock()

        # Helper class to simulate aiosqlite's execute() return value,
        # which can be both awaited directly AND used in an 'async with' block.
        class DualPurposeExecuteResult:
            def __await__(self):
                async def _awaitable():
                    return mock_cursor
                return _awaitable().__await__()

            async def __aenter__(self):
                return mock_cursor

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

        # Make db.execute a standard MagicMock that returns our dual-purpose object
        mock_db.execute = MagicMock(return_value=DualPurposeExecuteResult())

        mock_connect.return_value.__aenter__.return_value = mock_db
        yield mock_db, mock_cursor

@pytest.mark.asyncio
async def test_setup_top_games_new_tracker(admin_cog, mock_interaction, mock_db_connect):
    mock_db, mock_cursor = mock_db_connect

    # Simulate NO existing tracker found
    mock_cursor.fetchone.return_value = None

    # Simulate sending the embed message
    mock_sent_message = AsyncMock()
    mock_sent_message.id = 99999
    mock_interaction.channel.send.return_value = mock_sent_message

    # Patch the background task refresh
    with patch('cogs.admin_cmds.refresh_live_top_games', new_callable=AsyncMock) as mock_refresh:
        await admin_cog.setup_top_games.callback(admin_cog, mock_interaction, limit=10, interval=1.0)

        mock_interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        mock_interaction.channel.send.assert_awaited_once()

        # Verify database insert
        mock_db.execute.assert_called()
        call_args = mock_db.execute.call_args_list[-1][0]
        assert "INSERT INTO live_top_games" in call_args[0]
        assert call_args[1][0] == 99999  # message_id

        mock_refresh.assert_awaited_once_with(admin_cog.bot, force=True)
        mock_interaction.followup.send.assert_awaited_once()
        assert "✅ Live top games tracker posted!" in mock_interaction.followup.send.call_args[0][0]

@pytest.mark.asyncio
async def test_remove_top_games(admin_cog, mock_interaction, mock_db_connect):
    mock_db, mock_cursor = mock_db_connect
    mock_cursor.fetchone.return_value = (111222,) # Returns a channel_id

    mock_channel = AsyncMock()
    mock_interaction.guild.get_channel.return_value = mock_channel
    mock_msg = AsyncMock()
    mock_channel.fetch_message.return_value = mock_msg

    await admin_cog.remove_top_games.callback(admin_cog, mock_interaction, message="99999")

    mock_interaction.response.defer.assert_awaited_once()
    mock_msg.delete.assert_awaited_once()

    # Verify DB deletion
    mock_db.execute.assert_called_with("DELETE FROM live_top_games WHERE message_id = ?", (99999,))
    mock_interaction.followup.send.assert_awaited_once()
    assert "removed successfully" in mock_interaction.followup.send.call_args[0][0]

@pytest.mark.asyncio
async def test_server_overview_with_subscriptions(admin_cog, mock_interaction, mock_db_connect):
    mock_db, mock_cursor = mock_db_connect

    # Simulate DB rows: user_id, game_name
    mock_cursor.fetchall.return_value = [
        (123, "Elden Ring"),
        (123, "Hollow Knight"),
        (456, "Zelda")
    ]

    # Mock guild member fetch: user 123 is in server, user 456 is NOT in server
    mock_member_123 = MagicMock(spec=discord.Member)
    mock_member_123.display_name = "PlayerOne"

    def mock_get_member(user_id):
        return mock_member_123 if user_id == 123 else None

    mock_interaction.guild.get_member.side_effect = mock_get_member

    await admin_cog.server_overview.callback(admin_cog, mock_interaction)

    mock_interaction.followup.send.assert_awaited_once()
    embed = mock_interaction.followup.send.call_args.kwargs['embed']

    # Assert embed format correctly filters only user 123
    assert embed.title == "🏰 Subscriptions Overview for **Test Guild**"
    assert "Total Server Subscribers:** 1" in embed.description
    assert "Total Subscriptions:** 2" in embed.description
    assert "PlayerOne" in embed.description
    assert "Elden Ring" in embed.description
    assert "Zelda" not in embed.description # User 456 is not in the guild

@pytest.mark.asyncio
async def test_set_lfg_channels_whitelist(admin_cog, mock_interaction, mock_db_connect):
    mock_db, mock_cursor = mock_db_connect
    mock_cursor.fetchone.return_value = ("111,222",) # existing channels

    mode_choice = app_commands.Choice(name="Whitelist Specific Channels", value="whitelist")
    new_channel = "333"

    await admin_cog.set_lfg_channels.callback(
        admin_cog, mock_interaction, mode=mode_choice, channel=new_channel
    )

    # Check upsert logic
    mock_db.execute.assert_called()
    db_args = mock_db.execute.call_args[0]
    assert "INSERT INTO guild_lfg_settings" in db_args[0]

    # Verify 333 was added to existing 111,222
    inserted_channels = db_args[1][2]
    assert "111" in inserted_channels
    assert "222" in inserted_channels
    assert "333" in inserted_channels
    assert db_args[1][1] == "whitelist"

    mock_interaction.followup.send.assert_awaited_once()
    assert "Allowed channels:" in mock_interaction.followup.send.call_args[0][0]

@pytest.mark.asyncio
async def test_remove_lfg_channels_none_mode(admin_cog, mock_interaction, mock_db_connect):
    mock_db, mock_cursor = mock_db_connect
    mock_cursor.fetchone.return_value = ("whitelist", "111,222")

    mode_choice = app_commands.Choice(name="Disable/Blacklist All Channels", value="none")

    await admin_cog.remove_lfg_channels.callback(
        admin_cog, mock_interaction, mode=mode_choice, channel=None
    )

    mock_db.execute.assert_called()
    db_args = mock_db.execute.call_args[0]
    assert "VALUES (?, 'none', ?)" in db_args[0]

    mock_interaction.followup.send.assert_awaited_once()
    assert "disabled across all channels" in mock_interaction.followup.send.call_args[0][0]