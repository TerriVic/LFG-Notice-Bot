from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import discord
import pytest

from cogs.admin_cmds import AdminCommands, ServerSetupView


@pytest.fixture
async def setup_context(tmp_path, monkeypatch):
    path = str(tmp_path / 'setup.sqlite')
    monkeypatch.setattr('cogs.admin_cmds.DB_PATH', path)
    monkeypatch.setattr('cogs.admin_cmds.get_bot_owner_id', lambda: 999)
    async with aiosqlite.connect(path) as db:
        await db.executescript('''
            CREATE TABLE guild_lfg_settings(guild_id INTEGER PRIMARY KEY, mode TEXT,
                whitelisted_channels TEXT, game_ping_cooldown_seconds INTEGER);
            CREATE TABLE guild_ping_roles(guild_id INTEGER, role_id INTEGER);
            CREATE TABLE guild_game_ping_state(guild_id INTEGER, game_id INTEGER);
            CREATE TABLE quickstart_messages(message_id INTEGER, guild_id INTEGER,
                channel_id INTEGER, created_at INTEGER);
            INSERT INTO guild_lfg_settings VALUES (1, 'all', '', 30);
            INSERT INTO guild_ping_roles VALUES (1, 8);
        ''')
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 10
    channel.permissions_for.return_value = NS(view_channel=True, send_messages=True, embed_links=True)
    channel.send = AsyncMock(return_value=NS(id=77, delete=AsyncMock()))
    guild = NS(id=1, me=NS(), get_channel=lambda cid: channel if cid == 10 else None,
               get_role=lambda rid: NS(id=rid))
    origin = NS(user=NS(id=2), guild=guild, edit_original_response=AsyncMock())
    interaction = NS(user=NS(id=2), guild_id=1, permissions=NS(manage_messages=True),
        response=NS(defer=AsyncMock(), edit_message=AsyncMock(), send_message=AsyncMock()),
        followup=NS(send=AsyncMock()), edit_original_response=AsyncMock())
    return path, origin, interaction, channel


@pytest.mark.asyncio
async def test_setup_command_opens_private_draft(setup_context):
    _, origin, interaction, _ = setup_context
    interaction.guild = origin.guild
    cog = AdminCommands(MagicMock())
    await cog.server_setup.callback(cog, interaction)
    assert interaction.response.send_message.call_args.kwargs['ephemeral'] is True
    assert isinstance(interaction.response.send_message.call_args.kwargs['view'], ServerSetupView)


@pytest.mark.asyncio
async def test_setup_requires_initiator_and_current_permissions(setup_context):
    _, origin, interaction, _ = setup_context
    view = ServerSetupView(None, origin)
    assert await view.interaction_check(interaction)
    interaction.user.id = 3
    assert not await view.interaction_check(interaction)
    interaction.user.id = 2
    interaction.permissions.manage_messages = False
    assert not await view.interaction_check(interaction)


@pytest.mark.asyncio
async def test_setup_cancel_and_timeout_leave_settings(setup_context):
    path, origin, interaction, _ = setup_context
    view = ServerSetupView(None, origin)
    await view.cancel(interaction)
    await view.on_timeout()
    async with aiosqlite.connect(path) as db:
        assert await (await db.execute('SELECT mode FROM guild_lfg_settings')).fetchone() == ('all',)
    assert view.is_finished()
    origin.edit_original_response.assert_awaited_once()


@pytest.mark.asyncio
async def test_setup_save_replaces_roles_and_channels(setup_context):
    path, origin, interaction, _ = setup_context
    view = ServerSetupView(None, origin)
    view.channel_ids = [10]
    view.role_ids = [7]
    view.step = 4
    await view.save(interaction)
    async with aiosqlite.connect(path) as db:
        assert await (await db.execute('SELECT * FROM guild_lfg_settings')).fetchone() == (1, 'whitelist', '10', 600)
        assert await (await db.execute('SELECT * FROM guild_ping_roles')).fetchall() == [(1, 7)]
    assert view.is_finished()


@pytest.mark.asyncio
async def test_setup_invalid_channel_keeps_existing_settings(setup_context):
    path, origin, interaction, _ = setup_context
    view = ServerSetupView(None, origin)
    view.channel_ids = [11]
    await view.save(interaction)
    async with aiosqlite.connect(path) as db:
        assert await (await db.execute('SELECT mode FROM guild_lfg_settings')).fetchone() == ('all',)
    assert not view.is_finished()


@pytest.mark.asyncio
async def test_setup_database_failure_rolls_back_and_removes_guide(setup_context, monkeypatch):
    path, origin, interaction, channel = setup_context
    monkeypatch.setattr('cogs.admin_cmds.build_quickstart_embed', lambda bot: discord.Embed())
    async with aiosqlite.connect(path) as db:
        await db.execute("CREATE TRIGGER reject_role BEFORE INSERT ON guild_ping_roles BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        await db.commit()
    view = ServerSetupView(None, origin)
    view.channel_ids = [10]
    view.role_ids = [7]
    view.guide_channel_id = 10
    await view.save(interaction)
    async with aiosqlite.connect(path) as db:
        assert await (await db.execute('SELECT mode FROM guild_lfg_settings')).fetchone() == ('all',)
        assert await (await db.execute('SELECT role_id FROM guild_ping_roles')).fetchall() == [(8,)]
    channel.send.return_value.delete.assert_awaited_once()
    assert not view.is_finished()


@pytest.mark.asyncio
async def test_setup_successful_guide_is_tracked(setup_context, monkeypatch):
    path, origin, interaction, _ = setup_context
    monkeypatch.setattr('cogs.admin_cmds.build_quickstart_embed', lambda bot: discord.Embed())
    view = ServerSetupView(None, origin)
    view.channel_ids = [10]
    view.guide_channel_id = 10
    view.cooldown = 0
    await view.save(interaction)
    async with aiosqlite.connect(path) as db:
        assert await (await db.execute('SELECT message_id, guild_id, channel_id FROM quickstart_messages')).fetchone() == (77, 1, 10)
        assert await (await db.execute('SELECT role_id FROM guild_ping_roles')).fetchall() == []
    assert view.is_finished()
