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
async def test_setup_top_games_existing_tracker_prompt_and_confirm(admin_cog, mock_interaction, mock_db_connect):
    """Test setup_top_games detects an existing tracker, prompts confirmation via view, and creates a new one upon confirmation."""
    mock_db, mock_cursor = mock_db_connect

    # Simulate an existing tracker record in the database (channel_id=111222, created_at=1700000000)
    mock_cursor.fetchone.return_value = (111222, 1700000000)

    mock_sent_message = AsyncMock()
    mock_sent_message.id = 88888
    mock_interaction.channel.send.return_value = mock_sent_message

    with patch("cogs.admin_cmds.refresh_live_top_games", new_callable=AsyncMock) as mock_refresh, \
            patch("cogs.admin_cmds.format_interval", return_value="1 hour"):

        await admin_cog.setup_top_games.callback(admin_cog, mock_interaction, limit=10, interval=1.0)

        # 1. Verify deferred response and that a confirmation view was sent
        mock_interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        mock_interaction.followup.send.assert_awaited_once()

        call_args = mock_interaction.followup.send.call_args
        message_content = call_args.args[0] if call_args.args else call_args.kwargs.get('content', '')
        assert "A live top games tracker already exists" in message_content

        # Extract the confirmation view sent with the message
        view = call_args.kwargs.get('view') or (call_args.args[1] if len(call_args.args) > 1 else None)
        assert view is not None

        # 2. Simulate user clicking the confirm button on the view
        # GenericConfirmationView uses a style for confirm (often danger or success depending on setup)
        confirm_btn = next(
            (item for item in view.children if getattr(item, 'style', None) in (discord.ButtonStyle.green, discord.ButtonStyle.success, discord.ButtonStyle.danger)),
            view.children[0]
        )

        # Mock button interaction with a properly awaited response.edit_message
        btn_interaction = AsyncMock(spec=discord.Interaction)
        btn_interaction.user.id = mock_interaction.user.id
        btn_interaction.response = AsyncMock()
        btn_interaction.response.edit_message = AsyncMock()

        # Run the button's callback
        await confirm_btn.callback(btn_interaction)

        # 3. Verify that the new tracker was successfully created after confirmation
        mock_interaction.channel.send.assert_awaited()
        mock_refresh.assert_awaited_once_with(admin_cog.bot, force=True)

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

# -----------------------------------------------------------------------------
# Edit Top Games Command Tests
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_edit_top_games_none_message(admin_cog, mock_interaction):
    """Test edit_top_games handles '__NONE__' message selection gracefully."""
    await admin_cog.edit_top_games.callback(admin_cog, mock_interaction, message="__NONE__")

    mock_interaction.response.send_message.assert_awaited_once_with(
        "No live tracker message selected.", ephemeral=True
    )


@pytest.mark.asyncio
async def test_edit_top_games_invalid_message(admin_cog, mock_interaction):
    """Test edit_top_games handles non-integer message ID selections gracefully."""
    await admin_cog.edit_top_games.callback(admin_cog, mock_interaction, message="not_an_int")

    mock_interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    mock_interaction.followup.send.assert_awaited_once_with(
        "Invalid message selection.", ephemeral=True
    )


@pytest.mark.asyncio
async def test_edit_top_games_force_refresh(admin_cog, mock_interaction):
    """Test edit_top_games performs a force refresh when limit and interval are left empty."""
    with patch("cogs.admin_cmds.refresh_live_top_games", new_callable=AsyncMock) as mock_refresh:
        await admin_cog.edit_top_games.callback(
            admin_cog, mock_interaction, message="99999", limit=None, interval=None
        )

        mock_interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        mock_refresh.assert_awaited_once_with(admin_cog.bot, force=True, target_message_id=99999)
        mock_interaction.followup.send.assert_awaited_once()
        assert "force-refreshed" in mock_interaction.followup.send.call_args[0][0]


@pytest.mark.asyncio
async def test_edit_top_games_not_found_in_db(admin_cog, mock_interaction, mock_db_connect):
    """Test edit_top_games handles message IDs not found in the database."""
    _, mock_cursor = mock_db_connect
    mock_cursor.fetchone.return_value = None  # Tracker not found

    await admin_cog.edit_top_games.callback(
        admin_cog, mock_interaction, message="99999", limit=15
    )

    mock_interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    mock_interaction.followup.send.assert_awaited_once_with(
        "Tracker message not found in database.", ephemeral=True
    )


@pytest.mark.asyncio
async def test_edit_top_games_success(admin_cog, mock_interaction, mock_db_connect):
    """Test edit_top_games successfully updates settings, commits to DB, and refreshes the tracker."""
    mock_db, mock_cursor = mock_db_connect
    mock_cursor.fetchone.return_value = (10, 1.0)  # current_limit, current_interval

    with patch("cogs.admin_cmds.refresh_live_top_games", new_callable=AsyncMock) as mock_refresh, \
            patch("cogs.admin_cmds.format_interval", return_value="1 hour") as mock_format:

        await admin_cog.edit_top_games.callback(
            admin_cog, mock_interaction, message="99999", limit=20, interval=2.0
        )

        mock_interaction.response.defer.assert_awaited_once_with(ephemeral=True)

        # Verify database update was executed
        mock_db.execute.assert_called()
        update_call_found = any(
            "UPDATE live_top_games SET" in str(call) for call in mock_db.execute.call_args_list
        )
        assert update_call_found

        mock_refresh.assert_awaited_once_with(admin_cog.bot, force=True, target_message_id=99999)
        mock_interaction.followup.send.assert_awaited_once()
        assert "✅ Tracker message updated successfully!" in mock_interaction.followup.send.call_args[0][0]

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

# -----------------------------------------------------------------------------
# Remove LFG Channels Command Tests
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_remove_lfg_channels_no_guild(admin_cog, mock_interaction):
    """Test remove_lfg_channels rejects execution outside a guild (DM context)."""
    mock_interaction.guild = None
    mode_choice = app_commands.Choice(name="Disable/Blacklist All Channels", value="none")

    await admin_cog.remove_lfg_channels.callback(admin_cog, mock_interaction, mode=mode_choice)

    mock_interaction.response.send_message.assert_awaited_once_with(
        "This command must be run inside a server.", ephemeral=True
    )


@pytest.mark.asyncio
async def test_remove_lfg_channels_disable_none(admin_cog, mock_interaction, mock_db_connect):
    """Test remove_lfg_channels with 'none' mode successfully sets server-wide block."""
    mock_db, mock_cursor = mock_db_connect
    mock_cursor.fetchone.return_value = ("whitelist", "111,222")

    mode_choice = app_commands.Choice(name="Disable/Blacklist All Channels", value="none")

    await admin_cog.remove_lfg_channels.callback(admin_cog, mock_interaction, mode=mode_choice)

    mock_interaction.response.defer.assert_awaited_once_with(ephemeral=True)

    # Verify upsert / update to 'none' mode
    mock_db.execute.assert_called()
    db_args = mock_db.execute.call_args[0]
    assert "INSERT INTO guild_lfg_settings" in db_args[0] or "UPDATE" in db_args[0]

    mock_interaction.followup.send.assert_awaited_once()
    assert "disabled across all channels" in mock_interaction.followup.send.call_args[0][0]


@pytest.mark.asyncio
async def test_remove_lfg_channels_remove_specific_missing(admin_cog, mock_interaction, mock_db_connect):
    """Test remove_lfg_channels handles missing or '__NONE__' channel choices."""
    _, mock_cursor = mock_db_connect
    mock_cursor.fetchone.return_value = ("whitelist", "111,222")

    mode_choice = app_commands.Choice(name="Disable/Remove Specific Channel", value="remove_specific")

    # Case 1: Channel is None
    await admin_cog.remove_lfg_channels.callback(admin_cog, mock_interaction, mode=mode_choice, channel=None)
    mock_interaction.followup.send.assert_awaited_with("❌ Please select a valid whitelisted channel to remove.", ephemeral=True)

    # Case 2: Channel is "__NONE__"
    await admin_cog.remove_lfg_channels.callback(admin_cog, mock_interaction, mode=mode_choice, channel="__NONE__")
    assert "Please select a valid whitelisted channel" in mock_interaction.followup.send.call_args[0][0]


@pytest.mark.asyncio
async def test_remove_lfg_channels_remove_specific_not_whitelisted(admin_cog, mock_interaction, mock_db_connect):
    """Test remove_lfg_channels warns if the selected channel is not currently whitelisted."""
    _, mock_cursor = mock_db_connect
    mock_cursor.fetchone.return_value = ("whitelist", "111,222")

    mode_choice = app_commands.Choice(name="Disable/Remove Specific Channel", value="remove_specific")

    await admin_cog.remove_lfg_channels.callback(admin_cog, mock_interaction, mode=mode_choice, channel="999")

    mock_interaction.followup.send.assert_awaited_once_with(
        "⚠️ The selected channel is not currently in the whitelist.", ephemeral=True
    )


@pytest.mark.asyncio
async def test_remove_lfg_channels_remove_specific_success_with_remaining(admin_cog, mock_interaction, mock_db_connect):
    """Test remove_lfg_channels successfully removes channel and reports remaining ones."""
    mock_db, mock_cursor = mock_db_connect
    mock_cursor.fetchone.return_value = ("whitelist", "111,222")

    mode_choice = app_commands.Choice(name="Disable/Remove Specific Channel", value="remove_specific")

    await admin_cog.remove_lfg_channels.callback(admin_cog, mock_interaction, mode=mode_choice, channel="111")

    mock_interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    mock_db.execute.assert_called_with(
        "UPDATE guild_lfg_settings SET whitelisted_channels = ? WHERE guild_id = ?",
        ("222", 67890)
    )

    mock_interaction.followup.send.assert_awaited_once()
    msg = mock_interaction.followup.send.call_args[0][0]
    assert "Removed <#111> from LFG whitelist" in msg
    assert "Remaining allowed channels: <#222>" in msg


@pytest.mark.asyncio
async def test_remove_lfg_channels_remove_specific_success_empty_whitelist(admin_cog, mock_interaction, mock_db_connect):
    """Test remove_lfg_channels updates database and notifies when whitelist becomes empty."""
    _, mock_cursor = mock_db_connect
    mock_cursor.fetchone.return_value = ("whitelist", "111")

    mode_choice = app_commands.Choice(name="Disable/Remove Specific Channel", value="remove_specific")

    await admin_cog.remove_lfg_channels.callback(admin_cog, mock_interaction, mode=mode_choice, channel="111")

    mock_interaction.followup.send.assert_awaited_once()
    msg = mock_interaction.followup.send.call_args[0][0]
    assert "Removed <#111> from LFG whitelist" in msg
    assert "The whitelist is now empty." in msg


# -----------------------------------------------------------------------------
# View LFG Channels Command Tests
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_view_lfg_channels_no_guild(admin_cog, mock_interaction):
    """Test view_lfg_channels rejects execution outside a guild (DM context)."""
    mock_interaction.guild = None
    await admin_cog.view_lfg_channels.callback(admin_cog, mock_interaction)

    mock_interaction.response.send_message.assert_awaited_once_with(
        "This command must be run inside a server.", ephemeral=True
    )


@pytest.mark.asyncio
async def test_view_lfg_channels_unconfigured_default(admin_cog, mock_interaction, mock_db_connect):
    """Test view_lfg_channels reports default settings when no row exists in the database."""
    _, mock_cursor = mock_db_connect
    mock_cursor.fetchone.return_value = None

    await admin_cog.view_lfg_channels.callback(admin_cog, mock_interaction)

    mock_interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    mock_interaction.followup.send.assert_awaited_once_with(
        "⚙️ LFG channel settings are currently set to **default (Allow All Channels)**.", ephemeral=True
    )


@pytest.mark.asyncio
async def test_view_lfg_channels_embed_display(admin_cog, mock_interaction, mock_db_connect):
    """Test view_lfg_channels builds and sends the proper status embed for configured modes."""
    _, mock_cursor = mock_db_connect
    mock_cursor.fetchone.return_value = ("whitelist", "111,222")

    await admin_cog.view_lfg_channels.callback(admin_cog, mock_interaction)

    mock_interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    mock_interaction.followup.send.assert_awaited_once()

    embed = mock_interaction.followup.send.call_args.kwargs['embed']
    assert isinstance(embed, discord.Embed)
    assert embed.title == "⚙️ LFG Channel Settings for Test Guild"

    # Check fields
    field_names = [f.name for f in embed.fields]
    assert "Current Mode" in field_names
    assert "Whitelisted Channels" in field_names

# -----------------------------------------------------------------------------
# Owner / Moderator Permission Check Tests
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_is_owner_or_moderator_bot_owner():
    """Test is_owner_or_moderator permits bot owner regardless of server permissions."""
    from cogs.admin_cmds import is_owner_or_moderator

    check = is_owner_or_moderator()
    predicate = check.predicate

    mock_interaction = MagicMock(spec=discord.Interaction)
    mock_interaction.user.id = 99999
    mock_interaction.permissions = None

    with patch("cogs.admin_cmds.get_bot_owner_id", return_value=99999):
        result = await predicate(mock_interaction)
        assert result is True


@pytest.mark.asyncio
async def test_is_owner_or_moderator_server_mod():
    """Test is_owner_or_moderator permits users with manage_messages permission."""
    from cogs.admin_cmds import is_owner_or_moderator

    check = is_owner_or_moderator()
    predicate = check.predicate

    mock_interaction = MagicMock(spec=discord.Interaction)
    mock_interaction.user.id = 12345
    mock_interaction.permissions = MagicMock()
    mock_interaction.permissions.manage_messages = True

    with patch("cogs.admin_cmds.get_bot_owner_id", return_value=99999):
        result = await predicate(mock_interaction)
        assert result is True


@pytest.mark.asyncio
async def test_is_owner_or_moderator_unauthorized():
    """Test is_owner_or_moderator denies regular users without permissions or owner status."""
    from cogs.admin_cmds import is_owner_or_moderator

    check = is_owner_or_moderator()
    predicate = check.predicate

    mock_interaction = MagicMock(spec=discord.Interaction)
    mock_interaction.user.id = 12345
    mock_interaction.permissions = MagicMock()
    mock_interaction.permissions.manage_messages = False

    with patch("cogs.admin_cmds.get_bot_owner_id", return_value=99999):
        result = await predicate(mock_interaction)
        assert result is False


# -----------------------------------------------------------------------------
# Live Messages Autocomplete Tests
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_autocomplete_live_messages_no_guild():
    """Test autocomplete_live_messages returns empty list in DM context (no guild)."""
    from cogs.admin_cmds import autocomplete_live_messages

    mock_interaction = MagicMock(spec=discord.Interaction)
    mock_interaction.guild = None

    choices = await autocomplete_live_messages(mock_interaction, "")
    assert choices == []


@pytest.mark.asyncio
async def test_autocomplete_live_messages_empty_db(mock_db_connect):
    """Test autocomplete_live_messages returns fallback choice when no active messages exist."""
    from cogs.admin_cmds import autocomplete_live_messages
    _, mock_cursor = mock_db_connect
    mock_cursor.fetchall.return_value = []

    mock_interaction = MagicMock(spec=discord.Interaction)
    mock_interaction.guild = MagicMock()
    mock_interaction.guild.id = 67890

    choices = await autocomplete_live_messages(mock_interaction, "")
    assert len(choices) == 1
    assert choices[0].name == "No active live top game messages found"
    assert choices[0].value == "__NONE__"


@pytest.mark.asyncio
async def test_autocomplete_live_messages_with_rows(mock_db_connect):
    """Test autocomplete_live_messages formats labels, handles missing channels, and filters correctly."""
    from cogs.admin_cmds import autocomplete_live_messages
    _, mock_cursor = mock_db_connect

    # Rows: message_id, channel_id, entry_limit, update_interval, created_at
    mock_cursor.fetchall.return_value = [
        (11111, 22222, 10, 2.5, 1600000000),
        (33333, 44444, 5, 1.0, 1600003600)
    ]

    mock_channel = MagicMock()
    mock_channel.name = "general"

    mock_guild = MagicMock()
    mock_guild.id = 67890
    # Channel 22222 exists, channel 44444 does not (returns None to test fallback format)
    mock_guild.get_channel.side_effect = lambda cid: mock_channel if cid == 22222 else None

    mock_interaction = MagicMock(spec=discord.Interaction)
    mock_interaction.guild = mock_guild

    # Test empty query (returns all formatted choices)
    choices = await autocomplete_live_messages(mock_interaction, "")
    assert len(choices) == 2
    assert "11111" in choices[0].value
    assert "#general" in choices[0].name
    assert "Channel 44444" in choices[1].name  # Fallback formatting when channel is missing

    # Test filtered query by message ID string
    filtered_choices = await autocomplete_live_messages(mock_interaction, "33333")
    assert len(filtered_choices) == 1
    assert filtered_choices[0].value == "33333"

# -----------------------------------------------------------------------------
# Force Update Top Games Command Tests
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_force_update_top_games_none_message(admin_cog, mock_interaction):
    """Test force_update_top_games handles '__NONE__' message selection gracefully."""
    await admin_cog.force_update_top_games.callback(admin_cog, mock_interaction, message="__NONE__")

    mock_interaction.response.send_message.assert_awaited_once_with(
        "No live tracker message selected.", ephemeral=True
    )


@pytest.mark.asyncio
async def test_force_update_top_games_invalid_message(admin_cog, mock_interaction):
    """Test force_update_top_games handles non-integer message ID selections gracefully."""
    await admin_cog.force_update_top_games.callback(admin_cog, mock_interaction, message="not_an_int")

    mock_interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    mock_interaction.followup.send.assert_awaited_once_with(
        "Invalid message selection.", ephemeral=True
    )


@pytest.mark.asyncio
async def test_force_update_top_games_not_found_in_db(admin_cog, mock_interaction, mock_db_connect):
    """Test force_update_top_games handles message IDs not found in the database."""
    _, mock_cursor = mock_db_connect
    mock_cursor.fetchone.return_value = None  # Tracker not found

    await admin_cog.force_update_top_games.callback(admin_cog, mock_interaction, message="99999")

    mock_interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    mock_interaction.followup.send.assert_awaited_once_with(
        "Tracker message not found in database.", ephemeral=True
    )


@pytest.mark.asyncio
async def test_force_update_top_games_success(admin_cog, mock_interaction, mock_db_connect):
    """Test force_update_top_games successfully triggers a refresh and confirms to the user."""
    _, mock_cursor = mock_db_connect
    mock_cursor.fetchone.return_value = (99999,)  # Tracker exists

    with patch("cogs.admin_cmds.refresh_live_top_games", new_callable=AsyncMock) as mock_refresh:
        await admin_cog.force_update_top_games.callback(admin_cog, mock_interaction, message="99999")

        mock_interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        mock_refresh.assert_awaited_once_with(admin_cog.bot, force=True, target_message_id=99999)

        mock_interaction.followup.send.assert_awaited_once()
        assert "Live top games tracker refreshed successfully!" in mock_interaction.followup.send.call_args[0][0]