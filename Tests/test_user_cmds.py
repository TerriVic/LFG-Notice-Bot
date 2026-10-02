import pytest
import aiosqlite
import discord
from unittest.mock import AsyncMock, MagicMock, patch

from cogs.user_cmds import UserCommands, autocomplete_user_subscriptions
from database import init_db

# -----------------------------------------------------------------------------
# Test Fixtures & Helpers
# -----------------------------------------------------------------------------

@pytest.fixture
async def setup_test_db(tmp_path, monkeypatch):
    """Sets up a fresh temporary database schema for testing user_cmds."""
    test_db_path = tmp_path / "test_user_cmds.sqlite"

    # Ensure any lingering test DB is deleted prior to setup
    if test_db_path.exists():
        test_db_path.unlink()

    monkeypatch.setattr("database.DB_PATH", str(test_db_path))
    monkeypatch.setattr("cogs.user_cmds.DB_PATH", str(test_db_path))

    # Initialize complete DB schema
    await init_db()

    # Pre-seed basic test game
    async with aiosqlite.connect(test_db_path) as db:
        await db.execute(
            """
            INSERT INTO igdb_games (id, name, updated_at, is_multiplayer)
            VALUES (?, ?, ?, ?)
            """,
            (1, "Portal 2", 1600000000, 1)
        )
        await db.commit()

    return test_db_path


def make_mock_interaction(user_id=12345, guild_id=999, channel_id=888):
    """Creates a mock discord.Interaction with standard attributes."""
    interaction = AsyncMock(spec=discord.Interaction)
    interaction.user = MagicMock()
    interaction.user.id = user_id
    interaction.user.mention = f"<@{user_id}>"
    interaction.guild_id = guild_id
    interaction.channel_id = channel_id

    # Mock Guild
    if guild_id:
        interaction.guild = MagicMock()
        interaction.guild.id = guild_id
        interaction.guild.name = "Test Guild"
        # Mock member lookup
        mock_member = MagicMock()
        mock_member.id = user_id
        interaction.guild.get_member = lambda uid: mock_member
    else:
        interaction.guild = None

    # Mock response & followup
    interaction.response = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = AsyncMock()
    interaction.followup.send = AsyncMock()

    return interaction


# -----------------------------------------------------------------------------
# 1. Tests for Subscribing & Unsubscribing
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_subscribe_command_success(setup_test_db):
    """Test subscribing to an existing cached game."""
    cog = UserCommands(bot=MagicMock())
    interaction = make_mock_interaction(user_id=101)

    with patch("cogs.user_cmds.resolve_to_base_game", new=AsyncMock(return_value=(1, "Portal 2"))):
        await cog.subscribe.callback(cog, interaction, game="Portal 2")

    interaction.response.defer.assert_called_once_with(ephemeral=True)
    interaction.followup.send.assert_called_once()
    msg = interaction.followup.send.call_args[0][0]
    assert "subscribed to **Portal 2**" in msg

    # Verify database insertion
    async with aiosqlite.connect(setup_test_db) as db:
        async with db.execute("SELECT user_id, game_id FROM subscriptions WHERE user_id = ?", (101,)) as cursor:
            row = await cursor.fetchone()

    assert row is not None
    assert row[0] == 101
    assert row[1] == 1


@pytest.mark.asyncio
async def test_subscribe_already_subscribed(setup_test_db):
    """Test subscribing when user is already subscribed."""
    cog = UserCommands(bot=MagicMock())
    interaction = make_mock_interaction(user_id=101)

    # Insert existing subscription
    async with aiosqlite.connect(setup_test_db) as db:
        await db.execute("INSERT INTO subscriptions (user_id, game_id) VALUES (?, ?)", (101, 1))
        await db.commit()

    with patch("cogs.user_cmds.resolve_to_base_game", new=AsyncMock(return_value=(1, "Portal 2"))):
        await cog.subscribe.callback(cog, interaction, game="Portal 2")

    interaction.followup.send.assert_called_once()
    msg = interaction.followup.send.call_args[0][0]
    assert "already subscribed" in msg


@pytest.mark.asyncio
async def test_unsubscribe_command_success(setup_test_db):
    """Test unsubscribing from a game."""
    cog = UserCommands(bot=MagicMock())
    interaction = make_mock_interaction(user_id=101)

    # Insert existing subscription
    async with aiosqlite.connect(setup_test_db) as db:
        await db.execute("INSERT INTO subscriptions (user_id, game_id) VALUES (?, ?)", (101, 1))
        await db.commit()

    with patch("cogs.user_cmds.resolve_to_base_game", new=AsyncMock(return_value=(1, "Portal 2"))):
        await cog.unsubscribe.callback(cog, interaction, game="Portal 2")

    interaction.followup.send.assert_called_once()
    msg = interaction.followup.send.call_args[0][0]
    assert "unsubscribed from **Portal 2**" in msg

    # Verify record was removed
    async with aiosqlite.connect(setup_test_db) as db:
        async with db.execute("SELECT * FROM subscriptions WHERE user_id = ?", (101,)) as cursor:
            row = await cursor.fetchone()

    assert row is None


# -----------------------------------------------------------------------------
# 2. Tests for LFG Pings (`ping_game`)
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ping_game_disallowed_channel(setup_test_db):
    """Test ping_game when executed in a channel where LFG is disallowed."""
    cog = UserCommands(bot=MagicMock())
    interaction = make_mock_interaction(guild_id=999, channel_id=888)

    with patch("cogs.user_cmds.is_lfg_allowed_in_channel", new=AsyncMock(return_value=False)):
        await cog.ping_game.callback(cog, interaction, game="Portal 2")

    interaction.response.send_message.assert_called_once()
    msg = interaction.response.send_message.call_args[0][0]
    assert "LFG pings are not allowed in this channel" in msg


@pytest.mark.asyncio
async def test_ping_game_success(setup_test_db):
    """Test pinging game subscribers when LFG channel is allowed."""
    cog = UserCommands(bot=MagicMock())
    caller_interaction = make_mock_interaction(user_id=101, guild_id=999, channel_id=888)

    # Ensure guild.get_member returns valid member objects for both caller and subscriber
    sub_member = MagicMock()
    sub_member.id = 202
    caller_interaction.guild.get_member = lambda uid: sub_member if uid == 202 else MagicMock(id=uid)

    # Seed subscriber user 202
    async with aiosqlite.connect(setup_test_db) as db:
        await db.execute("INSERT INTO subscriptions (user_id, game_id) VALUES (?, ?)", (202, 1))
        await db.commit()

    with patch("cogs.user_cmds.is_lfg_allowed_in_channel", new=AsyncMock(return_value=True)), \
            patch("cogs.user_cmds.resolve_to_base_game", new=AsyncMock(return_value=(1, "Portal 2"))):
        await cog.ping_game.callback(cog, caller_interaction, game="Portal 2", note="Let's play co-op!")

    caller_interaction.followup.send.assert_called_once()
    sent_msg = caller_interaction.followup.send.call_args[0][0]
    assert "<@202>" in sent_msg
    assert "Portal 2" in sent_msg
    assert "Let's play co-op!" in sent_msg


# -----------------------------------------------------------------------------
# 3. Tests for Quiet Mode & Preferences
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_quiet_mode_toggle_global(setup_test_db):
    """Test toggling quiet_mode globally."""
    cog = UserCommands(bot=MagicMock())
    interaction = make_mock_interaction(user_id=101)

    # Enable global quiet mode
    await cog.quiet_mode.callback(cog, interaction, enabled=True, scope=None)

    interaction.followup.send.assert_called_once()
    msg = interaction.followup.send.call_args[0][0]
    assert "enabled 🔕" in msg
    assert "Global" in msg

    # Verify DB state
    async with aiosqlite.connect(setup_test_db) as db:
        async with db.execute("SELECT quiet_mode FROM user_settings WHERE user_id = ? AND guild_id = 0", (101,)) as cursor:
            row = await cursor.fetchone()

    assert row is not None
    assert row[0] == 1


# -----------------------------------------------------------------------------
# 4. Tests for Autocomplete Helpers
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_autocomplete_user_subscriptions(setup_test_db):
    """Test autocomplete listing user's active subscriptions."""
    interaction = make_mock_interaction(user_id=101)

    # Seed user subscription to Portal 2
    async with aiosqlite.connect(setup_test_db) as db:
        await db.execute("INSERT INTO subscriptions (user_id, game_id) VALUES (?, ?)", (101, 1))
        await db.commit()

    choices = await autocomplete_user_subscriptions(interaction, current="Port")
    assert len(choices) == 1
    assert choices[0].name == "Portal 2"