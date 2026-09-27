import os
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

def is_bot_owner():
    async def predicate(interaction: discord.Interaction) -> bool:
        raw_owner_id = os.getenv("BOT_OWNER_ID")
        owner_id = int(raw_owner_id) if raw_owner_id and raw_owner_id.isdigit() else 0
        return interaction.user.id == owner_id
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
@is_bot_owner()
async def setup_top_games(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("This command must be run inside a server.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    # Post the initial message that will be continuously updated
    embed = discord.Embed(title="📊 Live Top Games", description="Initializing live updates...", color=discord.Color.gold())
    message = await interaction.channel.send(embed=embed)

    # Save or update the tracked message target in DB
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO live_top_games (guild_id, channel_id, message_id)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                                                channel_id = excluded.channel_id,
                                                message_id = excluded.message_id
            """,
            (interaction.guild.id, interaction.channel.id, message.id)
        )
        await db.commit()

    # Trigger immediate embed update instead of waiting 1 hour
    await refresh_live_top_games(interaction.client)

    await interaction.followup.send("✅ Live top games tracker posted and updated!", ephemeral=True)

@admin_group.command(name="remove_top_games", description="Delete the bot's live top games message and stop updating it")
@is_bot_owner()
async def remove_top_games(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("This command must be run inside a server.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT channel_id, message_id FROM live_top_games WHERE guild_id = ?", (interaction.guild.id,)) as cursor:
            row = await cursor.fetchone()

        if not row:
            await interaction.followup.send("No active live top games tracker found for this server.", ephemeral=True)
            return

        channel_id, message_id = row

        # Attempt to delete the message from the channel
        channel = interaction.guild.get_channel(channel_id)
        if channel:
            try:
                msg = await channel.fetch_message(message_id)
                await msg.delete()
            except (discord.NotFound, discord.Forbidden):
                pass  # Message was already deleted manually or permissions missing

        # Remove entry from DB
        await db.execute("DELETE FROM live_top_games WHERE guild_id = ?", (interaction.guild.id,))
        await db.commit()

    await interaction.followup.send("🗑️ Live top games tracker removed successfully.", ephemeral=True)

# Bind the subscriber-specific autocomplete function
admin_subscribe.autocomplete('game')(autocomplete_games)
admin_unsubscribe.autocomplete('game')(autocomplete_admin_unsubscribe)