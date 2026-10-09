import os
import time
from operations import stats

import discord
from discord import app_commands
from discord.ext import commands
import aiosqlite
from database import DB_PATH
from utils import (
    resolve_to_base_game,
    fetch_and_cache_game_from_igdb,
    GenericConfirmationView,
    get_bot_owner_id,
    autocomplete_games
)

def is_bot_owner():
    async def predicate(interaction: discord.Interaction) -> bool:
        return interaction.user.id == get_bot_owner_id()
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

class OwnerCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # Declare group inside the cog
    owner_group = app_commands.Group(
        name="owner",
        description="Bot owner maintenance commands"
    )

    @owner_group.command(name="stats", description="View process uptime and aggregate operational counters")
    @is_bot_owner()
    async def operational_stats(self, interaction: discord.Interaction):
        counters = stats.snapshot()
        embed = discord.Embed(title="Bot operational statistics", color=discord.Color.blue())
        embed.add_field(name="Current servers", value=str(len(self.bot.guilds)))
        embed.add_field(name="Process uptime", value=f"{int(time.monotonic() - stats.started_at)} seconds")
        for label, key in (
            ("Completed LFG pings", "pings_sent"),
            ("IGDB requests (excluding cache hits)", "igdb_requests"),
            ("IGDB final failures", "igdb_failures"),
            ("Unexpected command errors", "command_errors"),
        ):
            embed.add_field(name=label, value=str(counters.get(key, 0)))
        embed.set_footer(text="Counters cover this process since startup and reset on restart. Partial LFG sends are not completed pings.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # --- PREFIX COMMANDS ---
    @commands.command(name="reload")
    @commands.is_owner()
    async def reload_cog(self, ctx: commands.Context, cog_name: str):
        # Strip .py extension if included by accident
        if cog_name.endswith(".py"):
            cog_name = cog_name[:-3]

        # Standardize path to 'cogs.name'
        target = cog_name if cog_name.startswith("cogs.") else f"cogs.{cog_name}"

        try:
            await ctx.bot.reload_extension(target)
            await ctx.send(f"✅ Reloaded `{target}` successfully!")
        except commands.ExtensionNotLoaded:
            # Fallback to load_extension if the cog wasn't loaded at startup
            try:
                await ctx.bot.load_extension(target)
                await ctx.send(f"✅ Loaded `{target}` successfully!")
            except Exception as e:
                await ctx.send(f"❌ Failed to load `{target}`:\n```py\n{e}\n```")
        except Exception as e:
            await ctx.send(f"❌ Failed to reload `{target}`:\n```py\n{e}\n```")

    @commands.command(name="delmsg", aliases=["delete_msg"])
    async def delete_bot_message(self, ctx: commands.Context, message_id: int, channel_id: int = None):
        # 1. Permission check matching custom bot owner ID
        if ctx.author.id != get_bot_owner_id():
            return

        # 2. Delete command invoke message if possible
        try:
            await ctx.message.delete()
        except discord.HTTPException:
            pass

        # 3. Resolve target channel (falling back to fetch_channel if not in cache)
        target_channel = ctx.channel
        if channel_id:
            target_channel = self.bot.get_channel(channel_id)
            if not target_channel:
                try:
                    target_channel = await self.bot.fetch_channel(channel_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    await ctx.send("❌ Channel not found or bot lacks access.", delete_after=5)
                    return

        # 4. Fetch and delete target message
        try:
            msg = await target_channel.fetch_message(message_id)
            if msg.author.id == self.bot.user.id:
                await msg.delete()
            else:
                await ctx.send("⚠️ Specified message was not sent by this bot.", delete_after=5)
        except discord.NotFound:
            await ctx.send("❌ Message not found.", delete_after=5)
        except discord.Forbidden:
            await ctx.send("⛔ Missing permissions to read or delete messages in that channel.", delete_after=5)
        except discord.HTTPException as e:
            await ctx.send(f"❌ HTTP Error: `{e}`", delete_after=5)

    @owner_group.command(name="subscribe_user", description="Force-subscribe a user to a game")
    @app_commands.autocomplete(game=autocomplete_games)
    @is_bot_owner()
    async def admin_subscribe(self, interaction: discord.Interaction, user: discord.User, game: str):
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
            result = await fetch_and_cache_game_from_igdb(clean_game)
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

    @owner_group.command(name="unsubscribe_user", description="Force-unsubscribe a user from a game")
    @app_commands.autocomplete(game=autocomplete_admin_unsubscribe)
    @is_bot_owner()
    async def admin_unsubscribe(self, interaction: discord.Interaction, user: discord.User, game: str):
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
            result = await fetch_and_cache_game_from_igdb(clean_game)
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

    @owner_group.command(name="delete_user_subscriptions", description="Permanently delete all subscription data for a specific user")
    @is_bot_owner()
    async def admin_delete_user_subscriptions(self, interaction: discord.Interaction, user: discord.User):
        async def do_delete_user_subs(_inter: discord.Interaction):
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("DELETE FROM subscriptions WHERE user_id = ?", (user.id,))
                await db.commit()

        view = GenericConfirmationView(
            author_id=interaction.user.id,
            confirm_label="Delete Subscriptions",
            confirm_message=f"🗑️ **All subscription data for {user.mention} has been permanently deleted.**",
            cancel_message=f"Action canceled. Subscription data for {user.mention} was preserved.",
            on_confirm=do_delete_user_subs
        )

        await interaction.response.send_message(
            f"⚠️ **Are you sure you want to delete all subscriptions for {user.mention}?**\nThis will permanently purge all their active subscriptions.",
            view=view,
            ephemeral=True
        )

    @owner_group.command(
        name="delete_all_user_data",
        description="Permanently delete all data and settings for a specific user"
    )
    @is_bot_owner()
    async def admin_delete_all_user_data(
            self,
            interaction: discord.Interaction,
            user: discord.User
    ):
        async def do_delete_all_user_data(_inter: discord.Interaction):
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "DELETE FROM subscriptions WHERE user_id = ?",
                    (user.id,)
                )
                await db.execute(
                    "DELETE FROM user_settings WHERE user_id = ?",
                    (user.id,)
                )
                await db.execute(
                    "DELETE FROM user_guild_settings WHERE user_id = ?",
                    (user.id,)
                )
                await db.commit()

        view = GenericConfirmationView(
            author_id=interaction.user.id,
            confirm_label="Delete All Data",
            confirm_message=(
                f"🗑️ **All subscriptions and user settings for "
                f"{user.mention} have been permanently deleted.**"
            ),
            cancel_message=(
                f"Action canceled. Data for {user.mention} survives another day."
            ),
            on_confirm=do_delete_all_user_data
        )

        await interaction.response.send_message(
            f"⚠️ **Are you sure you want to delete ALL data for "
            f"{user.mention}?**\n"
            "This will permanently purge their subscriptions, custom settings, "
            "and server-specific quiet-mode preferences.",
            view=view,
            ephemeral=True
        )

    @owner_group.command(name="overview", description="View all user subscriptions across all servers")
    @is_bot_owner()
    async def admin_overview(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                    """
                    SELECT s.user_id, g.name
                    FROM subscriptions s
                             JOIN igdb_games g ON s.game_id = g.id
                    ORDER BY s.user_id, g.name
                    """
            ) as cursor:
                rows = await cursor.fetchall()

        if not rows:
            await interaction.followup.send("📊 **Global Overview:** No active subscriptions found in the database.", ephemeral=True)
            return

        # Aggregate subscriptions per user ID
        user_map: dict[int, list[str]] = {}
        for user_id, game_name in rows:
            user_map.setdefault(user_id, []).append(game_name)

        total_subscriptions = len(rows)
        total_users = len(user_map)

        # Build description lines
        lines = [
            f"**Total Subscribers:** {total_users} | **Total Subscriptions:** {total_subscriptions}\n"
        ]

        for user_id, games in user_map.items():
            # Try fetching member/user display name from Discord cache, fallback to mention
            user_obj = interaction.client.get_user(user_id)
            user_label = f"**{user_obj.display_name}** (<@{user_id}>)" if user_obj else f"<@{user_id}>"

            games_str = ", ".join(f"`{g}`" for g in games)
            lines.append(f"• {user_label} ({len(games)}): {games_str}")

        # Chunk lines into multiple embeds if message exceeds Discord's character limits
        embeds = []
        current_embed = discord.Embed(
            title="🌐 Global Subscriptions Overview",
            color=discord.Color.blue()
        )
        current_text = ""

        for line in lines:
            if len(current_text) + len(line) + 1 > 3800:
                current_embed.description = current_text
                embeds.append(current_embed)
                current_embed = discord.Embed(color=discord.Color.blue())
                current_text = line + "\n"
            else:
                current_text += line + "\n"

        if current_text:
            current_embed.description = current_text
            embeds.append(current_embed)

        # Send the first embed via followup, and any extra chunks as additional followups
        await interaction.followup.send(embed=embeds[0], ephemeral=True)
        for extra_embed in embeds[1:]:
            await interaction.followup.send(embed=extra_embed, ephemeral=True)

    @commands.command(name="getdb", aliases=["download_db", "fetch_db"])
    async def send_database_file(self, ctx: commands.Context):
        # Ensure only the bot owner can request the file
        if ctx.author.id != get_bot_owner_id():
            return

        if not os.path.exists(DB_PATH):
            await ctx.send("❌ Database file not found.")
            return

        try:
            db_file = discord.File(DB_PATH, filename="db.sqlite")
            await ctx.author.send(
                "📁 Here is the current copy of the SQLite database:",
                file=db_file,
            )
        except discord.Forbidden:
            await ctx.send(
                "⚠️ Couldn't send you a DM. Enable DMs and try again. "
                "The database is only sent privately.",
                delete_after=10,
            )
            return

        # Only a status message may be posted in the invoking server channel.
        # Unexpected failures propagate to the shared prefix error handler.
        if ctx.guild:
            await ctx.send(
                "📬 Check your Direct Messages! Sent the database file.",
                delete_after=10,
            )

async def setup(bot):
    await bot.add_cog(OwnerCommands(bot))