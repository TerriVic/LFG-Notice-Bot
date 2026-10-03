import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import discord
import aiosqlite

from utils import autocomplete_games, GenericConfirmationView, autocomplete_whitelisted_channels_only, \
    autocomplete_text_channels, autocomplete_set_lfg_channels, resolve_to_base_game, fetch_and_cache_game_from_igdb, \
    fetch_game_details_from_igdb, is_lfg_allowed_in_channel


@pytest.fixture
async def setup_test_db(tmp_path, monkeypatch):
    """Fixture to set up a temporary SQLite test database and patch DB_PATH in utils."""
    db_file = tmp_path / "test_bot.db"
    db_path = str(db_file)

    # Patch DB_PATH in your utils module so aiosqlite.connect(DB_PATH) uses the test db
    monkeypatch.setattr("utils.DB_PATH", db_path)

    # Create the required tables with composite keys/schema
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
                         CREATE TABLE IF NOT EXISTS user_settings (
                                                                      user_id INTEGER,
                                                                      guild_id INTEGER DEFAULT 0,
                                                                      sub_indicator TEXT DEFAULT '⭐',
                                                                      PRIMARY KEY (user_id, guild_id)
                         )
                         """)
        await db.execute("""
                         CREATE TABLE IF NOT EXISTS igdb_games (
                                                                   id INTEGER PRIMARY KEY,
                                                                   name TEXT NOT NULL,
                                                                   is_multiplayer INTEGER DEFAULT 0,
                                                                   total_rating_count INTEGER DEFAULT 0,
                                                                   category INTEGER DEFAULT 0,
                                                                   parent_game_id INTEGER,
                                                                   version_parent_id INTEGER,
                                                                   updated_at INTEGER
                         )
                         """)
        await db.execute("""
                         CREATE TABLE IF NOT EXISTS subscriptions (
                                                                      user_id INTEGER,
                                                                      game_id INTEGER,
                PRIMARY KEY (user_id, game_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS guild_lfg_settings (
                guild_id INTEGER PRIMARY KEY,
                mode TEXT DEFAULT 'whitelist',
                whitelisted_channels TEXT
                         )
                         """)
        await db.commit()

    yield db_path

# -----------------------------------------------------------------------------
# Autocomplete Tests
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_autocomplete_empty_default(setup_test_db):
    """Test autocomplete with empty input in a server context with members and default sorting."""
    # Seed games and user settings, and subscribe to Half-Life so it passes the filter
    async with aiosqlite.connect(setup_test_db) as db:
        await db.execute(
            "INSERT OR IGNORE INTO igdb_games (id, name, is_multiplayer, total_rating_count, updated_at) VALUES (1, 'Portal 2', 1, 100, 1600000000)"
        )
        await db.execute(
            "INSERT OR IGNORE INTO igdb_games (id, name, is_multiplayer, total_rating_count, updated_at) VALUES (2, 'Half-Life', 0, 50, 1600000000)"
        )
        # Subscribe user 101 to Half-Life so it qualifies for selection even though it's single-player (is_multiplayer = 0)
        await db.execute("INSERT OR IGNORE INTO subscriptions (user_id, game_id) VALUES (?, ?)", (101, 2))
        await db.execute(
            "INSERT OR REPLACE INTO user_settings (user_id, guild_id, sub_indicator) VALUES (?, 0, ?)",
            (101, "🔥")
        )
        await db.commit()

    # Mock interaction with guild and members
    mock_member = MagicMock()
    mock_member.id = 202

    mock_interaction = MagicMock(spec=discord.Interaction)
    mock_interaction.user.id = 101
    mock_interaction.guild = MagicMock()
    mock_interaction.guild.members = [mock_member]
    mock_interaction.command = MagicMock()
    mock_interaction.command.name = "ping_game"  # Triggers boost_subscribed = True, allowing subscribed SP games
    mock_interaction.namespace = None

    choices = await autocomplete_games(mock_interaction, "")

    assert len(choices) > 0
    # Subscribed single-player game should have both the custom indicator and [SP 👤] tag
    sp_choice = next((c for c in choices if "Half-Life" in c.name), None)
    assert sp_choice is not None
    assert "[SP 👤]" in sp_choice.name
    assert "🔥" in sp_choice.name

@pytest.mark.asyncio
async def test_autocomplete_ping_game_boost_and_namespace(setup_test_db):
    """Test autocomplete with ping_game command name boosting subscriptions and namespace user override."""
    async with aiosqlite.connect(setup_test_db) as db:
        await db.execute(
            "INSERT OR IGNORE INTO igdb_games (id, name, is_multiplayer, total_rating_count, updated_at) VALUES (1, 'Portal 2', 1, 100, 1600000000)"
        )
        # Subscribe target user 303
        await db.execute("INSERT INTO subscriptions (user_id, game_id) VALUES (?, ?)", (303, 1))
        await db.commit()

    target_user = MagicMock()
    target_user.id = 303

    mock_interaction = MagicMock(spec=discord.Interaction)
    mock_interaction.user.id = 101
    mock_interaction.guild = None  # DM context
    mock_interaction.command = MagicMock()
    mock_interaction.command.name = "ping_game"  # Triggers boost_subscribed = True
    mock_interaction.namespace = MagicMock()
    mock_interaction.namespace.user = target_user

    choices = await autocomplete_games(mock_interaction, "")

    assert len(choices) > 0
    # Portal 2 is subscribed by target user 303, so it should be boosted with default star '⭐ '
    assert any("⭐ Portal 2" in c.name for c in choices)


@pytest.mark.asyncio
async def test_autocomplete_search_with_text_and_dlc(setup_test_db):
    """Test autocomplete search with text query and DLC intent detection."""
    async with aiosqlite.connect(setup_test_db) as db:
        await db.execute(
            "INSERT OR IGNORE INTO igdb_games (id, name, is_multiplayer, total_rating_count, category, updated_at) VALUES (1, 'Portal 2', 1, 500, 0, 1600000000)"
        )
        await db.execute(
            "INSERT OR IGNORE INTO igdb_games (id, name, is_multiplayer, total_rating_count, category, updated_at) VALUES (2, 'Portal 2: Peer Review', 1, 200, 1, 1600000000)"
        )
        await db.commit()

    mock_interaction = MagicMock(spec=discord.Interaction)
    mock_interaction.user.id = 101
    mock_interaction.guild = None
    mock_interaction.command = None

    # Search with colon triggers DLC/expansion search intent
    choices = await autocomplete_games(mock_interaction, "Portal 2: Peer")

    assert len(choices) > 0
    assert any("Portal 2: Peer Review" in c.name for c in choices)


@pytest.mark.asyncio
@patch("aiohttp.ClientSession.post")
async def test_autocomplete_live_igdb_fetch(mock_post, setup_test_db):
    """Test autocomplete falling back to live IGDB API when local results are absent."""
    # Mock aiohttp response for IGDB API
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=[
        {"name": "Cyberpunk 2077", "total_rating_count": 1000, "category": 0, "game_modes": [1, 2]}
    ])

    mock_context_manager = AsyncMock()
    mock_context_manager.__aenter__.return_value = mock_resp
    mock_post.return_value = mock_context_manager

    mock_interaction = MagicMock(spec=discord.Interaction)
    mock_interaction.user.id = 101
    mock_interaction.guild = None
    mock_interaction.command = None

    with patch.dict(os.environ, {"TWITCH_CLIENT_ID": "mock_id", "TWITCH_ACCESS_TOKEN": "mock_token"}):
        choices = await autocomplete_games(mock_interaction, "Cyberpunk")

    assert len(choices) == 1
    assert choices[0].name == "Cyberpunk 2077"
    assert choices[0].value == "Cyberpunk 2077"


@pytest.mark.asyncio
async def test_autocomplete_discord_exception_handling(setup_test_db):
    """Test autocomplete safely catches Discord HTTP/NotFound exceptions and returns empty list."""
    mock_interaction = MagicMock(spec=discord.Interaction)
    mock_interaction.user.id = 101
    mock_interaction.namespace = None  # Explicitly prevent MagicMock from mocking namespace
    mock_interaction.command = None

    # Force an exception during interaction processing by making guild raise discord.HTTPException
    type(mock_interaction).guild = property(
        lambda self: (_ for _ in ()).throw(discord.HTTPException(MagicMock(), "Error"))
    )

    choices = await autocomplete_games(mock_interaction, "Portal")
    assert choices == []

# -----------------------------------------------------------------------------
# UI View Confirmation Test
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_generic_confirmation_view_confirm():
    """Test confirmation button interaction inside GenericConfirmationView."""
    callback_executed = False

    async def mock_on_confirm(interaction: discord.Interaction):
        nonlocal callback_executed
        callback_executed = True

    view = GenericConfirmationView(
        author_id=12345,
        on_confirm=mock_on_confirm
    )

    interaction = MagicMock(spec=discord.Interaction)
    interaction.user.id = 12345
    interaction.response.edit_message = AsyncMock()

    button = view.children[0]
    await button.callback(interaction)

    assert callback_executed is True
    assert view.is_finished()
    interaction.response.edit_message.assert_called_once_with(
        content=view.confirm_message,
        view=None
    )

# -----------------------------------------------------------------------------
# Channel Autocomplete Tests
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_autocomplete_set_lfg_channels_no_guild():
    """Test autocomplete_set_lfg_channels returns empty list in DM context (no guild)."""
    mock_interaction = MagicMock(spec=discord.Interaction)
    mock_interaction.guild = None

    choices = await autocomplete_set_lfg_channels(mock_interaction, "")
    assert choices == []


@pytest.mark.asyncio
async def test_autocomplete_set_lfg_channels_with_whitelists(setup_test_db):
    """Test autocomplete_set_lfg_channels correctly flags whitelisted channels and filters by input."""
    async with aiosqlite.connect(setup_test_db) as db:
        await db.execute(
            "INSERT OR REPLACE INTO guild_lfg_settings (guild_id, whitelisted_channels) VALUES (?, ?)",
            (12345, "111,333")
        )
        await db.commit()

    # Mock text channels
    ch1 = MagicMock(spec=discord.TextChannel)
    ch1.id = 111
    ch1.name = "general"

    ch2 = MagicMock(spec=discord.TextChannel)
    ch2.id = 222
    ch2.name = "gaming"

    ch3 = MagicMock(spec=discord.TextChannel)
    ch3.id = 333
    ch3.name = "lfg-chat"

    mock_guild = MagicMock()
    mock_guild.id = 12345
    mock_guild.text_channels = [ch1, ch2, ch3]

    mock_interaction = MagicMock(spec=discord.Interaction)
    mock_interaction.guild = mock_guild

    # Test empty query (should return all channels with ✅ on whitelisted ones)
    choices = await autocomplete_set_lfg_channels(mock_interaction, "")
    assert len(choices) == 3

    gen_choice = next(c for c in choices if c.value == "111")
    game_choice = next(c for c in choices if c.value == "222")

    assert gen_choice.name == "✅ #general"
    assert game_choice.name == "#gaming"  # Not whitelisted, no checkmark

    # Test filtering by query text
    filtered_choices = await autocomplete_set_lfg_channels(mock_interaction, "lfg")
    assert len(filtered_choices) == 1
    assert filtered_choices[0].value == "333"
    assert "✅" in filtered_choices[0].name


@pytest.mark.asyncio
async def test_autocomplete_whitelisted_channels_only_fallbacks(setup_test_db):
    """Test autocomplete_whitelisted_channels_only returns appropriate fallback message when no channels are whitelisted."""
    mock_guild = MagicMock()
    mock_guild.id = 12345
    mock_guild.text_channels = []

    mock_interaction = MagicMock(spec=discord.Interaction)
    mock_interaction.guild = mock_guild

    # Case 1: No DB row / empty row
    choices = await autocomplete_whitelisted_channels_only(mock_interaction, "")
    assert len(choices) == 1
    assert choices[0].name == "No channels currently whitelisted"
    assert choices[0].value == "__NONE__"

    # Case 2: DB row exists but has empty string
    async with aiosqlite.connect(setup_test_db) as db:
        await db.execute(
            "INSERT OR REPLACE INTO guild_lfg_settings (guild_id, whitelisted_channels) VALUES (?, ?)",
            (12345, "")
        )
        await db.commit()

    choices2 = await autocomplete_whitelisted_channels_only(mock_interaction, "")
    assert choices2[0].name == "No channels currently whitelisted"


@pytest.mark.asyncio
async def test_autocomplete_whitelisted_channels_only_matching(setup_test_db):
    """Test autocomplete_whitelisted_channels_only filters correctly and returns no match fallback if necessary."""
    async with aiosqlite.connect(setup_test_db) as db:
        await db.execute(
            "INSERT OR REPLACE INTO guild_lfg_settings (guild_id, whitelisted_channels) VALUES (?, ?)",
            (12345, "111,333")
        )
        await db.commit()

    ch1 = MagicMock(spec=discord.TextChannel)
    ch1.id = 111
    ch1.name = "general"

    ch2 = MagicMock(spec=discord.TextChannel)
    ch2.id = 222
    ch2.name = "gaming"  # Whitelisted ID doesn't include this

    mock_guild = MagicMock()
    mock_guild.id = 12345
    mock_guild.text_channels = [ch1, ch2]

    mock_interaction = MagicMock(spec=discord.Interaction)
    mock_interaction.guild = mock_guild

    # Query matching whitelisted channel
    choices = await autocomplete_whitelisted_channels_only(mock_interaction, "gen")
    assert len(choices) == 1
    assert choices[0].value == "111"
    assert choices[0].name == "#general"

    # Query matching non-whitelisted channel or non-existent term
    no_match_choices = await autocomplete_whitelisted_channels_only(mock_interaction, "gaming")
    assert len(no_match_choices) == 1
    assert no_match_choices[0].name == "No matching whitelisted channels found"
    assert no_match_choices[0].value == "__NONE__"


@pytest.mark.asyncio
async def test_autocomplete_text_channels():
    """Test standard autocomplete_text_channels filtering by name and channel ID."""
    ch1 = MagicMock(spec=discord.TextChannel)
    ch1.id = 456789
    ch1.name = "announcements"

    ch2 = MagicMock(spec=discord.TextChannel)
    ch2.id = 123456
    ch2.name = "chat"

    mock_guild = MagicMock()
    mock_guild.text_channels = [ch1, ch2]

    mock_interaction = MagicMock(spec=discord.Interaction)
    mock_interaction.guild = mock_guild

    # Search by text name prefix/substring
    choices_name = await autocomplete_text_channels(mock_interaction, "announce")
    assert len(choices_name) == 1
    assert choices_name[0].name == "#announcements"
    assert choices_name[0].value == "456789"

    # Search by channel ID substring
    choices_id = await autocomplete_text_channels(mock_interaction, "1234")
    assert len(choices_id) == 1
    assert choices_id[0].name == "#chat"
    assert choices_id[0].value == "123456"

    # No guild context returns empty list
    mock_interaction.guild = None
    assert await autocomplete_text_channels(mock_interaction, "chat") == []

# -----------------------------------------------------------------------------
# IGDB API Integration Tests
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_game_details_missing_credentials(monkeypatch):
    """Test fetch_game_details_from_igdb returns None if environment credentials are missing."""
    monkeypatch.delenv("TWITCH_CLIENT_ID", raising=False)
    monkeypatch.delenv("TWITCH_ACCESS_TOKEN", raising=False)

    result = await fetch_game_details_from_igdb("Portal 2")
    assert result is None


@pytest.mark.asyncio
@patch("aiohttp.ClientSession.post")
async def test_fetch_game_details_success(mock_post, monkeypatch, setup_test_db):
    """Test fetch_game_details_from_igdb successfully queries and parses game details."""
    monkeypatch.setenv("TWITCH_CLIENT_ID", "mock_client_id")
    monkeypatch.setenv("TWITCH_ACCESS_TOKEN", "mock_token")

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=[{
        "name": "Portal 2",
        "summary": "Great puzzle game",
        "rating": 95.5,
        "genres": [{"name": "Puzzle"}],
        "platforms": [{"name": "PC"}],
        "first_release_date": 1302566400,
        "url": "https://www.igdb.com/games/portal-2"
    }])

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__.return_value = mock_resp
    mock_post.return_value = mock_ctx

    details = await fetch_game_details_from_igdb("Portal 2")
    assert details is not None
    assert details["name"] == "Portal 2"
    assert details["rating"] == 95.5


@pytest.mark.asyncio
@patch("aiohttp.ClientSession.post")
async def test_fetch_and_cache_game_priorities(mock_post, monkeypatch, setup_test_db):
    """Test fetch_and_cache_game_from_igdb prioritizes exact matches, then category 0 games."""
    monkeypatch.setenv("TWITCH_CLIENT_ID", "mock_client_id")
    monkeypatch.setenv("TWITCH_ACCESS_TOKEN", "mock_token")

    # Mock response containing multiple candidates, testing selection priority
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=[
        {"id": 10, "name": "Portal 2 DLC", "category": 1},
        {"id": 20, "name": "Portal 2", "category": 0},  # Exact match & category 0
    ])

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__.return_value = mock_resp
    mock_post.return_value = mock_ctx

    game_id, official_name = await fetch_and_cache_game_from_igdb("Portal 2")
    assert game_id == 20
    assert official_name == "Portal 2"

    # Verify game was cached into local DB
    async with aiosqlite.connect(setup_test_db) as db:
        async with db.execute("SELECT name FROM igdb_games WHERE id = ?", (20,)) as cursor:
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == "Portal 2"


@pytest.mark.asyncio
@patch("aiohttp.ClientSession.post")
async def test_resolve_to_base_game_expansion(mock_post, monkeypatch, setup_test_db):
    """Test resolve_to_base_game correctly maps DLC/expansion to parent base game."""
    monkeypatch.setenv("TWITCH_CLIENT_ID", "mock_client_id")
    monkeypatch.setenv("TWITCH_ACCESS_TOKEN", "mock_token")

    # Side effect to handle multiple consecutive aiohttp posts (DLC lookup -> Parent lookup)
    mock_resp_dlc = AsyncMock()
    mock_resp_dlc.status = 200
    mock_resp_dlc.json = AsyncMock(return_value=[
        {"id": 50, "name": "Cyberpunk 2077: Phantom Liberty", "category": 1, "parent_game": {"id": 100, "name": "Cyberpunk 2077"}}
    ])

    mock_resp_parent = AsyncMock()
    mock_resp_parent.status = 200
    mock_resp_parent.json = AsyncMock(return_value=[
        {"id": 100, "name": "Cyberpunk 2077", "category": 0}
    ])

    # Configure mock session.post to return dlc response first, then parent response
    mock_ctx_dlc = AsyncMock()
    mock_ctx_dlc.__aenter__.return_value = mock_resp_dlc

    mock_ctx_parent = AsyncMock()
    mock_ctx_parent.__aenter__.return_value = mock_resp_parent

    mock_post.side_effect = [mock_ctx_dlc, mock_ctx_parent]

    base_id, base_name = await resolve_to_base_game(50, "Cyberpunk 2077: Phantom Liberty")
    assert base_id == 100
    assert base_name == "Cyberpunk 2077"

    # Verify base game was cached in local DB
    async with aiosqlite.connect(setup_test_db) as db:
        async with db.execute("SELECT name FROM igdb_games WHERE id = ?", (100,)) as cursor:
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == "Cyberpunk 2077"


@pytest.mark.asyncio
@patch("aiohttp.ClientSession.post")
async def test_resolve_to_base_game_no_expansion(mock_post, monkeypatch, setup_test_db):
    """Test resolve_to_base_game returns original game if it is already a base game (category 0)."""
    monkeypatch.setenv("TWITCH_CLIENT_ID", "mock_client_id")
    monkeypatch.setenv("TWITCH_ACCESS_TOKEN", "mock_token")

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=[
        {"id": 100, "name": "Cyberpunk 2077", "category": 0}
    ])

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__.return_value = mock_resp
    mock_post.return_value = mock_ctx

    base_id, base_name = await resolve_to_base_game(100, "Cyberpunk 2077")
    assert base_id == 100
    assert base_name == "Cyberpunk 2077"

# -----------------------------------------------------------------------------
# LFG Permission Check Tests
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_is_lfg_allowed_no_row(setup_test_db):
    """Test is_lfg_allowed_in_channel returns True by default when no guild settings exist."""
    allowed = await is_lfg_allowed_in_channel(12345, 111)
    assert allowed is True


@pytest.mark.asyncio
async def test_is_lfg_allowed_mode_all(setup_test_db):
    """Test is_lfg_allowed_in_channel returns True when mode is set to 'all'."""
    async with aiosqlite.connect(setup_test_db) as db:
        await db.execute(
            "INSERT OR REPLACE INTO guild_lfg_settings (guild_id, mode, whitelisted_channels) VALUES (?, ?, ?)",
            (12345, "all", "")
        )
        await db.commit()

    allowed = await is_lfg_allowed_in_channel(12345, 111)
    assert allowed is True


@pytest.mark.asyncio
async def test_is_lfg_allowed_mode_none(setup_test_db):
    """Test is_lfg_allowed_in_channel returns False when mode is set to 'none'."""
    async with aiosqlite.connect(setup_test_db) as db:
        await db.execute(
            "INSERT OR REPLACE INTO guild_lfg_settings (guild_id, mode, whitelisted_channels) VALUES (?, ?, ?)",
            (12345, "none", "")
        )
        await db.commit()

    allowed = await is_lfg_allowed_in_channel(12345, 111)
    assert allowed is False


@pytest.mark.asyncio
async def test_is_lfg_allowed_mode_whitelist(setup_test_db):
    """Test is_lfg_allowed_in_channel enforces whitelist rules correctly."""
    async with aiosqlite.connect(setup_test_db) as db:
        await db.execute(
            "INSERT OR REPLACE INTO guild_lfg_settings (guild_id, mode, whitelisted_channels) VALUES (?, ?, ?)",
            (12345, "whitelist", "111,222")
        )
        await db.commit()

    # Whitelisted channel ID -> True
    assert await is_lfg_allowed_in_channel(12345, 111) is True
    assert await is_lfg_allowed_in_channel(12345, 222) is True

    # Non-whitelisted channel ID -> False
    assert await is_lfg_allowed_in_channel(12345, 333) is False


@pytest.mark.asyncio
async def test_is_lfg_allowed_mode_whitelist_empty(setup_test_db):
    """Test is_lfg_allowed_in_channel returns False in whitelist mode if whitelist string is empty."""
    async with aiosqlite.connect(setup_test_db) as db:
        await db.execute(
            "INSERT OR REPLACE INTO guild_lfg_settings (guild_id, mode, whitelisted_channels) VALUES (?, ?, ?)",
            (12345, "whitelist", "")
        )
        await db.commit()

    assert await is_lfg_allowed_in_channel(12345, 111) is False