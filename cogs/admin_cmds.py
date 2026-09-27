import os
import time

import aiosqlite
import discord
from discord import app_commands

from cogs.background_tasks import refresh_live_top_games
from database import DB_PATH
from utils import (
    resolve_to_base_game,
    autocomplete_games,
    fetch_and_cache_game_from_igdb,
    ConfirmDataDeletionView
)

def get_bot_owner_id() -> int:
    raw_owner_id = os.getenv("BOT_OWNER_ID")
    return int(raw_owner_id) if raw_owner_id and raw_owner_id.isdigit() else 0

def is_bot_owner():
    async def predicate(interaction: discord.Interaction) -> bool:
        return interaction.user.id == get_bot_owner_id()
    return app_commands.check(predicate)

def is_owner_or_admin():
    async def predicate(interaction: discord.Interaction) -> bool:
        # 1. Allow Bot Owner anywhere
        if interaction.user.id == get_bot_owner_id():
            return True

        # 2. Allow Server Administrator in their guild
        if interaction.permissions and interaction.permissions.administrator:
            return True

        return False
    return app_commands.check(predicate)

async def autocomplete_admin_unsubscribe(interaction: discord.Interaction, current: str):
    clean_current = current.strip().replace("’", "'").replace("‘", "'")

    # Pull the target user directly from interaction namespace
    target_user = interaction.namespace.user

    # If the admin hasn't selected a user in the UI yet
    if not target_user:
        return [app_commands.Choice(name="Select a user first...", value="__NONE__")]

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
                """
                SELECT g.name
                FROM subscriptions s
                         JOIN igdb_games g ON s.game_id = g.id
                WHERE s.user_id = ?
                  AND g.name LIKE ?
                ORDER BY g.name
                    LIMIT 25
                """,
                (target_user.id, f"%{clean_current}%")
        ) as cursor:
            rows = await cursor.fetchall()

    if not rows:
        return [app_commands.Choice(name="User has no active subscriptions", value="__NONE__")]

    return [
        app_commands.Choice(name=row[0][:100], value=row[0][:100])
        for row in rows
    ]

async def autocomplete_live_messages(interaction: discord.Interaction, current: str):
    if not interaction.guild:
        return []

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
                "SELECT message_id, channel_id, entry_limit, created_at FROM live_top_games WHERE guild_id = ? ORDER BY created_at DESC",
                (interaction.guild.id,)
        ) as cursor:
            rows = await cursor.fetchall()

    if not rows:
        return [app_commands.Choice(name="No active live top game messages found", value="__NONE__")]

    choices = []
    for msg_id, ch_id, limit, created_at in rows:
        channel = interaction.guild.get_channel(ch_id)
        channel_name = f"#{channel.name}" if channel else f"Channel {ch_id}"

        # Format the timestamp into a readable plain-text date/time
        if created_at:
            time_str = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(created_at))
        else:
            time_str = "Unknown date"

        # Construct autocomplete label with timestamp, channel, limit, and message ID
        label = f"{time_str} | {channel_name} | Top {limit} | ID: {msg_id}"

        # Match search input against timestamp, channel, or message ID
        if not current or current.lower() in label.lower() or current in str(msg_id):
            choices.append(app_commands.Choice(name=label[:100], value=str(msg_id)))

    return choices[:25]

class ConfirmDuplicateSetupView(discord.ui.View):
    def __init__(self, author_id: int):
        super().__init__(timeout=60.0)
        self.author_id = author_id
        self.value: bool | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("You cannot use these buttons.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Create Tracker Anyway", style=discord.ButtonStyle.primary)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button):
        self.value = True
        self.stop()
        await interaction.response.defer()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button):
        self.value = False
        self.stop()
        await interaction.response.edit_message(
            content="❌ Setup canceled. No new tracker message was posted.",
            view=None
        )

    async def on_timeout(self):
        self.value = None
        self.stop()

admin_group = app_commands.Group(name="admin", description="Admin-only bot management commands")

@admin_group.command(name="subscribe_user", description="Force-subscribe a user to a game")
@is_bot_owner()
async def admin_subscribe(interaction: discord.Interaction, user: discord.User, game: str):
    await interaction.response.defer(ephemeral=True)
    clean_game = game.strip().replace("’", "'").replace("‘", "'")

    raw_id = None
    raw_name = None

    async with aiosqlite.connect(DB_PATH) as db:
        # 1. Check local catalog
        async with db.execute("SELECT id, name FROM igdb_games WHERE name = ? COLLATE NOCASE", (clean_game,)) as cursor:
            row = await cursor.fetchone()

        if row:
            raw_id, raw_name = row[0], row[1]

    # 2. Live IGDB Fallback (Handles single-player games & DLCs via delimiters)
    if not raw_id:
        user_searching_dlc = any(char in clean_game for char in [":", "-", "–", "—"])
        result = await fetch_and_cache_game_from_igdb(clean_game, allow_dlc=user_searching_dlc)
        if not result:
            await interaction.followup.send(
                f"Could not find **{clean_game}** in local catalog or on IGDB.",
                ephemeral=True
            )
            return
        raw_id, raw_name = result

    # 3. Normalize special editions to base game
    game_id, official_name = await resolve_to_base_game(raw_id, raw_name)

    # 4. Insert subscription within a clean connection context
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO subscriptions (user_id, game_id) VALUES (?, ?)",
            (user.id, game_id)
        )
        await db.commit()

    await interaction.followup.send(
        f"✅ Subscribed {user.mention} to **{official_name}**.",
        ephemeral=True
    )

@admin_group.command(name="unsubscribe_user", description="Force-unsubscribe a user from a game")
@is_bot_owner()
async def admin_unsubscribe(interaction: discord.Interaction, user: discord.User, game: str):
    if game == "__NONE__":
        await interaction.response.send_message(
            f"**{user.display_name}** has no active subscriptions to remove.",
            ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    clean_game = game.strip().replace("’", "'").replace("‘", "'")

    raw_id = None
    raw_name = None

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, name FROM igdb_games WHERE name = ? COLLATE NOCASE", (clean_game,)) as cursor:
            row = await cursor.fetchone()

        if row:
            raw_id, raw_name = row[0], row[1]

    if not raw_id:
        user_searching_dlc = any(char in clean_game for char in [":", "-", "–", "—"])
        result = await fetch_and_cache_game_from_igdb(clean_game, allow_dlc=user_searching_dlc)
        if not result:
            await interaction.followup.send(
                f"Could not find **{clean_game}** in local catalog or on IGDB.",
                ephemeral=True
            )
            return
        raw_id, raw_name = result

    game_id, official_name = await resolve_to_base_game(raw_id, raw_name)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM subscriptions WHERE user_id = ? AND game_id = ?", (user.id, game_id))
        await db.commit()

    await interaction.followup.send(
        f"🗑️ Unsubscribed {user.mention} from **{official_name}**.",
        ephemeral=True
    )

@admin_group.command(name="delete_user_data", description="Permanently delete all subscription data for a specific user")
@is_bot_owner()
async def admin_delete_user_data(interaction: discord.Interaction, user: discord.User):
    view = ConfirmDataDeletionView(target_user_id=user.id, author_id=interaction.user.id)
    await interaction.response.send_message(
        f"⚠️ **Are you sure you want to delete all data for {user.mention}?**\nThis will permanently purge all their active subscriptions.",
        view=view,
        ephemeral=True
    )

@admin_group.command(name="setup_top_games", description="Post a live-updating top games message in this channel")
@app_commands.describe(limit="Number of top games to display in this list (default: 10, max: 25)")
@is_owner_or_admin()
async def setup_top_games(interaction: discord.Interaction, limit: int = 10):
    if not interaction.guild:
        await interaction.response.send_message("This command must be run inside a server.", ephemeral=True)
        return

    clean_limit = max(1, min(limit, 25))

    # 1. Check if a tracker already exists in this server
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT channel_id, created_at FROM live_top_games WHERE guild_id = ? ORDER BY created_at DESC LIMIT 1",
            (interaction.guild.id,)
        ) as cursor:
            existing = await cursor.fetchone()

    # 2. Ask for confirmation if a tracker exists
    if existing:
        ch_id, created_at = existing
        time_formatted = f"<t:{created_at}:f> (<t:{created_at}:R>)" if created_at else "an unknown date"
        channel_mention = f"<#{ch_id}>"

        view = ConfirmDuplicateSetupView(author_id=interaction.user.id)
        await interaction.response.send_message(
            f"⚠️ A live top games tracker already exists in {channel_mention}, sent on {time_formatted}.\n"
            "Are you sure you want to create another one?",
            view=view,
            ephemeral=True
        )

        # Wait for the user to click Confirm, Cancel, or let it time out
        await view.wait()

        if view.value is not True:
            if view.value is None:  # Timed out
                try:
                    await interaction.edit_original_response(
                        content="⏳ Setup timed out. Action canceled.",
                        view=None
                    )
                except Exception:
                    pass
            return

        # Clear buttons from confirmation prompt before creating message
        await interaction.edit_original_response(
            content="⏳ Creating new live top games tracker...",
            view=None
        )
    else:
        await interaction.response.defer(ephemeral=True)

    # 3. Create placeholder and initialize top games tracker
    embed = discord.Embed(
        title=f"📊 Live Top {clean_limit} Games",
        description="Initializing live updates...",
        color=discord.Color.gold()
    )
    message = await interaction.channel.send(embed=embed)

    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """
                INSERT INTO live_top_games (message_id, guild_id, channel_id, entry_limit, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (message.id, interaction.guild.id, interaction.channel.id, clean_limit, int(time.time()))
            )
            await db.commit()

        await refresh_live_top_games(interaction.client)
        await interaction.followup.send(f"✅ Live top games tracker (Top {clean_limit}) posted!", ephemeral=True)

    except Exception as e:
        print(f"[Setup Error] Failed to initialize live top games: {e}")
        try:
            await message.delete()
        except Exception:
            pass
        await interaction.followup.send(f"❌ Failed to set up live tracker: `{e}`", ephemeral=True)


@admin_group.command(name="remove_top_games", description="Delete a live top games tracker message")
@app_commands.describe(message="Select the live tracker message to remove")
@app_commands.autocomplete(message=autocomplete_live_messages)
@is_owner_or_admin()
async def remove_top_games(interaction: discord.Interaction, message: str):
    if message == "__NONE__":
        await interaction.response.send_message("No live tracker message selected.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    try:
        target_msg_id = int(message)
    except ValueError:
        await interaction.followup.send("Invalid message selection.", ephemeral=True)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT channel_id FROM live_top_games WHERE message_id = ?", (target_msg_id,)) as cursor:
            row = await cursor.fetchone()

        if not row:
            await interaction.followup.send("Tracker message not found in database.", ephemeral=True)
            return

        channel_id = row[0]
        channel = interaction.guild.get_channel(channel_id)
        if channel:
            try:
                msg = await channel.fetch_message(target_msg_id)
                await msg.delete()
            except (discord.NotFound, discord.Forbidden):
                pass

        await db.execute("DELETE FROM live_top_games WHERE message_id = ?", (target_msg_id,))
        await db.commit()

    await interaction.followup.send("🗑️ Live top games tracker removed successfully.", ephemeral=True)


@admin_group.command(name="edit_top_games", description="Edit the entry limit of an existing live top games tracker")
@app_commands.describe(message="Select the tracker message to edit", limit="New number of top games to display (1-25)")
@app_commands.autocomplete(message=autocomplete_live_messages)
@is_owner_or_admin()
async def edit_top_games(interaction: discord.Interaction, message: str, limit: int):
    if message == "__NONE__":
        await interaction.response.send_message("No live tracker message selected.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    try:
        target_msg_id = int(message)
    except ValueError:
        await interaction.followup.send("Invalid message selection.", ephemeral=True)
        return

    clean_limit = max(1, min(limit, 25))

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("UPDATE live_top_games SET entry_limit = ? WHERE message_id = ?", (clean_limit, target_msg_id))
        await db.commit()

        if cursor.rowcount == 0:
            await interaction.followup.send("Tracker message not found in database.", ephemeral=True)
            return

    await refresh_live_top_games(interaction.client)
    await interaction.followup.send(f"✅ Tracker message updated to show **Top {clean_limit}** games!", ephemeral=True)

# Bind the subscriber-specific autocomplete function
admin_subscribe.autocomplete('game')(autocomplete_games)
admin_unsubscribe.autocomplete('game')(autocomplete_admin_unsubscribe)