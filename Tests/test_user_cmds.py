import pytest
import re
import aiosqlite
import discord
from unittest.mock import AsyncMock, MagicMock, patch

from cogs.user_cmds import UserCommands, autocomplete_user_subscriptions, can_user_ping_game, split_ping_message
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

    # /ping_game now posts via the channel rather than the interaction webhook.
    interaction.channel = MagicMock()
    interaction.channel.send = AsyncMock()
    interaction.delete_original_response = AsyncMock()

    # Mock Guild
    if guild_id:
        interaction.guild = MagicMock()
        interaction.guild.id = guild_id
        interaction.guild.name = "Test Guild"
        # Mock member lookup
        mock_member = MagicMock()
        mock_member.id = user_id
        mock_member.display_name = "TestPlayer"
        interaction.guild.get_member = lambda uid: mock_member
    else:
        interaction.guild = None

    # Mock response & followup
    interaction.response = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.channel = MagicMock()
    interaction.channel.send = AsyncMock()
    interaction.delete_original_response = AsyncMock()

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
@patch("cogs.user_cmds.fetch_and_cache_game_from_igdb", new_callable=AsyncMock)
async def test_subscribe_game_not_found(mock_fetch_cache, setup_test_db):
    """Test subscribe command when the game is missing locally and on IGDB."""

    # 1. Arrange: Configure mock IGDB fetch to return None (game not found)
    mock_fetch_cache.return_value = None

    mock_guild = MagicMock()
    mock_guild.name = "Test Server"

    interaction = MagicMock()
    interaction.guild = mock_guild
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    bot = MagicMock()
    cog = UserCommands(bot)

    # 2. Act: Call the subscribe command with a nonexistent game name
    # (Assuming your command argument is named 'game')
    await cog.subscribe.callback(cog, interaction, game="TotallyFakeGame123")

    # 3. Assert: Verify it tried to fetch it, then sent the error message
    mock_fetch_cache.assert_awaited_once()

    interaction.followup.send.assert_awaited_once_with(
        "Could not find **TotallyFakeGame123** in local cache or on IGDB.",
        ephemeral=True
    )

@pytest.mark.asyncio
@patch("cogs.user_cmds.fetch_and_cache_game_from_igdb", new_callable=AsyncMock)
async def test_subscribe_game_fetched_from_igdb(mock_fetch_cache, setup_test_db):
    """Test subscribe when the game is missing locally, but successfully found on IGDB."""

    # 1. Arrange: Return a valid (id, name) tuple to simulate a successful IGDB fetch
    mock_fetch_cache.return_value = (456, "Cyberpunk 2077")

    mock_guild = MagicMock()
    mock_guild.name = "Test Server"

    interaction = MagicMock()
    interaction.user.id = 101
    interaction.guild.id = 555
    interaction.guild = mock_guild
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    cog = UserCommands(bot=MagicMock())

    # 2. Act: Call the subscribe command for a game not in the local test DB
    await cog.subscribe.callback(cog, interaction, game="Cyberpunk 2077")

    # 3. Assert: Verify that the fetch function was called,
    # which executes 'raw_id, raw_name = result' and proceeds to the next steps.
    mock_fetch_cache.assert_awaited_once()

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

@pytest.mark.asyncio
async def test_unsubscribe_no_games(setup_test_db):
    """Test that selecting the '__NONE__' option triggers the warning message."""

    # 1. Arrange: Setup mocks for interaction and bot
    interaction = MagicMock()
    interaction.response.send_message = AsyncMock()

    bot = MagicMock()
    cog = UserCommands(bot)

    # 2. Act: Invoke the command callback directly with game="__NONE__"
    await cog.unsubscribe.callback(cog, interaction, game="__NONE__")

    # 3. Assert: Verify the response was sent ephemerally and execution stopped
    interaction.response.send_message.assert_awaited_once_with(
        "You are not subscribed to any games.",
        ephemeral=True
    )

@pytest.mark.asyncio
async def test_unsubscribe_game_not_found(setup_test_db):
    """Test that selecting a game that doesn't exist triggers the warning message."""

    # Use make_mock_interaction to ensure user_id and response mocks are fully configured
    interaction = make_mock_interaction(user_id=101)

    bot = MagicMock()
    cog = UserCommands(bot)

    # Act: Invoke unsubscribe with a game not in the database
    await cog.unsubscribe.callback(cog, interaction, game="abc")

    # Assert: Verify warning message was sent
    interaction.followup.send.assert_awaited_once_with(
        "Game not found.",
        ephemeral=True
    )

# -----------------------------------------------------------------------------
# 2. Tests for LFG Pings (`ping_game`)
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ping_game_rejects_dm_context(setup_test_db):
    cog = UserCommands(bot=MagicMock())
    interaction = make_mock_interaction(user_id=101, guild_id=None, channel_id=None)

    await cog.ping_game.callback(cog, interaction, game="Portal 2")

    interaction.response.send_message.assert_awaited_once_with(
        "This command can only be used inside a server.",
        ephemeral=True
    )
    interaction.response.defer.assert_not_awaited()


@pytest.mark.asyncio
async def test_can_user_ping_game_requires_configured_role(setup_test_db):
    interaction = make_mock_interaction(user_id=101, guild_id=999, channel_id=888)
    interaction.user.guild_permissions = MagicMock(administrator=False, manage_messages=False)
    interaction.user.roles = []

    allowed_role = MagicMock()
    allowed_role.id = 777
    interaction.guild.get_role = lambda role_id: allowed_role if role_id == 777 else None

    async with aiosqlite.connect(setup_test_db) as db:
        await db.execute(
            "INSERT INTO guild_ping_roles (guild_id, role_id) VALUES (?, ?)",
            (999, 777)
        )
        await db.commit()

    with patch("cogs.user_cmds.get_bot_owner_id", return_value=0):
        allowed, role_ids = await can_user_ping_game(interaction)

    assert allowed is False
    assert role_ids == [777]

    member_role = MagicMock()
    member_role.id = 777
    interaction.user.roles = [member_role]

    with patch("cogs.user_cmds.get_bot_owner_id", return_value=0):
        allowed, role_ids = await can_user_ping_game(interaction)

    assert allowed is True
    assert role_ids == [777]


@pytest.mark.asyncio
async def test_ping_game_rejects_user_without_allowed_role(setup_test_db):
    cog = UserCommands(bot=MagicMock())
    interaction = make_mock_interaction(user_id=101, guild_id=999, channel_id=888)

    with patch("cogs.user_cmds.is_lfg_allowed_in_channel", new=AsyncMock(return_value=True)), \
            patch("cogs.user_cmds.can_user_ping_game", new=AsyncMock(return_value=(False, [777]))):
        await cog.ping_game.callback(cog, interaction, game="Portal 2")

    interaction.response.defer.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once()
    msg = interaction.response.send_message.await_args.args[0]
    assert "allowed LFG ping roles" in msg
    assert "<@&777>" in msg


@pytest.mark.asyncio
async def test_ping_game_blocked_by_server_game_cooldown(setup_test_db):
    cog = UserCommands(bot=MagicMock())
    interaction = make_mock_interaction(user_id=101, guild_id=999, channel_id=888)

    subscriber = MagicMock()
    subscriber.id = 202
    interaction.guild.get_member = lambda uid: subscriber if uid == 202 else MagicMock(id=uid, display_name="Caller")

    async with aiosqlite.connect(setup_test_db) as db:
        await db.execute("INSERT INTO subscriptions (user_id, game_id) VALUES (?, ?)", (202, 1))
        await db.commit()

    with patch("cogs.user_cmds.is_lfg_allowed_in_channel", new=AsyncMock(return_value=True)), \
            patch("cogs.user_cmds.can_user_ping_game", new=AsyncMock(return_value=(True, []))), \
            patch("cogs.user_cmds.resolve_to_base_game", new=AsyncMock(return_value=(1, "Portal 2"))), \
            patch("cogs.user_cmds.claim_game_ping_cooldown", new=AsyncMock(return_value=(False, 300))):
        await cog.ping_game.callback(cog, interaction, game="Portal 2")

    interaction.response.defer.assert_awaited_once()
    interaction.followup.send.assert_awaited_once()
    msg = interaction.followup.send.await_args.args[0]
    assert "Portal 2" in msg
    assert "pinged recently" in msg
    assert "<t:" in msg


@pytest.mark.asyncio
async def test_ping_game_disallowed_channel(setup_test_db):
    """Test ping_game when executed in a channel where LFG is disallowed."""
    cog = UserCommands(bot=MagicMock())
    interaction = make_mock_interaction(guild_id=999, channel_id=888)

    with patch("cogs.user_cmds.is_lfg_allowed_in_channel", new=AsyncMock(return_value=False)):
        await cog.ping_game.callback(cog, interaction, game="Portal 2")

    interaction.response.send_message.assert_called_once()
    msg = interaction.response.send_message.call_args[0][0]
    assert "LFG pings are not enabled in this channel" in msg
    assert "/config setup_bot" in msg


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_failure", [False, True], ids=["status-deleted", "status-delete-failed"])
async def test_ping_game_success(setup_test_db, cleanup_failure):
    """Test pinging game subscribers when LFG channel is allowed."""
    cog = UserCommands(bot=MagicMock())
    caller_interaction = make_mock_interaction(user_id=101, guild_id=999, channel_id=888)

    if cleanup_failure:
        caller_interaction.delete_original_response.side_effect = discord.HTTPException(
            MagicMock(status=404, reason="Not Found"), "Unknown interaction"
        )

    # Ensure guild.get_member returns valid member objects for both caller and subscriber
    sub_member = MagicMock()
    sub_member.id = 202
    caller_interaction.guild.get_member = lambda uid: sub_member if uid == 202 else MagicMock(id=uid, display_name="Caller")

    # Seed subscriber user 202
    async with aiosqlite.connect(setup_test_db) as db:
        await db.execute("INSERT INTO subscriptions (user_id, game_id) VALUES (?, ?)", (202, 1))
        await db.commit()

    with patch("cogs.user_cmds.is_lfg_allowed_in_channel", new=AsyncMock(return_value=True)), \
            patch("cogs.user_cmds.resolve_to_base_game", new=AsyncMock(return_value=(1, "Portal 2"))):
        await cog.ping_game.callback(cog, caller_interaction, game="Portal 2", note="Let's play co-op!")

    caller_interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    caller_interaction.channel.send.assert_awaited_once()
    caller_interaction.delete_original_response.assert_awaited_once()
    caller_interaction.followup.send.assert_not_awaited()

    sent_msg = caller_interaction.channel.send.await_args.args[0]
    assert "<@101>" in sent_msg  # Caller shown as clickable mention, no notification.
    assert "<@202>" in sent_msg
    assert "Portal 2" in sent_msg
    assert "Let's play co-op!" in sent_msg

    send_kwargs = caller_interaction.channel.send.await_args.kwargs
    assert send_kwargs["silent"] is False
    allowed_mentions = send_kwargs["allowed_mentions"]
    assert [user.id for user in allowed_mentions.users] == [202]
    assert all(user.id != 101 for user in allowed_mentions.users)
    assert allowed_mentions.roles is False
    assert allowed_mentions.everyone is False

@pytest.mark.asyncio
@patch("cogs.user_cmds.is_lfg_allowed_in_channel", new_callable=AsyncMock)
async def test_ping_game_game_not_found(mock_lfg_allowed, setup_test_db):
    """Test that selecting a game that doesn't exist triggers the warning message."""

    mock_lfg_allowed.return_value = True

    # Arrange with full mock interaction and db schema
    interaction = make_mock_interaction(user_id=101, guild_id=999, channel_id=888)

    bot = MagicMock()
    cog = UserCommands(bot)

    # Act: Invoke ping_game with a nonexistent game name
    await cog.ping_game.callback(cog, interaction, game="abc")

    # Assert: Verify followup warning message
    interaction.followup.send.assert_awaited_once_with(
        "Game not found.",
        ephemeral=True
    )

@pytest.mark.asyncio
@patch("cogs.user_cmds.is_lfg_allowed_in_channel", new_callable=AsyncMock)
@patch("cogs.user_cmds.resolve_to_base_game", new_callable=AsyncMock)
async def test_ping_game_with_custom_template(mock_resolve, mock_lfg_allowed, setup_test_db):
    """Test ping_game command utilizing a user's custom ping template."""

    # 1. Arrange Mocks
    mock_lfg_allowed.return_value = True
    mock_resolve.return_value = (2, "Elden Ring")

    interaction = make_mock_interaction(user_id=101, guild_id=123, channel_id=456)
    interaction.user.mention = "<@101>"

    # 2. Seed Test Database (Use ID 2 to avoid conflicting with Portal 2's ID 1 in the fixture)
    async with aiosqlite.connect(setup_test_db) as db:
        await db.execute(
            "INSERT INTO igdb_games (id, name, updated_at, is_multiplayer) VALUES (?, ?, ?, ?)",
            (2, "Elden Ring", 1600000000, 1)
        )
        await db.execute("INSERT INTO subscriptions (user_id, game_id) VALUES (?, ?)", (102, 2))
        await db.execute(
            """INSERT OR REPLACE INTO user_settings (user_id, ping_template, quiet_mode) 
               VALUES (?, ?, 0)""",
            (101, "Custom alert by {caller} for {game}! {mentions} - {note}")
        )
        await db.commit()

    cog = UserCommands(bot=MagicMock())

    # 3. Act: Invoke the command callback with a note
    await cog.ping_game.callback(cog, interaction, game="Elden Ring", note="Need a tank")

    # 4. Assert
    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    interaction.channel.send.assert_awaited_once()
    interaction.delete_original_response.assert_awaited_once()
    interaction.followup.send.assert_not_awaited()

    sent_msg = interaction.channel.send.await_args.args[0]

    assert "Custom alert by <@101> for **Elden Ring**!" in sent_msg
    assert "<@102>" in sent_msg
    assert [user.id for user in interaction.channel.send.await_args.kwargs["allowed_mentions"].users] == [102]
    assert "Need a tank" in sent_msg


@pytest.mark.asyncio
@patch("cogs.user_cmds.is_lfg_allowed_in_channel", new_callable=AsyncMock)
@patch("cogs.user_cmds.resolve_to_base_game", new_callable=AsyncMock)
async def test_ping_game_with_quiet_users(mock_resolve, mock_lfg_allowed, setup_test_db):
    """Test ping_game when subscribed users have quiet mode enabled."""

    # 1. Arrange Mocks
    mock_lfg_allowed.return_value = True
    mock_resolve.return_value = (1, "Portal 2")

    interaction = make_mock_interaction(user_id=101, guild_id=999, channel_id=888)

    # Mock guild member lookup, including a real display name for quiet-mode text.
    sub_member = MagicMock()
    sub_member.id = 202
    sub_member.display_name = "QuietPlayer"
    interaction.guild.get_member = lambda uid: sub_member if uid == 202 else None

    # 2. Seed Test Database with a quiet subscriber (user 202)
    async with aiosqlite.connect(setup_test_db) as db:
        await db.execute("INSERT INTO subscriptions (user_id, game_id) VALUES (?, ?)", (202, 1))
        # Enable quiet mode for user 202
        await db.execute(
            "INSERT OR REPLACE INTO user_settings (user_id, quiet_mode, guild_id) VALUES (?, ?, ?)",
            (202, 1, 0)
        )
        await db.commit()

    cog = UserCommands(bot=MagicMock())

    # 3. Act: Invoke ping_game
    await cog.ping_game.callback(cog, interaction, game="Portal 2", note="Let's play co-op!")

    # 4. Quiet subscribers are clickable mentions, excluded from notifications.
    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    interaction.channel.send.assert_awaited_once()
    interaction.delete_original_response.assert_awaited_once()
    interaction.followup.send.assert_not_awaited()

    sent_msg = interaction.channel.send.await_args.args[0]
    assert "QuietPlayer" not in sent_msg
    assert "<@101>" in sent_msg
    assert "<@202> has quiet mode turned on" in sent_msg
    allowed_mentions = interaction.channel.send.await_args.kwargs["allowed_mentions"]
    assert allowed_mentions.users == []
    assert allowed_mentions.roles is False
    assert allowed_mentions.everyone is False
    assert "has quiet mode turned on" in sent_msg

# -----------------------------------------------------------------------------
# 3. Tests for Custom Ping Templates (`set_ping_template`)
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_set_ping_template_too_long(setup_test_db):
    """Test that templates exceeding 300 characters are rejected."""
    interaction = make_mock_interaction(user_id=101)
    cog = UserCommands(bot=MagicMock())

    # Create a string longer than 300 characters
    long_template = "A" * 301

    await cog.set_ping_template.callback(cog, interaction, template=long_template)

    # Verify error response and ensure no database interaction occurred
    interaction.response.send_message.assert_called_once_with(
        "Template is too long! Please keep it under 300 characters.",
        ephemeral=True
    )


@pytest.mark.asyncio
async def test_set_ping_template_success(setup_test_db):
    """Test setting a valid custom template that includes {mentions}."""
    interaction = make_mock_interaction(user_id=101)
    cog = UserCommands(bot=MagicMock())

    template_str = "Hey {caller}, let's play {game}! {mentions} - Note: {note}"

    await cog.set_ping_template.callback(cog, interaction, template=template_str)

    # Verify database update
    async with aiosqlite.connect(setup_test_db) as db:
        async with db.execute("SELECT ping_template FROM user_settings WHERE user_id = ?", (101,)) as cursor:
            row = await cursor.fetchone()

    assert row is not None
    assert row[0] == template_str


@pytest.mark.asyncio
async def test_set_ping_template_preserves_authored_template(setup_test_db):
    """Keep missing tags visible to later previews by storing the authored template."""
    interaction = make_mock_interaction(user_id=101)
    cog = UserCommands(bot=MagicMock())

    template_str = "Hey everyone, time for {game}!"

    await cog.set_ping_template.callback(cog, interaction, template=template_str)

    # Verify the authored template is stored unchanged
    async with aiosqlite.connect(setup_test_db) as db:
        async with db.execute("SELECT ping_template FROM user_settings WHERE user_id = ?", (101,)) as cursor:
            row = await cursor.fetchone()

    assert row is not None
    assert row[0] == template_str  # Authored tags are preserved; fallback is rendered.

@pytest.mark.asyncio
async def test_view_ping_template_default(setup_test_db):
    """Test view_ping_template when the user has no custom template saved."""
    interaction = make_mock_interaction(user_id=101)
    cog = UserCommands(bot=MagicMock())

    await cog.view_ping_template.callback(cog, interaction)

    interaction.response.send_message.assert_awaited_once()
    msg = interaction.response.send_message.call_args[0][0]

    assert "You are currently using the **default ping format**" in msg
    assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_view_ping_template_custom(setup_test_db):
    """Test view_ping_template when a custom template is saved."""
    interaction = make_mock_interaction(user_id=101)
    cog = UserCommands(bot=MagicMock())

    # Seed a custom template for user 101 using the correct composite key structure (guild_id = 0)
    async with aiosqlite.connect(setup_test_db) as db:
        await db.execute(
            """INSERT OR REPLACE INTO user_settings (user_id, guild_id, ping_template) 
               VALUES (?, 0, ?)""",
            (101, "Custom template by {caller} for {game}! {mentions} - {note}")
        )
        await db.commit()

    await cog.view_ping_template.callback(cog, interaction)

    interaction.response.send_message.assert_awaited_once()
    msg = interaction.response.send_message.call_args[0][0]

    assert "Your Current Custom Template:" in msg
    # Verify that placeholders like {caller} and {game} were correctly parsed in the preview
    assert "Custom template by <@101> for **[game]**!" in msg
    assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True

@pytest.mark.asyncio
async def test_reset_ping_template(setup_test_db):
    """Test reset_ping_template command sends a confirmation view and clears the template upon confirmation."""
    interaction = make_mock_interaction(user_id=101)
    cog = UserCommands(bot=MagicMock())

    # 1. Seed a custom template first so we can verify it gets cleared later
    async with aiosqlite.connect(setup_test_db) as db:
        await db.execute(
            """INSERT OR REPLACE INTO user_settings (user_id, guild_id, ping_template) 
               VALUES (?, 0, ?)""",
            (101, "Custom template to be deleted")
        )
        await db.commit()

    # 2. Act: Call the reset command
    await cog.reset_ping_template.callback(cog, interaction)

    # 3. Assert: Verify the confirmation prompt and view were sent ephemerally
    interaction.response.send_message.assert_awaited_once()
    call_kwargs = interaction.response.send_message.call_args.kwargs

    assert "Are you **sure** you want to reset your custom ping template" in interaction.response.send_message.call_args[0][0]
    assert call_kwargs.get("ephemeral") is True

    # Extract the attached confirmation view
    view = call_kwargs.get("view")
    assert view is not None

    # 4. Simulate confirmation button click by invoking the view's on_confirm callback
    if hasattr(view, "on_confirm"):
        await view.on_confirm(interaction)

    # 5. Assert: Verify database row now has ping_template set to NULL
    async with aiosqlite.connect(setup_test_db) as db:
        async with db.execute("SELECT ping_template FROM user_settings WHERE user_id = ?", (101,)) as cursor:
            row = await cursor.fetchone()

    assert row is not None
    assert row[0] is None
# -----------------------------------------------------------------------------
# 4. Tests for Quiet Mode & Preferences
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
# 5. Tests for Autocomplete Helpers
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_autocomplete_user_subscriptions(setup_test_db):
    """Test autocomplete listing user's active subscriptions."""
    interaction = make_mock_interaction(user_id=101)

    choices = await autocomplete_user_subscriptions(interaction, current="Port")
    assert len(choices) == 1
    assert choices[0].name == "No subscribed games found"

    # Seed user subscription to Portal 2
    async with aiosqlite.connect(setup_test_db) as db:
        await db.execute("INSERT INTO subscriptions (user_id, game_id) VALUES (?, ?)", (101, 1))
        await db.commit()

    choices = await autocomplete_user_subscriptions(interaction, current="Port")
    assert len(choices) == 1
    assert choices[0].name == "Portal 2"


# -----------------------------------------------------------------------------
# 6. Tests for Top Games
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_top_games_no_guild(setup_test_db):
    """Test top games listing."""
    interaction = make_mock_interaction(user_id=101)
    interaction.guild = None

    bot = MagicMock()
    cog = UserCommands(bot)

    await cog.top_games.callback(cog, interaction, limit=10, public=False)

    interaction.response.send_message.assert_awaited_once_with(
        "This command can only be used inside a server.", ephemeral=True
    )

@pytest.mark.asyncio
async def test_top_games_empty_database(setup_test_db):
    """Test top_games when no subscriptions exist in the database."""
    mock_guild = MagicMock()
    mock_guild.name = "Test Server"

    interaction = MagicMock()
    interaction.guild = mock_guild
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    bot = MagicMock()
    cog = UserCommands(bot)

    await cog.top_games.callback(cog, interaction, limit=10, public=False)

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    interaction.followup.send.assert_awaited_once_with(
        "There are no active subscriptions in this server.", ephemeral=True
    )

@pytest.mark.asyncio
async def test_top_games_no_members_in_guild(setup_test_db):
    """Test top_games when global subscriptions exist, but none belong to members of this server."""
    mock_guild = MagicMock()
    mock_guild.name = "Test Server"
    # Ensure get_member always returns None (user not in guild)
    mock_guild.get_member.return_value = None

    interaction = MagicMock()
    interaction.guild = mock_guild
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    # Seed a subscription for user 999, who is NOT in this guild
    async with aiosqlite.connect(setup_test_db) as db:
        await db.execute("INSERT OR IGNORE INTO igdb_games (id, name) VALUES (1, 'Portal 2')")
        await db.execute("INSERT OR IGNORE INTO subscriptions (user_id, game_id) VALUES (999, 1)")
        await db.commit()

    bot = MagicMock()
    cog = UserCommands(bot)

    await cog.top_games.callback(cog, interaction, limit=10, public=False)

    # Verify it hits the exact message you asked about
    interaction.followup.send.assert_awaited_once_with(
        "No members in this server are currently subscribed to any games.", ephemeral=True
    )

@pytest.mark.asyncio
async def test_top_games_success(setup_test_db):
    """Test top_games successfully aggregates, sorts, and displays games."""
    mock_guild = MagicMock()
    mock_guild.name = "Test Server"

    # Simulate that user 101 and 102 are present in the guild, but 999 is not
    mock_guild.get_member.side_effect = lambda uid: MagicMock() if uid in [101, 102] else None

    interaction = MagicMock()
    interaction.guild = mock_guild
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    # Seed test database safely using INSERT OR IGNORE
    async with aiosqlite.connect(setup_test_db) as db:
        await db.execute(
            "INSERT OR IGNORE INTO igdb_games (id, name) VALUES (?, ?), (?, ?)",
            (1, 'Portal 2', 2, 'Elden Ring')
        )
        await db.execute(
            "INSERT OR IGNORE INTO subscriptions (user_id, game_id) VALUES (?, ?), (?, ?), (?, ?)",
            (101, 1, 102, 1, 999, 2)
        )
        await db.commit()

    bot = MagicMock()
    cog = UserCommands(bot)

    await cog.top_games.callback(cog, interaction, limit=10, public=False)

    # Assert responses and embed generation
    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    interaction.followup.send.assert_awaited_once()

    # Extract the embed passed to followup.send and verify its content
    call_kwargs = interaction.followup.send.await_args.kwargs
    embed = call_kwargs.get("embed")

    assert embed is not None
    assert "Top 1 Games in Test Server" in embed.title  # Only Portal 2 counts because 999 is filtered out
    assert "Portal 2" in embed.description
    assert "2 subscribers" in embed.description

# -----------------------------------------------------------------------------
# Tests for User Subscriptions & Show Subscribers
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_user_subscriptions_empty(setup_test_db):
    """Test user_subscriptions when the target user has no subscriptions."""
    interaction = make_mock_interaction(user_id=101)
    cog = UserCommands(bot=MagicMock())

    await cog.user_subscriptions.callback(cog, interaction, user=None, public=False)

    interaction.response.send_message.assert_awaited_once()
    msg = interaction.response.send_message.call_args[0][0]
    assert "No active subscriptions found for" in msg
    assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_user_subscriptions_success(setup_test_db):
    """Test user_subscriptions successfully lists a user's games alphabetically in an embed."""
    interaction = make_mock_interaction(user_id=101)

    async with aiosqlite.connect(setup_test_db) as db:
        # Provide updated_at to satisfy the NOT NULL constraint
        await db.execute(
            "INSERT OR IGNORE INTO igdb_games (id, name, updated_at) VALUES (?, ?, ?)",
            (2, 'Halo Infinite', 1600000000)
        )
        await db.executemany(
            "INSERT INTO subscriptions (user_id, game_id) VALUES (?, ?)",
            [(101, 1), (101, 2)]
        )
        await db.commit()

    cog = UserCommands(bot=MagicMock())
    await cog.user_subscriptions.callback(cog, interaction, user=None, public=True)

    interaction.response.send_message.assert_awaited_once()

    # Retrieve keyword arguments passed to send_message
    kwargs = interaction.response.send_message.call_args.kwargs
    assert kwargs.get("ephemeral") is False

    # Extract the embed and assert on title and description
    embed = kwargs.get("embed")
    assert embed is not None
    assert embed.title == "Your Subscriptions"
    assert "• Portal 2" in embed.description
    assert "• Halo Infinite" in embed.description


@pytest.mark.asyncio
async def test_show_subscribers_no_guild(setup_test_db):
    """Test show_subscribers rejects usage outside a server context."""
    interaction = make_mock_interaction(user_id=101)
    interaction.guild = None  # Outside server

    cog = UserCommands(bot=MagicMock())
    await cog.show_subscribers.callback(cog, interaction, game="Portal 2", public=False)

    interaction.response.send_message.assert_awaited_once_with(
        "This command can only be used inside a server.", ephemeral=True
    )


@pytest.mark.asyncio
async def test_show_subscribers_success(setup_test_db):
    """Test show_subscribers successfully finds and displays guild members subscribed to a game."""
    mock_guild = MagicMock()
    mock_guild.name = "Test Server"
    # User 202 is a member of the server
    mock_guild.get_member.side_effect = lambda uid: MagicMock() if uid == 202 else None

    interaction = MagicMock()
    interaction.guild = mock_guild
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    # Seed subscription for user 202 on Portal 2 (id=1)
    async with aiosqlite.connect(setup_test_db) as db:
        await db.execute("INSERT INTO subscriptions (user_id, game_id) VALUES (?, ?)", (202, 1))
        await db.commit()

    cog = UserCommands(bot=MagicMock())
    with patch("cogs.user_cmds.resolve_to_base_game", new=AsyncMock(return_value=(1, "Portal 2"))):
        await cog.show_subscribers.callback(cog, interaction, game="Portal 2", public=False)

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    interaction.followup.send.assert_awaited_once()

    sent_msg = interaction.followup.send.call_args[0][0]
    assert "Players in this server interested in **Portal 2**" in sent_msg
    assert "<@202>" in sent_msg

# -----------------------------------------------------------------------------
# Tests for Data Deletion & Game Info Commands
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delete_my_subscriptions(setup_test_db):
    """Test delete_my_subscriptions prompts confirmation and deletes subscriptions upon confirmation."""
    interaction = make_mock_interaction(user_id=101)
    cog = UserCommands(bot=MagicMock())

    # Seed subscription for user 101
    async with aiosqlite.connect(setup_test_db) as db:
        await db.execute("INSERT INTO subscriptions (user_id, game_id) VALUES (?, ?)", (101, 1))
        await db.commit()

    # Call command
    await cog.delete_my_subscriptions.callback(cog, interaction)

    # Verify confirmation prompt was sent ephemerally
    interaction.response.send_message.assert_awaited_once()
    call_kwargs = interaction.response.send_message.call_args.kwargs
    assert "Are you sure you want to delete your subscriptions" in interaction.response.send_message.call_args[0][0]
    assert call_kwargs.get("ephemeral") is True

    # Invoke confirm action via view
    view = call_kwargs.get("view")
    if hasattr(view, "on_confirm"):
        await view.on_confirm(interaction)

    # Verify subscriptions are deleted
    async with aiosqlite.connect(setup_test_db) as db:
        async with db.execute("SELECT * FROM subscriptions WHERE user_id = ?", (101,)) as cursor:
            rows = await cursor.fetchall()
    assert len(rows) == 0


@pytest.mark.asyncio
async def test_delete_all_my_data(setup_test_db):
    """Test delete_all_my_data removes all user-owned data upon confirmation."""
    interaction = make_mock_interaction(user_id=101)
    cog = UserCommands(bot=MagicMock())

    # Seed all types of user-owned data
    async with aiosqlite.connect(setup_test_db) as db:
        await db.execute(
            "INSERT INTO subscriptions (user_id, game_id) VALUES (?, ?)",
            (101, 1)
        )
        await db.execute(
            """
            INSERT OR REPLACE INTO user_settings
                (user_id, guild_id, ping_template)
            VALUES (?, 0, ?)
            """,
            (101, "Custom template")
        )

        # Seed multiple per-server quiet-mode overrides
        await db.execute(
            """
            INSERT INTO user_guild_settings
                (user_id, guild_id, quiet_mode)
            VALUES (?, ?, ?)
            """,
            (101, 999, 1)
        )
        await db.execute(
            """
            INSERT INTO user_guild_settings
                (user_id, guild_id, quiet_mode)
            VALUES (?, ?, ?)
            """,
            (101, 1234, 0)
        )

        await db.commit()

    await cog.delete_all_my_data.callback(cog, interaction)

    interaction.response.send_message.assert_awaited_once()
    view = interaction.response.send_message.call_args.kwargs.get("view")

    if hasattr(view, "on_confirm"):
        await view.on_confirm(interaction)

    # Verify ALL user-owned data is gone
    async with aiosqlite.connect(setup_test_db) as db:
        async with db.execute(
                "SELECT * FROM subscriptions WHERE user_id = ?",
                (101,)
        ) as cursor:
            sub_rows = await cursor.fetchall()

        async with db.execute(
                "SELECT * FROM user_settings WHERE user_id = ?",
                (101,)
        ) as cursor:
            setting_rows = await cursor.fetchall()

        async with db.execute(
                "SELECT * FROM user_guild_settings WHERE user_id = ?",
                (101,)
        ) as cursor:
            guild_setting_rows = await cursor.fetchall()

    assert len(sub_rows) == 0
    assert len(setting_rows) == 0
    assert len(guild_setting_rows) == 0


@pytest.mark.asyncio
@patch("cogs.user_cmds.fetch_game_details_from_igdb", new_callable=AsyncMock)
@patch("cogs.user_cmds.resolve_to_base_game", new_callable=AsyncMock)
async def test_game_info_success(mock_resolve, mock_fetch_details, setup_test_db):
    """Test game_info command successfully fetches details and builds an embed with server stats."""
    mock_guild = MagicMock()
    mock_guild.get_member.side_effect = lambda uid: MagicMock() if uid == 202 else None

    interaction = make_mock_interaction(user_id=101, guild_id=999)
    interaction.guild = mock_guild
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    # Mock IGDB game details payload
    mock_fetch_details.return_value = {
        "id": 1,
        "name": "Portal 2",
        "summary": "A puzzle platformer game.",
        "rating": 95.4,
        "url": "https://igdb.com/portal-2",
        "genres": [{"name": "Platform"}],
        "platforms": [{"name": "PC"}],
        "first_release_date": 1302835200,
        "cover": {"image_id": "co1234"}
    }
    mock_resolve.return_value = (1, "Portal 2")

    # Seed subscriber (user 202) for game id 1
    async with aiosqlite.connect(setup_test_db) as db:
        await db.execute("INSERT INTO subscriptions (user_id, game_id) VALUES (?, ?)", (202, 1))
        await db.commit()

    cog = UserCommands(bot=MagicMock())
    await cog.game_info.callback(cog, interaction, game="Portal 2", public=False)

    # Verify defer and followup embed transmission
    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    interaction.followup.send.assert_awaited_once()

    call_kwargs = interaction.followup.send.call_args.kwargs
    embed = call_kwargs.get("embed")

    assert embed is not None
    assert embed.title == "Portal 2"
    assert "A puzzle platformer game." in embed.description
    assert call_kwargs.get("ephemeral") is True

# -----------------------------------------------------------------------------
# Tests for Subscribed Game Indicator Commands
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_set_subscribed_indicator_too_long(setup_test_db):
    """Test that indicators exceeding 10 characters are rejected."""
    interaction = make_mock_interaction(user_id=101)
    cog = UserCommands(bot=MagicMock())

    long_indicator = "A" * 11

    await cog.set_subscribed_indicator.callback(cog, interaction, indicator=long_indicator)

    interaction.response.send_message.assert_called_once_with(
        "Indicator is too long! Keep it under 10 characters.",
        ephemeral=True
    )


@pytest.mark.asyncio
async def test_set_subscribed_indicator_success(setup_test_db):
    """Test setting a valid custom subscribed indicator."""
    interaction = make_mock_interaction(user_id=101)
    cog = UserCommands(bot=MagicMock())

    await cog.set_subscribed_indicator.callback(cog, interaction, indicator="🔥")

    # Verify database update (using guild_id = 0 for global user settings)
    async with aiosqlite.connect(setup_test_db) as db:
        async with db.execute("SELECT sub_indicator FROM user_settings WHERE user_id = ?", (101,)) as cursor:
            row = await cursor.fetchone()

    assert row is not None
    assert row[0] == "🔥"


@pytest.mark.asyncio
async def test_view_subscribed_indicator_default(setup_test_db):
    """Test view_subscribed_indicator defaults to '⭐' when none is set."""
    interaction = make_mock_interaction(user_id=101)
    cog = UserCommands(bot=MagicMock())

    await cog.view_subscribed_indicator.callback(cog, interaction)

    interaction.response.send_message.assert_awaited_once()
    msg = interaction.response.send_message.call_args[0][0]

    assert "`⭐`" in msg
    assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_view_subscribed_indicator_custom(setup_test_db):
    """Test view_subscribed_indicator displays the user's custom indicator."""
    interaction = make_mock_interaction(user_id=101)
    cog = UserCommands(bot=MagicMock())

    async with aiosqlite.connect(setup_test_db) as db:
        await db.execute(
            "INSERT OR REPLACE INTO user_settings (user_id, guild_id, sub_indicator) VALUES (?, 0, ?)",
            (101, "[SUB]")
        )
        await db.commit()

    await cog.view_subscribed_indicator.callback(cog, interaction)

    interaction.response.send_message.assert_awaited_once()
    msg = interaction.response.send_message.call_args[0][0]

    assert "`[SUB]`" in msg
    assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_reset_subscribed_indicator(setup_test_db):
    """Test reset_subscribed_indicator prompts confirmation and resets indicator to '⭐'."""
    interaction = make_mock_interaction(user_id=101)
    cog = UserCommands(bot=MagicMock())

    # Seed a custom indicator first
    async with aiosqlite.connect(setup_test_db) as db:
        await db.execute(
            "INSERT OR REPLACE INTO user_settings (user_id, guild_id, sub_indicator) VALUES (?, 0, ?)",
            (101, "🔥")
        )
        await db.commit()

    # Call reset command
    await cog.reset_subscribed_indicator.callback(cog, interaction)

    # Verify confirmation prompt
    interaction.response.send_message.assert_awaited_once()
    call_kwargs = interaction.response.send_message.call_args.kwargs
    assert "Are you sure you want to reset your subscribed game indicator" in interaction.response.send_message.call_args[0][0]
    assert call_kwargs.get("ephemeral") is True

    # Invoke confirm action via view
    view = call_kwargs.get("view")
    if hasattr(view, "on_confirm"):
        await view.on_confirm(interaction)

    # Verify database record is reset to '⭐'
    async with aiosqlite.connect(setup_test_db) as db:
        async with db.execute("SELECT sub_indicator FROM user_settings WHERE user_id = ?", (101,)) as cursor:
            row = await cursor.fetchone()

    assert row is not None
    assert row[0] == "⭐"

@pytest.mark.parametrize("message", [
    "short ping <@123>",
    "x" * 2000,
    "x" * 2001,
    "x" * 1995 + "[mentions]",
    "long note " + "x" * 10000,
    " ".join(f"<@{100000000000000000 + i}>" for i in range(1500)),
    ], ids=[
    "short",
    "exact-limit",
    "overflow",
    "mention-at-boundary",
    "long-note",
    "1500-subscribers",
])
def test_split_ping_message_preserves_content_and_mentions(message):
    batches = split_ping_message(message)
    prefix = "**LFG ping continued:**\n"
    assert all(0 < len(batch) <= 2000 for batch in batches)
    assert all(batch.startswith(prefix) for batch in batches[1:])
    assert batches[0] + "".join(batch[len(prefix):] for batch in batches[1:]) == message
    assert re.findall(r"<@!?\d+>", message) == [
        mention for batch in batches for mention in re.findall(r"<@!?\d+>", batch)
    ]
    assert all(len(set(re.findall(r"<@!?(\d+)>", batch))) <= 100 for batch in batches)
    if len(message) <= 2000:
        assert batches == [message]


@pytest.mark.asyncio
@pytest.mark.parametrize("partial_failure", [False, True], ids=["complete", "partial-failure"])
async def test_ping_game_many_batches_preserves_quiet_mode_and_notifies_once(monkeypatch, partial_failure):
    from operations import OperationalStats
    counters = OperationalStats()
    monkeypatch.setattr("cogs.user_cmds.stats", counters)
    cog = UserCommands(bot=MagicMock())
    interaction = make_mock_interaction(user_id=101)
    if partial_failure:
        interaction.channel.send.side_effect = [None, RuntimeError("send failed")]
    ids = list(range(100000000000000000, 100000000000000400))
    rows = [(uid, i % 2) for i, uid in enumerate(ids)]
    interaction.guild.get_member = lambda uid: MagicMock(id=uid, display_name=f"Player-{uid}")
    # Duplicate placeholders exercise repeated subscriber mentions across batches.
    template = "{caller} {game}: {mentions}\n{mentions}\n{note}"
    cursor_game = MagicMock()
    cursor_game.fetchone = AsyncMock(return_value=(1, "Portal 2"))
    cursor_rows = MagicMock()
    cursor_rows.fetchall = AsyncMock(return_value=rows)
    cursor_template = MagicMock()
    cursor_template.fetchone = AsyncMock(return_value=(template,))
    contexts = []
    for cursor in (cursor_game, cursor_rows, cursor_template):
        context = AsyncMock()
        context.__aenter__.return_value = cursor
        contexts.append(context)
    db = MagicMock()
    db.execute.side_effect = contexts
    connection = AsyncMock()
    connection.__aenter__.return_value = db
    with patch("cogs.user_cmds.aiosqlite.connect", return_value=connection), \
            patch("cogs.user_cmds.is_lfg_allowed_in_channel", new=AsyncMock(return_value=True)), \
            patch("cogs.user_cmds.can_user_ping_game", new=AsyncMock(return_value=(True, []))), \
            patch("cogs.user_cmds.resolve_to_base_game", new=AsyncMock(return_value=(1, "Portal 2"))), \
            patch("cogs.user_cmds.claim_game_ping_cooldown", new=AsyncMock(return_value=(True, 0))):
        if partial_failure:
            with pytest.raises(RuntimeError, match="send failed"):
                await cog.ping_game.callback(cog, interaction, game="Portal 2", note="Let's play! <@999999999999999999> <@&777> @everyone")
            assert counters.counters["pings_sent"] == 0
            interaction.delete_original_response.assert_not_awaited()
            return
        await cog.ping_game.callback(cog, interaction, game="Portal 2", note="Let's play! <@999999999999999999> <@&777> @everyone")
    assert counters.counters["pings_sent"] == 1
    interaction.followup.send.assert_not_awaited()
    interaction.delete_original_response.assert_awaited_once()
    calls = interaction.channel.send.call_args_list
    assert len(calls) > 2
    notified = []
    for i, call in enumerate(calls):
        message = call.args[0]
        assert len(message) <= 2000
        if i:
            assert message.startswith("**LFG ping continued:**\n")
        assert call.kwargs["silent"] is False
        allowed = call.kwargs["allowed_mentions"]
        assert allowed.roles is False and allowed.everyone is False
        assert "<@999999999999999999>" not in message
        batch_ids = [int(uid) for uid in re.findall(r"<@!?(\d+)>", message)]
        assert len(set(batch_ids)) <= 100
        assert all(f"<@{uid}>" in message for uid in batch_ids)
        allowed_ids = [user.id for user in allowed.users]
        assert set(allowed_ids) == (set(batch_ids) & set(ids[::2]))
        assert 101 not in allowed_ids
        notified.extend(allowed_ids)
    assert '<@101>' in calls[0].args[0]
    assert sorted(notified) == ids[::2]
    text = calls[0].args[0] + "".join(call.args[0].removeprefix("**LFG ping continued:**\n") for call in calls[1:])
    assert all(f"<@{uid}>" in text for uid in ids[::2])
    assert all(f"<@{uid}>" in text for uid in ids[1::2])
    assert all(f"Player-{uid}" not in text for uid in ids[1::2])
    assert "Let's play!" in text


# -----------------------------------------------------------------------------
# /ping_game scheduled timestamp feature
# -----------------------------------------------------------------------------

def test_ping_game_has_only_optional_time_no_format_option():
    import inspect
    command = UserCommands.ping_game
    params = inspect.signature(command.callback).parameters
    assert 'time' in params
    assert 'time_format' not in params
    option_names = {param.name for param in command.parameters}
    assert 'time' in option_names
    assert 'time_format' not in option_names

@pytest.mark.asyncio
async def test_autocomplete_ping_time_presets_and_custom_input():
    from cogs.user_cmds import autocomplete_ping_time
    choices = await autocomplete_ping_time(None, '')
    assert any(choice.value == '30m' for choice in choices)
    assert len(choices) <= 25
    assert (await autocomplete_ping_time(None, '45m'))[0].value == '45m'
    assert (await autocomplete_ping_time(None, 'tomorrow 20:00')) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('source', [
    '1h30m',
    '2100-01-02T20:00+03:00',
    '<t:4102444800:F>',  # A pasted full-date timestamp should still render relatively.
])
async def test_ping_game_scheduled_always_relative(setup_test_db, source):
    cog = UserCommands(bot=MagicMock())
    interaction = make_mock_interaction(user_id=101)
    interaction.guild.get_member = lambda uid: MagicMock(id=uid, display_name='Player')
    async with aiosqlite.connect(setup_test_db) as db:
        await db.execute('INSERT INTO subscriptions (user_id, game_id) VALUES (?, ?)', (202, 1))
        await db.commit()
    with patch('cogs.user_cmds.is_lfg_allowed_in_channel', new=AsyncMock(return_value=True)), \
         patch('cogs.user_cmds.claim_game_ping_cooldown', new=AsyncMock(return_value=(True, 0))), \
         patch('cogs.user_cmds.resolve_to_base_game', new=AsyncMock(return_value=(1, 'Portal 2'))):
        await cog.ping_game.callback(cog, interaction, game='Portal 2', time=source)
    sent = interaction.channel.send.await_args.args[0]
    assert re.search(r'<t:\d+:[tTdDfFsSR]>', sent)
    assert re.search(r'<t:\d+:R>', sent)
    assert ':F>' not in sent
    assert 'Starting' in sent
    assert '<@202>' in sent
    assert [user.id for user in interaction.channel.send.await_args.kwargs['allowed_mentions'].users] == [202]


@pytest.mark.asyncio
async def test_ping_game_custom_template_has_explicit_time_placeholder(setup_test_db):
    cog = UserCommands(bot=MagicMock())
    interaction = make_mock_interaction(user_id=101)
    interaction.guild.get_member = lambda uid: MagicMock(id=uid, display_name='Player')
    async with aiosqlite.connect(setup_test_db) as db:
        await db.execute('INSERT INTO subscriptions VALUES (202, 1)')
        await db.execute("INSERT INTO user_settings (user_id, guild_id, ping_template) VALUES (101, 0, 'Join {game} at {time}: {mentions}')")
        await db.commit()
    with patch('cogs.user_cmds.is_lfg_allowed_in_channel', new=AsyncMock(return_value=True)), \
         patch('cogs.user_cmds.claim_game_ping_cooldown', new=AsyncMock(return_value=(True, 0))), \
         patch('cogs.user_cmds.resolve_to_base_game', new=AsyncMock(return_value=(1, 'Portal 2'))):
        await cog.ping_game.callback(cog, interaction, game='Portal 2', time='2h')
    sent = interaction.channel.send.await_args.args[0]
    assert 'Join **Portal 2** at <t:' in sent
    assert 'Starting' not in sent
    assert '<@202>' in sent


@pytest.mark.asyncio
async def test_ping_game_invalid_time_does_not_send_or_claim_cooldown(setup_test_db):
    cog = UserCommands(bot=MagicMock())
    interaction = make_mock_interaction(user_id=101)
    with patch('cogs.user_cmds.claim_game_ping_cooldown', new=AsyncMock()) as claim:
        await cog.ping_game.callback(cog, interaction, game='Portal 2', time='tomorrow 8pm')
        claim.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once()
    assert 'Invalid start time' in interaction.response.send_message.await_args.args[0]
    interaction.response.defer.assert_not_awaited()
    interaction.channel.send.assert_not_awaited()


def test_split_ping_message_keeps_timestamp_token_whole():
    from cogs.user_cmds import split_ping_message
    ts = '<t:1791849600:F>'
    input_text = 'a' * 1987 + ts + ' end'
    chunks = split_ping_message(input_text)
    assert len(chunks) == 2
    assert ts in chunks[1]
    assert all(len(chunk) <= 2000 for chunk in chunks)


@pytest.mark.asyncio
async def test_ping_game_preserves_caller_mention_without_ping_even_if_subscribed(setup_test_db):
    """The caller may be subscribed, but only other active members are notified."""
    cog = UserCommands(bot=MagicMock())
    interaction = make_mock_interaction(user_id=101)
    interaction.guild.get_member = lambda uid: MagicMock(id=uid, display_name=f'Player-{uid}')
    async with aiosqlite.connect(setup_test_db) as db:
        await db.executemany('INSERT INTO subscriptions (user_id, game_id) VALUES (?, ?)',
                             [(101, 1), (202, 1)])
        await db.commit()
    with patch('cogs.user_cmds.is_lfg_allowed_in_channel', new=AsyncMock(return_value=True)), \
         patch('cogs.user_cmds.claim_game_ping_cooldown', new=AsyncMock(return_value=(True, 0))), \
         patch('cogs.user_cmds.resolve_to_base_game', new=AsyncMock(return_value=(1, 'Portal 2'))):
        await cog.ping_game.callback(cog, interaction, game='Portal 2',
                                     note='Join me <@101> <@999> <@202>')
    sent = interaction.channel.send.await_args.args[0]
    assert sent.count('<@101>') == 2  # Caller + explicit caller mention in note.
    assert sent.count('<@202>') == 1  # Subscriber not pinged twice.
    assert '<@999>' not in sent  # Arbitrary mentions are sanitized.
    allowed = interaction.channel.send.await_args.kwargs['allowed_mentions']
    assert [member.id for member in allowed.users] == [202]
    assert allowed.roles is False and allowed.everyone is False
    assert allowed.replied_user is False


def test_ping_game_note_and_time_remain_optional():
    options = {option["name"]: option for option in UserCommands.ping_game.to_dict(MagicMock())["options"]}
    assert not options["note"]["required"]
    assert not options["time"]["required"]


@pytest.mark.asyncio
async def test_help_fields_fit_with_real_command_mentions():
    bot = MagicMock()
    bot.command_ids = {command.name: 1234567890123456789 for command in UserCommands.__cog_app_commands__}
    bot.command_ids["config"] = 1234567890123456789
    interaction = make_mock_interaction()
    await UserCommands.help_command.callback(UserCommands(bot), interaction)
    embed = interaction.response.send_message.await_args.kwargs["embed"]
    assert all(len(field.value) <= 1024 for field in embed.fields)
    assert len(embed) <= 6000


@pytest.mark.asyncio
@pytest.mark.parametrize("template,missing", [
    ("Anyone playing?", ["{caller}", "{game}", "{mentions}"]),
    ("{game} {mentions}", ["{caller}"]),
    ("{caller} {mentions}", ["{game}"]),
    ("{caller} {game}", ["{mentions}"]),
    ("{user} {game} {mentions}", []),
    ("{mentions} hosted by {caller}: {game}", []),
    ("", ["{caller}", "{game}", "{mentions}"]),
])
async def test_template_save_and_view_show_same_fallbacks(setup_test_db, template, missing):
    from cogs.user_cmds import render_ping_template
    cog = UserCommands(bot=MagicMock())
    interaction = make_mock_interaction(user_id=101)
    with patch("cogs.user_cmds.time.time", return_value=1700000000):
        await cog.set_ping_template.callback(cog, interaction, template)
        saved = interaction.response.send_message.await_args
        interaction.response.send_message.reset_mock()
        await cog.view_ping_template.callback(cog, interaction)
    viewed = interaction.response.send_message.await_args
    expected, warning = render_ping_template(template, caller="<@101>", game="**[game]**",
        mentions="[mentions]", note="[note]", scheduled_time="<t:1700007200:R>")
    for call in (saved, viewed):
        assert expected in call.args[0]
        assert "⚠️ Missing tags:" in call.args[0] if missing else "⚠️" not in call.args[0]
        assert warning in call.args[0]
        assert call.kwargs["allowed_mentions"].users is False
        assert call.kwargs["ephemeral"] is True
    assert "<@101>" in expected and "**[game]**" in expected and "[mentions]" in expected
    async with aiosqlite.connect(setup_test_db) as db:
        assert (await (await db.execute("SELECT ping_template FROM user_settings WHERE user_id=101")).fetchone())[0] == template


@pytest.mark.asyncio
async def test_legacy_ping_template_gets_attribution_game_and_mentions(setup_test_db):
    cog = UserCommands(bot=MagicMock())
    interaction = make_mock_interaction(user_id=101)
    async with aiosqlite.connect(setup_test_db) as db:
        await db.execute("INSERT INTO subscriptions VALUES (202, 1)")
        await db.execute("INSERT INTO user_settings (user_id, guild_id, ping_template) VALUES (101, 0, 'Anyone up for a game?')")
        await db.commit()
    with patch("cogs.user_cmds.is_lfg_allowed_in_channel", new=AsyncMock(return_value=True)), \
            patch("cogs.user_cmds.resolve_to_base_game", new=AsyncMock(return_value=(1, "Portal 2"))):
        await cog.ping_game.callback(cog, interaction, game="Portal 2")
    call = interaction.channel.send.await_args
    assert "Hosted by <@101> · Game: **Portal 2**" in call.args[0]
    assert "<@202>" in call.args[0]
    assert [user.id for user in call.kwargs["allowed_mentions"].users] == [202]


def test_template_tag_positions_alias_and_literal_note_are_preserved():
    from cogs.user_cmds import render_ping_template
    message, warning = render_ping_template("{mentions} {game} by {user}: {note}", caller="CALLER",
        game="GAME", mentions="SUBS", note="literal {caller}")
    assert message == "SUBS GAME by CALLER: literal {caller}"
    assert warning == ""


# -----------------------------------------------------------------------------
# Ping body / details / footer layout
# -----------------------------------------------------------------------------

@pytest.mark.parametrize("note", ["", "Bring snacks"])
@pytest.mark.parametrize("scheduled_time", ["", "<t:4102444800:R>"])
@pytest.mark.parametrize("mentions", ["", "<@202> <@203>"])
@pytest.mark.parametrize("template,attribution", [
    ("Let's play!", "-# Hosted by <@101> · Game: **Portal 2**"),
    ("{caller} wants to play {game}!", ""),
])
def test_exact_ping_section_layout(template, attribution, note, scheduled_time, mentions):
    from cogs.user_cmds import render_ping_template
    actual, _ = render_ping_template(template, caller="<@101>", game="**Portal 2**",
        mentions=mentions, note=note, scheduled_time=scheduled_time)
    body = "Let's play!" if attribution else "<@101> wants to play **Portal 2**!"
    details = []
    if note:
        details.append("> Bring snacks")
    if scheduled_time:
        details.append("*Starting <t:4102444800:R>*")
    footer = []
    if mentions:
        footer.append("-# <@202> <@203>")
    if attribution:
        footer.append(attribution)
    expected = body
    if details:
        expected += "\n\n" + "\n".join(details)
    if footer:
        expected += "\n\n" + "\n".join(footer)
    assert actual == expected
    assert " · -#" not in actual and "\n\n\n" not in actual


def test_explicit_tags_keep_authored_positions_and_optional_whitespace_is_empty():
    from cogs.user_cmds import render_ping_template
    template = "{mentions} {game} by {user} at {time}\n{note}"
    actual, warning = render_ping_template(template, caller="HOST", game="GAME", mentions="PLAYERS",
        note="NOTE", scheduled_time="TIME")
    assert actual == "PLAYERS GAME by HOST at TIME\nNOTE"
    assert warning == ""
    actual, _ = render_ping_template("{caller} {game} {mentions}", caller="HOST", game="GAME",
        mentions="PLAYERS", note="   ")
    assert actual == "HOST GAME PLAYERS"


def test_empty_template_has_no_leading_blank_lines_or_stray_subtext():
    from cogs.user_cmds import render_ping_template, ping_template_fallbacks
    actual, _ = render_ping_template("", caller="HOST", game="GAME", mentions="")
    assert actual == "-# Hosted by HOST · Game: GAME"
    effective, missing = ping_template_fallbacks("Hi!")
    assert effective == "Hi!\n\n-# Subscribers: {mentions}\n-# Hosted by {caller} · Game: {game}"
    assert missing == ["{caller}", "{game}", "{mentions}"]


@pytest.mark.asyncio
@pytest.mark.parametrize("custom", [False, True])
@pytest.mark.parametrize("has_details", [False, True])
@pytest.mark.parametrize("has_quiet", [False, True])
async def test_actual_ping_uses_compact_footer_and_preserves_notifications(
        setup_test_db, custom, has_details, has_quiet):
    cog = UserCommands(bot=MagicMock())
    interaction = make_mock_interaction(user_id=101)
    async with aiosqlite.connect(setup_test_db) as db:
        await db.execute("INSERT INTO subscriptions VALUES (203, 1)")
        if has_quiet:
            await db.execute("INSERT INTO subscriptions VALUES (202, 1)")
            await db.execute("INSERT INTO user_settings (user_id, guild_id, quiet_mode) VALUES (202, 0, 1)")
        if custom:
            await db.execute("INSERT INTO user_settings (user_id, guild_id, ping_template) VALUES (101, 0, 'Let us play!')")
        await db.commit()
    with patch("cogs.user_cmds.is_lfg_allowed_in_channel", new=AsyncMock(return_value=True)), \
            patch("cogs.user_cmds.resolve_to_base_game", new=AsyncMock(return_value=(1, "Portal 2"))), \
            patch("cogs.user_cmds.parse_ping_time", return_value=(4102444800, None)):
        await cog.ping_game.callback(cog, interaction, game="Portal 2",
            note="Bring snacks" if has_details else None, time="30m" if has_details else None)
    call = interaction.channel.send.await_args
    expected = "Let us play!" if custom else "<@101> wants to play **Portal 2**!"
    if has_details:
        expected += "\n\n> Bring snacks\n*Starting <t:4102444800:R>*"
    expected += "\n\n-# <@203>"
    if custom:
        expected += "\n-# Hosted by <@101> · Game: **Portal 2**"
    if has_quiet:
        expected += "\n-# *<@202> has quiet mode turned on*"
    assert call.args[0] == expected
    assert [user.id for user in call.kwargs["allowed_mentions"].users] == [203]
    assert call.kwargs["allowed_mentions"].roles is False
    assert call.kwargs["allowed_mentions"].everyone is False
    assert call.kwargs["silent"] is False


@pytest.mark.asyncio
async def test_quiet_only_footer_has_one_separator(setup_test_db):
    cog = UserCommands(bot=MagicMock())
    interaction = make_mock_interaction(user_id=101)
    async with aiosqlite.connect(setup_test_db) as db:
        await db.execute("INSERT INTO subscriptions VALUES (202, 1)")
        await db.execute("INSERT INTO user_settings (user_id, guild_id, quiet_mode) VALUES (202, 0, 1)")
        await db.commit()
    with patch("cogs.user_cmds.is_lfg_allowed_in_channel", new=AsyncMock(return_value=True)), \
            patch("cogs.user_cmds.resolve_to_base_game", new=AsyncMock(return_value=(1, "Portal 2"))):
        await cog.ping_game.callback(cog, interaction, game="Portal 2")
    call = interaction.channel.send.await_args
    assert call.args[0] == "<@101> wants to play **Portal 2**!\n\n-# *<@202> has quiet mode turned on*"
    assert call.kwargs["allowed_mentions"].users == []
