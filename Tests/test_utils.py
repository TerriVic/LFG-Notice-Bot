import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import discord

from utils import autocomplete_games, GenericConfirmationView

# -----------------------------------------------------------------------------
# Autocomplete Test
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
@patch("aiosqlite.connect")
async def test_autocomplete_games(mock_connect):
    """Test autocomplete returning formatted app_commands.Choice lists."""
    # 1. Mock cursors
    mock_settings_cursor = AsyncMock()
    mock_settings_cursor.fetchone.return_value = ("⭐ ",)

    mock_games_cursor = AsyncMock()
    mock_games_cursor.fetchall.return_value = [
        ("Portal 2", 1, 1, 5),
        ("Portal", 0, 0, 2),
    ]

    # 2. Use standard MagicMock for database object so db.execute(...) is synchronous
    mock_db = MagicMock()

    def execute_side_effect(sql, *args, **kwargs):
        cursor_context = MagicMock()
        if "user_settings" in sql:
            cursor_context.__aenter__.return_value = mock_settings_cursor
        else:
            cursor_context.__aenter__.return_value = mock_games_cursor
        return cursor_context

    mock_db.execute.side_effect = execute_side_effect
    mock_connect.return_value.__aenter__.return_value = mock_db

    # 3. Mock Discord Interaction
    mock_interaction = MagicMock(spec=discord.Interaction)
    mock_interaction.user.id = 123456789
    mock_interaction.guild_id = 987654321
    mock_interaction.command = None

    choices = await autocomplete_games(mock_interaction, "Portal")

    assert len(choices) == 2
    assert isinstance(choices[0], discord.app_commands.Choice)
    assert any("Portal 2" in choice.name for choice in choices)


# -----------------------------------------------------------------------------
# UI View Confirmation Test
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_generic_confirmation_view_confirm():
    """Test confirmation button interaction inside GenericConfirmationView."""
    # 1. Track whether the callback function was executed
    callback_executed = False

    async def mock_on_confirm(interaction: discord.Interaction):
        nonlocal callback_executed
        callback_executed = True

    # 2. Instantiate view with callback
    view = GenericConfirmationView(
        author_id=12345,
        on_confirm=mock_on_confirm
    )

    # 3. Mock Discord button interaction
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user.id = 12345
    interaction.response.edit_message = AsyncMock()

    # 4. Trigger confirm button callback
    button = view.children[0]  # Standard confirm button
    await button.callback(interaction)

    # 5. Assert callback ran and message was edited
    assert callback_executed is True
    assert view.is_finished()
    interaction.response.edit_message.assert_called_once_with(
        content=view.confirm_message,
        view=None
    )