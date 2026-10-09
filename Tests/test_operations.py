import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import discord
import pytest
from discord import app_commands
from discord.ext import commands

import bot
import database
import operations
import igdb_client
from cogs.owner_cmds import OwnerCommands
from cogs.user_cmds import UserCommands


@pytest.fixture
def metrics(monkeypatch):
    counters = operations.OperationalStats()
    monkeypatch.setattr(operations, "stats", counters)
    monkeypatch.setattr(igdb_client, "stats", counters)
    monkeypatch.setattr("cogs.owner_cmds.stats", counters)
    monkeypatch.setattr("cogs.user_cmds.stats", counters)
    return counters


def interaction(done=False):
    return SimpleNamespace(
        response=SimpleNamespace(is_done=lambda: done, send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
        command=SimpleNamespace(name="test_command", qualified_name="test_command"),
        user=SimpleNamespace(id=123),
    )


@pytest.mark.asyncio
async def test_cleanup_is_scoped_and_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "cleanup.sqlite"))
    await database.init_db()
    await database.init_db()  # Index migrations can run repeatedly.
    async with aiosqlite.connect(database.DB_PATH) as db:
        await db.execute("INSERT INTO igdb_games (id, name, updated_at) VALUES (1, 'Game', 0)")
        await db.execute("INSERT INTO subscriptions VALUES (123, 1)")
        for guild in (0, 10, 20):
            await db.execute("INSERT INTO user_settings (user_id, guild_id) VALUES (123, ?)", (guild,))
        for guild in (10, 20):
            await db.execute("INSERT INTO guild_lfg_settings (guild_id) VALUES (?)", (guild,))
            await db.execute("INSERT INTO guild_game_ping_state VALUES (?, 1, 0)", (guild,))
            await db.execute("INSERT INTO guild_ping_roles VALUES (?, 456)", (guild,))
            await db.execute("INSERT INTO quickstart_messages VALUES (?, ?, 789, 0)", (guild, guild))
            await db.execute("INSERT INTO live_top_games (message_id, guild_id, channel_id) VALUES (?, ?, 789)", (guild, guild))
            await db.execute("INSERT INTO user_guild_settings VALUES (123, ?, 1)", (guild,))
        await db.commit()
    await database.delete_guild_data(10)
    await database.delete_guild_data(10)
    async with aiosqlite.connect(database.DB_PATH) as db:
        for table in ('guild_lfg_settings', 'guild_game_ping_state', 'guild_ping_roles',
                      'quickstart_messages', 'live_top_games', 'user_guild_settings'):
            assert await (await db.execute(f"SELECT guild_id FROM {table}")).fetchall() == [(20,)]
        assert await (await db.execute("SELECT guild_id FROM user_settings ORDER BY guild_id")).fetchall() == [(0,), (20,)]
        assert await (await db.execute("SELECT * FROM subscriptions")).fetchall() == [(123, 1)]
        indexes = {row[0] for row in await (await db.execute("SELECT name FROM sqlite_master WHERE type='index'")).fetchall()}
        assert {'idx_subscriptions_game', 'idx_user_guild_settings_guild', 'idx_user_settings_guild', 'idx_quickstart_messages_guild'} <= indexes


@pytest.mark.asyncio
async def test_cleanup_rolls_back_partial_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "rollback.sqlite"))
    await database.init_db()
    async with aiosqlite.connect(database.DB_PATH) as db:
        await db.execute("INSERT INTO guild_lfg_settings (guild_id) VALUES (10)")
        await db.execute("DROP TABLE guild_ping_roles")
        await db.commit()
    with pytest.raises(aiosqlite.OperationalError):
        await database.delete_guild_data(10)
    async with aiosqlite.connect(database.DB_PATH) as db:
        assert await (await db.execute("SELECT guild_id FROM guild_lfg_settings")).fetchall() == [(10,)]


@pytest.mark.asyncio
async def test_remove_event_skips_unavailable_guild(monkeypatch):
    cleanup = AsyncMock()
    monkeypatch.setattr(bot, "delete_guild_data", cleanup)
    await bot.on_guild_remove(SimpleNamespace(id=10, unavailable=True))
    cleanup.assert_not_awaited()
    await bot.on_guild_remove(SimpleNamespace(id=10, unavailable=False))
    cleanup.assert_awaited_once_with(10)


@pytest.mark.asyncio
@pytest.mark.parametrize('done', [False, True])
async def test_unexpected_error_logs_original_and_replies_privately(done, metrics, caplog):
    inter = interaction(done)
    error = RuntimeError('test diagnostic')
    await operations.handle_app_command_error(inter, app_commands.CommandInvokeError(inter.command, error))
    assert metrics.counters['command_errors'] == 1
    sender = inter.followup.send if done else inter.response.send_message
    assert sender.await_args.kwargs['ephemeral'] is True
    assert 'test diagnostic' not in sender.await_args.args[0]
    assert caplog.records[-1].exc_info[1] is error
    assert 'test_command' in caplog.text


@pytest.mark.asyncio
async def test_expected_command_errors_are_not_operational_failures(metrics):
    for error in (app_commands.CheckFailure(), app_commands.CommandOnCooldown(app_commands.Cooldown(1, 30), 15)):
        await operations.handle_app_command_error(interaction(), error)
    ctx = SimpleNamespace(send=AsyncMock(), command=None)
    for error in (commands.CommandNotFound(), commands.NotOwner(), commands.MissingRequiredArgument(MagicMock())):
        await operations.handle_prefix_command_error(ctx, error)
    assert metrics.counters['command_errors'] == 0
    await operations.handle_prefix_command_error(ctx, RuntimeError('failed'))
    assert metrics.counters['command_errors'] == 1


@pytest.mark.asyncio
async def test_expired_interaction_does_not_raise_again(metrics):
    inter = interaction()
    inter.response.send_message.side_effect = discord.HTTPException(SimpleNamespace(status=404, reason='expired'), 'expired')
    await operations.handle_app_command_error(inter, RuntimeError('failure'))
    assert metrics.counters['command_errors'] == 1


@pytest.mark.asyncio
async def test_igdb_counts_final_results_and_excludes_cancelled_requests(metrics):
    client = igdb_client.IGDBClient()
    client._request_impl = AsyncMock(return_value=igdb_client.Response(200, []))
    await client._request('x')
    client._request_impl.return_value = igdb_client.Response(403, None)
    await client._request('x')
    client._request_impl.side_effect = igdb_client.IGDBError('exhausted retries')
    with pytest.raises(igdb_client.IGDBError):
        await client._request('x')
    client._request_impl.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await client._request('x')
    assert metrics.snapshot() == {'igdb_requests': 4, 'igdb_failures': 2}


@pytest.mark.asyncio
async def test_privacy_uses_configured_contact_and_policy(monkeypatch):
    monkeypatch.setenv('PRIVACY_POLICY_URL', 'https://example.com/privacy')
    monkeypatch.setenv('PRIVACY_CONTACT', 'owner@example.com')
    inter = interaction()
    await UserCommands.privacy.callback(UserCommands(MagicMock()), inter)
    assert inter.response.send_message.await_args.kwargs['ephemeral'] is True
    fields = inter.response.send_message.await_args.kwargs['embed'].fields
    assert any(field.value == 'https://example.com/privacy' for field in fields)
    assert any(field.value == 'owner@example.com' for field in fields)


@pytest.mark.asyncio
async def test_stats_is_owner_only_and_ephemeral(metrics, monkeypatch):
    monkeypatch.setattr('cogs.owner_cmds.get_bot_owner_id', lambda: 123)
    cog = OwnerCommands(SimpleNamespace(guilds=[1, 2]))
    inter = interaction()
    assert await OwnerCommands.operational_stats.checks[0](inter)
    inter.user.id = 456
    assert not await OwnerCommands.operational_stats.checks[0](inter)
    inter.user.id = 123
    metrics.increment('pings_sent')
    await OwnerCommands.operational_stats.callback(cog, inter)
    call = inter.response.send_message.await_args
    assert call.kwargs['ephemeral'] is True
    fields = {field.name: field.value for field in call.kwargs['embed'].fields}
    assert fields['Current servers'] == '2'
    assert fields['Completed LFG pings'] == '1'
