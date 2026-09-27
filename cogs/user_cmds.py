import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

from database import DB_PATH
from utils import (
    resolve_to_base_game,
    autocomplete_games,
    fetch_and_cache_game_from_igdb,
    ConfirmDataDeletionView
)


async def autocomplete_user_subscriptions(interaction: discord.Interaction, current: str):
    clean_current = current.strip().replace("’", "'").replace("‘", "'")
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
                (interaction.user.id, f"%{clean_current}%")
        ) as cursor:
            rows = await cursor.fetchall()

    if not rows:
        return [
            app_commands.Choice(
                name="No subscribed games found",
                value="__NONE__"
            )
        ]

    return [
        app_commands.Choice(
            name=row[0][:100],
            value=row[0][:100]
        )
        for row in rows
    ]

class UserCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="top_games", description="Show the top games on the server by subscription count")
    @app_commands.describe(limit="Number of top games to display (default: 10, max: 25)", public="Set to True to share publicly (default: False)")
    async def top_games(self, interaction: discord.Interaction, limit: int = 10, public: bool = False):
        # Guild check
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return

        # Cap the maximum limit to prevent huge wall-of-text messages
        limit = max(1, min(limit, 25))

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT s.user_id, g.name FROM subscriptions s JOIN igdb_games g ON s.game_id = g.id") as cursor:
                rows = await cursor.fetchall()

        if not rows:
            await interaction.response.send_message("There are no active subscriptions in this server.", ephemeral=not public)
            return

        # Count subscriptions per game for members present in this guild
        game_counts = {}
        for user_id, game_name in rows:
            if interaction.guild.get_member(user_id):
                game_counts[game_name] = game_counts.get(game_name, 0) + 1

        if not game_counts:
            await interaction.response.send_message("No members in this server are currently subscribed to any games.", ephemeral=not public)
            return

        # Sort games by subscriber count in descending order
        sorted_games = sorted(game_counts.items(), key=lambda x: x[1], reverse=True)[:limit]
        formatted_list = [f"`#{idx + 1}` **{gname}** — {cnt} {'subscriber' if cnt == 1 else 'subscribers'}" for idx, (gname, cnt) in enumerate(sorted_games)]

        embed = discord.Embed(
            title=f"Top {len(sorted_games)} Games in {interaction.guild.name}",
            description="\n".join(formatted_list),
            color=discord.Color.gold()
        )

        await interaction.response.send_message(embed=embed, ephemeral=not public)

    @app_commands.command(name="subscribe", description="Subscribe to a game")
    @app_commands.autocomplete(game=autocomplete_games)
    async def subscribe(self, interaction: discord.Interaction, game: str):
        await interaction.response.defer(ephemeral=True)
        clean_game = game.strip().replace("’", "'").replace("‘", "'")

        raw_id = None
        raw_name = None

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT id, name FROM igdb_games WHERE name = ?", (clean_game,)) as cursor:
                row = await cursor.fetchone()
        if row:
            raw_id, raw_name = row[0], row[1]

        # If game wasn't found in local multiplayer cache, fetch and store it from IGDB live
        if not raw_id:
            user_searching_dlc = any(char in clean_game for char in [":", "-", "–", "—"])
            result = await fetch_and_cache_game_from_igdb(clean_game, allow_dlc=user_searching_dlc)
            if not result:
                await interaction.followup.send(
                    f"Could not find **{clean_game}** in local cache or on IGDB.",
                    ephemeral=True
                )
                return

            raw_id, raw_name = result

        game_id, official_name = await resolve_to_base_game(raw_id, raw_name)

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                    "SELECT 1 FROM subscriptions WHERE user_id = ? AND game_id = ?",
                    (interaction.user.id, game_id)
            ) as cursor:
                if await cursor.fetchone():
                    await interaction.followup.send(f"You are already subscribed to **{official_name}**!", ephemeral=True)
                    return

            await db.execute(
                "INSERT INTO subscriptions (user_id, game_id) VALUES (?, ?)",
                (interaction.user.id, game_id)
            )
            await db.commit()

        await interaction.followup.send(f"You subscribed to **{official_name}**!", ephemeral=True)

    @app_commands.command(name="unsubscribe", description="Unsubscribe from a game")
    @app_commands.autocomplete(game=autocomplete_user_subscriptions)
    async def unsubscribe(self, interaction: discord.Interaction, game: str):
        if game == "__NONE__":
            await interaction.response.send_message(
                "You are not subscribed to any games.",
                ephemeral=True
            )
            return

        clean_game = game.strip().replace("’", "'").replace("‘", "'")
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT id, name FROM igdb_games WHERE name = ?", (clean_game,)) as cursor:
                row = await cursor.fetchone()

        # Handle missing game
        if not row:
            await interaction.response.send_message("Game not found.", ephemeral=True)
            return

        game_id, official_name = await resolve_to_base_game(row[0], row[1])

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM subscriptions WHERE user_id = ? AND game_id = ?",
                (interaction.user.id, game_id)
            )
            await db.commit()

        await interaction.response.send_message(f"You unsubscribed from **{official_name}**", ephemeral=True)

    @app_commands.command(name="ping_game", description="Ping all users subscribed to a game")
    @app_commands.describe(
        game="The game to ping subscribers for",
        note="Optional note or extra details"
    )
    @app_commands.checks.cooldown(1, 30.0)
    @app_commands.autocomplete(game=autocomplete_games)
    async def ping_game(self, interaction: discord.Interaction, game: str, note: str = None):
        await interaction.response.defer()

        clean_game = game.strip().replace("’", "'").replace("‘", "'")

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT id, name FROM igdb_games WHERE name = ? COLLATE NOCASE", (clean_game,)) as cursor:
                row = await cursor.fetchone()

            if not row:
                await interaction.followup.send("Game not found.")
                return

            game_id, official_name = await resolve_to_base_game(row[0], row[1])

            async with db.execute("SELECT user_id FROM subscriptions WHERE game_id = ?", (game_id,)) as cursor:
                users = await cursor.fetchall()

            # Retrieve user custom template if saved
            async with db.execute("SELECT ping_template FROM user_settings WHERE user_id = ?", (interaction.user.id,)) as cursor:
                template_row = await cursor.fetchone()

        if not users:
            await interaction.followup.send(f"No subscribers for **{official_name}**.")
            return

        mentions = " ".join(f"<@{u[0]}>" for u in users)
        caller_mention = interaction.user.mention
        formatted_game = f"**{official_name}**"
        clean_note = note.strip() if note else ""

        custom_template = template_row[0] if template_row and template_row[0] else None

        if custom_template:
            msg = (
                custom_template
                .replace("{caller}", caller_mention)
                .replace("{user}", caller_mention)
                .replace("{game}", formatted_game)
                .replace("{mentions}", mentions)
                .replace("{note}", clean_note)
            )
            # Safeguard: Append note if present but omitted from template
            if clean_note and clean_note not in msg:
                msg += f"\n> {clean_note}"
            # Safeguard: Append mentions if omitted from template
            if mentions not in msg:
                msg += f"\n{mentions}"
        else:
            # Default format: "<caller> wants to play <game>! <mentions>"
            msg = f"{caller_mention} wants to play {formatted_game}! {mentions}"
            if clean_note:
                msg += f"\n> {clean_note}"

        await interaction.followup.send(msg)

    @app_commands.command(name="set_ping_template", description="Customize how your /ping_game calls look")
    @app_commands.describe(template="Your custom message. Placeholders: {caller}, {game}, {mentions}, {note}")
    async def set_ping_template(self, interaction: discord.Interaction, template: str):
        if len(template) > 300:
            await interaction.response.send_message("Template is too long! Please keep it under 300 characters.", ephemeral=True)
            return

        final_template = template
        warning = ""
        if "{mentions}" not in template:
            final_template += "\n{mentions}"
            warning = "\n*(Note: `{mentions}` was appended automatically so subscribers receive the ping notification.)*"

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """
                INSERT INTO user_settings (user_id, ping_template)
                VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET ping_template = excluded.ping_template
                """,
                (interaction.user.id, final_template)
            )
            await db.commit()

        preview = (
            final_template
            .replace("{caller}", interaction.user.mention)
            .replace("{user}", interaction.user.mention)
            .replace("{game}", "**Helldivers 2**")
            .replace("{mentions}", interaction.user.mention)
            .replace("{note}", "Looking for 2 more!")
        )

        await interaction.response.send_message(
            f"✅ **Custom ping template saved!**{warning}\n\n"
            f"**Preview:**\n{preview}",
            ephemeral=True
        )

    @app_commands.command(name="view_ping_template", description="View your current custom ping template setting")
    async def view_ping_template(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT ping_template FROM user_settings WHERE user_id = ?", (interaction.user.id,)) as cursor:
                row = await cursor.fetchone()

        if not row or not row[0]:
            await interaction.response.send_message(
                "You are currently using the **default ping format**:\n"
                "`<caller> wants to play <game>! <mentions> <note>`",
                ephemeral=True
            )
            return

        template = row[0]
        preview = (
            template
            .replace("{caller}", interaction.user.mention)
            .replace("{user}", interaction.user.mention)
            .replace("{game}", "**Helldivers 2**")
            .replace("{mentions}", interaction.user.mention)
            .replace("{note}", "Looking for 2 more!")
        )

        await interaction.response.send_message(
            f"**Your Current Custom Template:**\n```{template}```\n"
            f"**Preview:**\n{preview}",
            ephemeral=True
        )

    @app_commands.command(name="reset_ping_template", description="Reset your ping template back to default")
    async def reset_ping_template(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM user_settings WHERE user_id = ?", (interaction.user.id,))
            await db.commit()

        await interaction.response.send_message("✅ Your ping template has been reset to the default format!", ephemeral=True)

    @app_commands.command(name="user_subscriptions", description="View subscriptions for yourself or another user.")
    async def user_subscriptions(self, interaction: discord.Interaction, user: discord.User = None, public: bool = False):
        target_user = user or interaction.user

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                    "SELECT g.name FROM subscriptions s JOIN igdb_games g ON s.game_id = g.id WHERE s.user_id = ? ORDER BY g.name ",
                    (target_user.id,)
            ) as cursor:
                rows = await cursor.fetchall()

        header = "Your subscribed games" if target_user.id == interaction.user.id else f"Subscribed games for {target_user.mention}"

        if not rows:
            await interaction.response.send_message(
                f"No active subscriptions found for {target_user.mention}.",
                ephemeral=not public,
                allowed_mentions=discord.AllowedMentions.none()
            )
            return

        game_list = "\n".join(f"• {row[0]}" for row in rows)

        await interaction.response.send_message(
            f"**{header}** ({len(rows)}):\n{game_list}",
            ephemeral=not public,
            allowed_mentions=discord.AllowedMentions.none()
        )

    @app_commands.command(name="show_subscribers", description="View all users in this server subscribed to a game without pinging them")
    @app_commands.autocomplete(game=autocomplete_games)
    async def show_subscribers(self, interaction: discord.Interaction, game: str, public: bool = False):
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=not public)
        clean_game = game.strip().replace("’", "'").replace("‘", "'")

        raw_id = None
        raw_name = None

        # Keep database operations cleanly scoped or re-opened
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT id, name FROM igdb_games WHERE name = ?", (clean_game,)) as cursor:
                row = await cursor.fetchone()

        if row:
            raw_id, raw_name = row[0], row[1]

        # If game wasn't found in local multiplayer cache, fetch and store it from IGDB live
        if not raw_id:
            user_searching_dlc = any(char in clean_game for char in [":", "-", "–", "—"])
            result = await fetch_and_cache_game_from_igdb(clean_game, allow_dlc=user_searching_dlc)
            if not result:
                await interaction.followup.send(
                    f"Could not find **{clean_game}** in local cache or on IGDB.",
                    ephemeral=not public
                )
                return

            raw_id, raw_name = result

        game_id, official_name = await resolve_to_base_game(raw_id, raw_name)

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT user_id FROM subscriptions WHERE game_id = ?", (game_id,)) as cursor:
                users = await cursor.fetchall()

        if not users:
            await interaction.followup.send(f"No subscribers for **{official_name}**.", ephemeral=not public)
            return

        local_mentions = [f"<@{u[0]}>" for u in users if interaction.guild.get_member(u[0])]
        if not local_mentions:
            await interaction.followup.send(
                f"No users in **{interaction.guild.name}** are subscribed to **{official_name}**.",
                ephemeral=not public
            )
            return

        await interaction.followup.send(
            f"Players in this server interested in **{official_name}** ({len(local_mentions)}):\n{' '.join(local_mentions)}",
            ephemeral=not public,
            allowed_mentions=discord.AllowedMentions.none()
        )


    @app_commands.command(name="delete_my_data", description="Permanently delete all your subscription data from the bot")
    async def delete_my_data(self, interaction: discord.Interaction):
        view = ConfirmDataDeletionView(target_user_id=interaction.user.id)
        await interaction.response.send_message(
            "⚠️ **Are you sure you want to delete all your data?**\nThis will immediately remove all your active game subscriptions.",
            view=view,
            ephemeral=True
        )


    @app_commands.command(name="help", description="Show information about available bot commands")
    async def help_command(self, interaction: discord.Interaction):
        # Lookup helper: supports standalone commands or group subcommands
        def fmt(name: str, sub: str = None) -> str:
            cid = getattr(self.bot, "command_ids", {}).get(name)
            if sub:
                full_cmd = f"{name} {sub}"
                return f"</{full_cmd}:{cid}>" if cid else f"`/{full_cmd}`"
            return f"</{name}:{cid}>" if cid else f"`/{name}`"

        embed = discord.Embed(
            title="Bot Command Guide",
            description="Here is everything you can do with the bot:\n"
                        "-# Options marked `[public]` are visible only to you by default. Set `public: True` to share them in channel.",
            color=discord.Color.blue()
        )

        embed.add_field(
            name="Managing Subscriptions",
            value=(
                f"• {fmt('subscribe')} `<game>` — Subscribe to LFG notifications.\n"
                f"• {fmt('unsubscribe')} `<game>` — Unsubscribe from active notifications.\n"
                f"• {fmt('user_subscriptions')} — View active subscriptions."
            ),
            inline=False
        )

        embed.add_field(
            name="Server Activity & Social",
            value=(
                f"• {fmt('ping_game')} `<game>` `[note]` — Ping subscribers with optional note.\n"
                f"• {fmt('set_ping_template')} `<template>` — Set custom ping format.\n"
                f"• {fmt('view_ping_template')} — View your saved ping format.\n"
                f"• {fmt('reset_ping_template')} — Reset ping format to default.\n"
                f"• {fmt('top_games')} `[limit] [public]` — View popular server games.\n"
                f"• {fmt('show_subscribers')} `<game>` — List subscribers."
            ),
            inline=False
        )

        embed.add_field(
            name="Data & Privacy",
            value=f"• {fmt('delete_my_data')} — Delete your subscription data.",
            inline=False
        )


                # Conditionally display admin section if user has Administrator permissions
        is_admin = interaction.permissions and interaction.permissions.administrator
        if is_admin:
            # Visual separator line (only appears for admins/owners)
            embed.add_field(name="\u200b", value="─────────────────────────────\n", inline=False)
            embed.add_field(
                name="Server Administration",
                value=(
                    f"• {fmt('admin', 'setup_top_games')} `[limit] [interval]` — Post a live-updating top games message.\n"
                    f"• {fmt('admin', 'edit_top_games')} `<message> [limit] [interval]` — Edit tracker settings.\n"
                    f"• {fmt('admin', 'force_update_top_games')} `<message>` — Force an immediate refresh.\n"
                    f"• {fmt('admin', 'remove_top_games')} `<message>` — Remove an active live tracker."
                ),
                inline=False
            )

        embed.set_footer(text="Looking for a missing game or DLC? Type its full name in /subscribe!")

        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(UserCommands(bot))