import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import discord
from discord.ext import commands

# Adjust import path as needed
from cogs.owner_cmds import OwnerCommands, autocomplete_admin_unsubscribe

@pytest.fixture
def mock_bot():
    bot = AsyncMock(spec=commands.Bot)
    bot.user = MagicMock()
    bot.user.id = 999
    return bot

@pytest.fixture
def owner_cog(mock_bot):
    return OwnerCommands(mock_bot)

@pytest.fixture
def mock_interaction():
    interaction = AsyncMock(spec=discord.Interaction)
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.id = 12345
    interaction.user.display_name = "BotOwner"
    interaction.user.mention = "<@12345>"

    interaction.response = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = AsyncMock()
    interaction.followup.send = AsyncMock()

    # FIX: Use MagicMock for the client so get_user is synchronous
    interaction.client = MagicMock()
    interaction.client.get_user.return_value = interaction.user

    return interaction

@pytest.fixture
def mock_ctx(mock_bot):
    ctx = AsyncMock(spec=commands.Context)
    ctx.bot = mock_bot
    ctx.author = MagicMock(spec=discord.Member)
    ctx.author.id = 12345
    ctx.send = AsyncMock()
    ctx.message = AsyncMock()
    ctx.message.delete = AsyncMock()
    ctx.channel = AsyncMock(spec=discord.TextChannel)
    return ctx

@pytest.fixture
def mock_db_connect():
    """Mocks aiosqlite connection and dual-purpose execute statements."""
    with patch('cogs.owner_cmds.aiosqlite.connect') as mock_connect:
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
        mock_connect.return_value.__aenter__.return_value = mock_db
        yield mock_db, mock_cursor

@pytest.fixture
def patch_owner_id():
    with patch('cogs.owner_cmds.get_bot_owner_id', return_value=12345) as p:
        yield p

@pytest.mark.asyncio
async def test_autocomplete_admin_unsubscribe(mock_interaction, mock_db_connect):
    mock_db, mock_cursor = mock_db_connect
    mock_cursor.fetchall.return_value = [("Elden Ring",)]

    # Namespace mocking
    mock_interaction.namespace = MagicMock()
    mock_interaction.namespace.user = MagicMock()
    mock_interaction.namespace.user.id = 555

    choices = await autocomplete_admin_unsubscribe(mock_interaction, "Elden")

    assert len(choices) == 1
    assert choices[0].name == "Elden Ring"
    assert choices[0].value == "Elden Ring"

    # Test no user selected fallback
    mock_interaction.namespace.user = None
    choices_no_user = await autocomplete_admin_unsubscribe(mock_interaction, "Elden")
    assert choices_no_user[0].value == "__NONE__"

@pytest.mark.asyncio
async def test_reload_cog_success(owner_cog, mock_ctx):
    await owner_cog.reload_cog(owner_cog, mock_ctx, cog_name="admin_cmds.py")

    # Should strip .py and prepend cogs.
    mock_ctx.bot.reload_extension.assert_awaited_once_with("cogs.admin_cmds")
    mock_ctx.send.assert_awaited_once()
    assert "Reloaded `cogs.admin_cmds` successfully" in mock_ctx.send.call_args[0][0]

@pytest.mark.asyncio
async def test_reload_cog_fallback_load(owner_cog, mock_ctx):
    mock_ctx.bot.reload_extension.side_effect = commands.ExtensionNotLoaded(name="cogs.test")

    await owner_cog.reload_cog(owner_cog, mock_ctx, cog_name="cogs.test")

    mock_ctx.bot.load_extension.assert_awaited_once_with("cogs.test")
    assert "Loaded `cogs.test` successfully" in mock_ctx.send.call_args[0][0]

@pytest.mark.asyncio
async def test_delete_bot_message_success(owner_cog, mock_ctx, patch_owner_id):
    mock_target_msg = AsyncMock()
    mock_target_msg.author.id = owner_cog.bot.user.id
    mock_ctx.channel.fetch_message.return_value = mock_target_msg

    await owner_cog.delete_bot_message(owner_cog, mock_ctx, message_id=111)

    mock_ctx.message.delete.assert_awaited_once()
    mock_target_msg.delete.assert_awaited_once()

@pytest.mark.asyncio
async def test_delete_bot_message_wrong_owner(owner_cog, mock_ctx, patch_owner_id):
    mock_ctx.author.id = 99999 # Not the owner ID

    await owner_cog.delete_bot_message(owner_cog, mock_ctx, message_id=111)

    # Execution should abort immediately
    mock_ctx.channel.fetch_message.assert_not_called()

@pytest.mark.asyncio
@patch('cogs.owner_cmds.resolve_to_base_game')
async def test_admin_subscribe_db_hit(mock_resolve, owner_cog, mock_interaction, mock_db_connect):
    mock_db, mock_cursor = mock_db_connect
    mock_cursor.fetchone.return_value = (10, "Dark Souls")
    mock_resolve.return_value = (10, "Dark Souls")

    target_user = AsyncMock(spec=discord.User)
    target_user.id = 555
    target_user.mention = "<@555>"

    await owner_cog.admin_subscribe.callback(owner_cog, mock_interaction, user=target_user, game="Dark Souls")

    mock_interaction.response.defer.assert_awaited_once()

    # Check that insert was called
    db_calls = mock_db.execute.call_args_list
    insert_call = next(call for call in db_calls if "INSERT OR IGNORE" in call.args[0])

    # FIX: Access parameters via .args[1]
    assert insert_call.args[1][0] == 555
    assert insert_call.args[1][1] == 10

    mock_interaction.followup.send.assert_awaited_once()
    assert "Subscribed <@555> to **Dark Souls**" in mock_interaction.followup.send.call_args[0][0]

@pytest.mark.asyncio
@patch('cogs.owner_cmds.fetch_and_cache_game_from_igdb')
@patch('cogs.owner_cmds.resolve_to_base_game')
async def test_admin_subscribe_igdb_fallback(mock_resolve, mock_fetch, owner_cog, mock_interaction, mock_db_connect):
    mock_db, mock_cursor = mock_db_connect
    mock_cursor.fetchone.return_value = None # Force IGDB fallback
    mock_fetch.return_value = (20, "Halo")
    mock_resolve.return_value = (20, "Halo")

    target_user = AsyncMock(spec=discord.User)
    target_user.id = 555

    await owner_cog.admin_subscribe.callback(owner_cog, mock_interaction, user=target_user, game="Halo")

    mock_fetch.assert_awaited_once_with("Halo")
    mock_interaction.followup.send.assert_awaited_once()
    assert "**Halo**" in mock_interaction.followup.send.call_args[0][0]

@pytest.mark.asyncio
@patch('cogs.owner_cmds.resolve_to_base_game')
async def test_admin_unsubscribe(mock_resolve, owner_cog, mock_interaction, mock_db_connect):
    mock_db, mock_cursor = mock_db_connect
    mock_cursor.fetchone.return_value = (30, "Zelda")
    mock_resolve.return_value = (30, "Zelda")

    target_user = AsyncMock()
    target_user.id = 777

    await owner_cog.admin_unsubscribe.callback(owner_cog, mock_interaction, user=target_user, game="Zelda")

    db_calls = mock_db.execute.call_args_list
    delete_call = next(call for call in db_calls if "DELETE FROM subscriptions" in call.args[0])

    # FIX: Access parameters via .args[1]
    assert delete_call.args[1] == (777, 30)

@pytest.mark.asyncio
@patch('cogs.owner_cmds.GenericConfirmationView')
async def test_admin_delete_all_user_data(mock_view_class, owner_cog, mock_interaction, mock_db_connect):
    mock_db, mock_cursor = mock_db_connect
    target_user = AsyncMock()
    target_user.id = 444

    await owner_cog.admin_delete_all_user_data.callback(owner_cog, mock_interaction, user=target_user)

    mock_interaction.response.send_message.assert_awaited_once()

    # Extract the created view and invoke its on_confirm callback directly to test DB deletion
    created_view = mock_view_class.call_args.kwargs
    on_confirm_callback = created_view['on_confirm']

    await on_confirm_callback(mock_interaction)

    # Verify both tables are deleted from
    db_calls = mock_db.execute.call_args_list
    assert any("DELETE FROM subscriptions" in str(call) for call in db_calls)
    assert any("DELETE FROM user_settings" in str(call) for call in db_calls)

@pytest.mark.asyncio
async def test_admin_overview(owner_cog, mock_interaction, mock_db_connect):
    mock_db, mock_cursor = mock_db_connect
    mock_cursor.fetchall.return_value = [
        (12345, "Elden Ring"),
        (12345, "Hollow Knight"),
        (67890, "Celeste")
    ]

    await owner_cog.admin_overview.callback(owner_cog, mock_interaction)

    mock_interaction.followup.send.assert_awaited_once()
    embed = mock_interaction.followup.send.call_args.kwargs['embed']

    assert "Total Subscribers:** 2" in embed.description
    assert "Total Subscriptions:** 3" in embed.description
    assert "Elden Ring" in embed.description
    assert "Celeste" in embed.description

@pytest.mark.asyncio
@patch('cogs.owner_cmds.os.path.exists')
@patch('cogs.owner_cmds.discord.File')
async def test_send_database_file(mock_file, mock_exists, owner_cog, mock_ctx, patch_owner_id):
    mock_exists.return_value = True
    mock_file.return_value = "mock_db_file"

    await owner_cog.send_database_file(owner_cog, mock_ctx)

    # Test DM sending
    mock_ctx.author.send.assert_awaited_once_with("📁 Here is the current copy of the SQLite database:", file="mock_db_file")
    mock_ctx.send.assert_awaited_once()
    assert "Check your Direct Messages!" in mock_ctx.send.call_args[0][0]