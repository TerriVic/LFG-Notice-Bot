import datetime
import os

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

from database import DB_PATH
from utils import (
    resolve_to_base_game,
    autocomplete_games,
    fetch_and_cache_game_from_igdb,
    fetch_game_details_from_igdb,
    GenericConfirmationView, is_lfg_allowed_in_channel
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

        # Defer immediately to prevent 3-second timeout
        await interaction.response.defer(ephemeral=not public)

        # Cap the maximum limit to prevent huge wall-of-text messages
        limit = max(1, min(limit, 25))

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT s.user_id, g.name FROM subscriptions s JOIN igdb_games g ON s.game_id = g.id") as cursor:
                rows = await cursor.fetchall()

        if not rows:
            await interaction.followup.send("There are no active subscriptions in this server.", ephemeral=not public)
            return

        # Count subscriptions per game for members present in this guild
        game_counts = {}
        for user_id, game_name in rows:
            if interaction.guild.get_member(user_id):
                game_counts[game_name] = game_counts.get(game_name, 0) + 1

        if not game_counts:
            await interaction.followup.send("No members in this server are currently subscribed to any games.", ephemeral=not public)
            return

        # Sort games by subscriber count in descending order
        sorted_games = sorted(game_counts.items(), key=lambda x: x[1], reverse=True)[:limit]
        formatted_list = [f"`#{idx + 1}` **{gname}** — {cnt} {'subscriber' if cnt == 1 else 'subscribers'}" for idx, (gname, cnt) in enumerate(sorted_games)]

        embed = discord.Embed(
            title=f"Top {len(sorted_games)} Games in {interaction.guild.name}",
            description="\n".join(formatted_list),
            color=discord.Color.gold()
        )

        await interaction.followup.send(embed=embed, ephemeral=not public)

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
            result = await fetch_and_cache_game_from_igdb(clean_game)
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

        # Defer immediately to allow database and HTTP operations
        await interaction.response.defer(ephemeral=True)

        clean_game = game.strip().replace("’", "'").replace("‘", "'")
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT id, name FROM igdb_games WHERE name = ?", (clean_game,)) as cursor:
                row = await cursor.fetchone()

        # Handle missing game
        if not row:
            await interaction.followup.send("Game not found.", ephemeral=True)
            return

        game_id, official_name = await resolve_to_base_game(row[0], row[1])

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM subscriptions WHERE user_id = ? AND game_id = ?",
                (interaction.user.id, game_id)
            )
            await db.commit()

        await interaction.followup.send(f"You unsubscribed from **{official_name}**", ephemeral=True)

    @app_commands.command(name="ping_game", description="Ping all users subscribed to a game")
    @app_commands.describe(
        game="The game to ping subscribers for",
        note="Optional note or extra details"
    )
    @app_commands.checks.cooldown(1, 30.0)
    @app_commands.autocomplete(game=autocomplete_games)
    async def ping_game(self, interaction: discord.Interaction, game: str, note: str = None):
        # 1. Check LFG channel permissions before deferring
        if interaction.guild_id:
            allowed = await is_lfg_allowed_in_channel(interaction.guild_id, interaction.channel_id)
            if not allowed:
                await interaction.response.send_message(
                    "❌ LFG pings are not allowed in this channel.",
                    ephemeral=True
                )
                return

        # 2. Defer immediately to prevent Discord interaction timeout
        await interaction.response.defer()

        clean_game = game.strip().replace("’", "'").replace("‘", "'")
        current_guild_id = interaction.guild_id or 0

        # 3. Single-context database handling
        async with aiosqlite.connect(DB_PATH) as db:
            # Lookup game ID
            async with db.execute("SELECT id, name FROM igdb_games WHERE name = ? COLLATE NOCASE", (clean_game,)) as cursor:
                row = await cursor.fetchone()

            if not row:
                await interaction.followup.send("Game not found.")
                return

            game_id, official_name = await resolve_to_base_game(row[0], row[1])

            # Fetch all subscribers and check quiet mode status
            async with db.execute(
                    """
                    SELECT 
                        s.user_id,
                        CASE WHEN EXISTS (
                            SELECT 1 FROM user_settings us
                            WHERE us.user_id = s.user_id
                              AND us.guild_id IN (0, ?)
                              AND us.quiet_mode = 1
                        ) THEN 1 ELSE 0 END AS is_quiet
                    FROM subscriptions s
                    WHERE s.game_id = ?
                      AND s.user_id != ?
                    """,
                    (current_guild_id, game_id, interaction.user.id)
            ) as cursor:
                rows = await cursor.fetchall()

            async with db.execute(
                    "SELECT ping_template FROM user_settings WHERE user_id = ? AND guild_id = 0",
                    (interaction.user.id,)
            ) as cursor:
                template_row = await cursor.fetchone()

        if not rows:
            await interaction.followup.send(f"No other subscribers to ping for **{official_name}**.")
            return

        active_user_ids = [r[0] for r in rows if r[1] == 0]
        quiet_user_ids = [r[0] for r in rows if r[1] == 1]

        # Format mentions for phantom badges
        all_mentions = " ".join(f"<@{r[0]}>" for r in rows)
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
                .replace("{mentions}", all_mentions)
                .replace("{note}", clean_note)
            )
            if clean_note and clean_note not in msg:
                msg += f"\n> {clean_note}"
            if all_mentions not in msg:
                msg += f"\n{all_mentions}"
        else:
            msg = f"{caller_mention} wants to play {formatted_game}! {all_mentions}"
            if clean_note:
                msg += f"\n> {clean_note}"

        if quiet_user_ids:
            quiet_mentions_str = ", ".join(f"<@{uid}>" for uid in quiet_user_ids)
            verb = "has" if len(quiet_user_ids) == 1 else "have"
            msg += f"\n\n-# {quiet_mentions_str} {verb} quiet mode turned on"

        # Restrict notification pings strictly to active (non-quiet) users
        allowed_users = [discord.Object(id=uid) for uid in active_user_ids]
        allowed_mentions = discord.AllowedMentions(
            users=allowed_users if allowed_users else False,
            roles=False,
            everyone=False
        )

        await interaction.followup.send(msg, allowed_mentions=allowed_mentions)

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
                INSERT INTO user_settings (user_id, guild_id, ping_template)
                VALUES (?, 0, ?)
                ON CONFLICT(user_id, guild_id) DO UPDATE SET ping_template = excluded.ping_template
                """,
                (interaction.user.id, final_template)
            )
            await db.commit()


        # Dummy mentions for template preview
        mock_caller = interaction.user.mention
        mock_mentions = "<@123456789012345678>"

        preview = (
            template
            .replace("{caller}", mock_caller)
            .replace("{user}", interaction.user.mention)
            .replace("{game}", "**Wardogs**")
            .replace("{mentions}", mock_mentions)
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
            async with db.execute(
                    "SELECT ping_template FROM user_settings WHERE user_id = ? AND guild_id = 0",
                    (interaction.user.id,)
            ) as cursor:
                row = await cursor.fetchone()

        if not row or not row[0]:
            await interaction.response.send_message(
                "You are currently using the **default ping format**:\n"
                "`<caller> wants to play <game>! <mentions> <note>`",
                ephemeral=True
            )
            return

        template = row[0]

        mock_caller = interaction.user.mention
        mock_mentions = "<@123456789012345678>"
        preview = (
            template
            .replace("{caller}", mock_caller)
            .replace("{user}", interaction.user.mention)
            .replace("{game}", "**Wardogs**")
            .replace("{mentions}", mock_mentions)
            .replace("{note}", "Looking for 2 more!")
        )

        await interaction.response.send_message(
            f"**Your Current Custom Template:**\n```{template}```\n"
            f"**Preview:**\n{preview}",
            ephemeral=True
        )

    @app_commands.command(name="reset_ping_template", description="Reset your ping template back to default")
    async def reset_ping_template(self, interaction: discord.Interaction):
        async def do_reset(_inter: discord.Interaction):
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE user_settings SET ping_template = NULL WHERE user_id = ? AND guild_id = 0",
                    (interaction.user.id,)
                )
                await db.commit()

        view = GenericConfirmationView(
            author_id=interaction.user.id,
            confirm_label="Reset Template",
            confirm_message="✅ Your ping template has been reset to the default format!",
            cancel_message="Action canceled. Your custom ping template was preserved.",
            on_confirm=do_reset
        )

        await interaction.response.send_message(
            "Are you **sure** you want to reset your custom ping template back to default?",
            view=view,
            ephemeral=True
        )

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
            result = await fetch_and_cache_game_from_igdb(clean_game)
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

    @app_commands.command(name="delete_my_subscriptions", description="Permanently delete all your subscription data")
    async def delete_my_subscriptions(self, interaction: discord.Interaction):
        async def do_delete_subs(_inter: discord.Interaction):
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("DELETE FROM subscriptions WHERE user_id = ?", (interaction.user.id,))
                await db.commit()

        view = GenericConfirmationView(
            author_id=interaction.user.id,
            confirm_label="Confirm Deletion",
            confirm_message="**All your active game subscriptions have been deleted.**",
            cancel_message="Action canceled. Subscription data was preserved.",
            on_confirm=do_delete_subs
        )

        await interaction.response.send_message(
            "⚠️ **Are you sure you want to delete your subscriptions?**",
            view=view,
            ephemeral=True
        )

    @app_commands.command(name="delete_all_my_data", description="Permanently delete all your data and settings (e.g., ping templates).")
    async def delete_all_my_data(self, interaction: discord.Interaction):
        async def do_delete_all(_inter: discord.Interaction):
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("DELETE FROM subscriptions WHERE user_id = ?", (interaction.user.id,))
                await db.execute("DELETE FROM user_settings WHERE user_id = ?", (interaction.user.id,))
                await db.commit()

        view = GenericConfirmationView(
            author_id=interaction.user.id,
            confirm_label="Delete All Data",
            confirm_message="🗑️ **All your subscriptions and custom settings have been permanently deleted.**",
            cancel_message="Action canceled. Your data remains untouched.",
            on_confirm=do_delete_all
        )

        await interaction.response.send_message(
            "⚠️ **Are you sure you want to delete ALL your data?**\nThis will remove subscriptions and custom ping settings.",
            view=view,
            ephemeral=True
        )

    @app_commands.command(name="game_info", description="Get details, genres, and cover image for a game.")
    @app_commands.describe(
        game="The game to look up",
        public="Set to True to share publicly (default: False)"
    )
    @app_commands.autocomplete(game=autocomplete_games)
    async def game_info(self, interaction: discord.Interaction, game: str, public: bool = False):
        # Defers the response as ephemeral unless public is True
        await interaction.response.defer(ephemeral=not public)

        game_data = await fetch_game_details_from_igdb(game)
        if not game_data:
            await interaction.followup.send(f"❌ Could not find game details for **{game}**.", ephemeral=not public)
            return

        # Safe field extractions
        title = game_data.get("name", "Unknown Title")
        summary = game_data.get("summary", "No description available.")
        rating = f"{round(game_data['rating'])}%" if game_data.get("rating") else "N/A"
        url = game_data.get("url", "")

        # Use `or []` to guarantee an iterable if IGDB returns `None`
        genres_data = game_data.get("genres") or []
        genres = ", ".join([g.get("name", "Unknown") for g in genres_data]) or "N/A"

        platforms_data = game_data.get("platforms") or []
        platforms = ", ".join([p.get("name", "Unknown") for p in platforms_data]) or "N/A"

        release_date = "Unknown"
        if game_data.get("first_release_date"):
            dt = datetime.datetime.fromtimestamp(game_data["first_release_date"], tz=datetime.timezone.utc)
            release_date = dt.strftime("%B %d, %Y")

        # Safely check that 'cover' is a dict before accessing 'image_id'
        cover_url = None
        if isinstance(game_data.get("cover"), dict) and "image_id" in game_data["cover"]:
            cover_url = f"https://images.igdb.com/igdb/image/upload/t_cover_big/{game_data['cover']['image_id']}.jpg"

        # Calculate subscriber count for current server
        subscriber_count = 0
        if interaction.guild and "id" in game_data:
            game_id, _ = await resolve_to_base_game(game_data["id"], title)
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute("SELECT user_id FROM subscriptions WHERE game_id = ?", (game_id,)) as cursor:
                    rows = await cursor.fetchall()
            subscriber_count = sum(1 for row in rows if interaction.guild.get_member(row[0]))

        embed = discord.Embed(
            title=title,
            description=summary[:2048],
            url=url,
            color=discord.Color.blurple()
        )

        if cover_url:
            embed.set_thumbnail(url=cover_url)

        embed.add_field(name="🎮 Genres", value=genres, inline=True)
        embed.add_field(name="⭐ Rating", value=rating, inline=True)
        embed.add_field(name="📅 Release Date", value=release_date, inline=True)
        embed.add_field(name="💻 Platforms", value=platforms, inline=True)
        embed.add_field(name="👥 Subscribers in Server", value=str(subscriber_count), inline=True)
        embed.set_footer(text="Data provided by IGDB")

        await interaction.followup.send(embed=embed, ephemeral=not public)

    @app_commands.command(name="set_subscribed_indicator", description="Set a custom icon/indicator for your subscribed games in autocomplete")
    @app_commands.describe(indicator="Custom icon, emoji, or text (e.g. 🔥, [SUB], ⭐). Max 10 chars.")
    async def set_subscribed_indicator(self, interaction: discord.Interaction, indicator: str):
        clean_indicator = indicator.strip()
        if len(clean_indicator) > 10:
            await interaction.response.send_message("Indicator is too long! Keep it under 10 characters.", ephemeral=True)
            return

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """
                INSERT INTO user_settings (user_id, guild_id, sub_indicator)
                VALUES (?, 0, ?)
                ON CONFLICT(user_id, guild_id) DO UPDATE SET sub_indicator = excluded.sub_indicator
                """,
                (interaction.user.id, clean_indicator)
            )
            await db.commit()

        await interaction.response.send_message(
            f"✅ **Subscribed game indicator updated!**\n\n"
            f"Subscribed games in autocomplete will now show: `{clean_indicator} Game Title`",
            ephemeral=True
        )

    @app_commands.command(name="view_subscribed_indicator", description="View your current custom subscribed game indicator")
    async def view_subscribed_indicator(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                    "SELECT sub_indicator FROM user_settings WHERE user_id = ? AND guild_id = 0",
                    (interaction.user.id,)
            ) as cursor:
                row = await cursor.fetchone()

        indicator = row[0] if (row and row[0]) else "⭐"

        await interaction.response.send_message(
            f"Your current subscribed game indicator is: `{indicator}`\n"
            f"**Preview in search:** `{indicator} Helldivers 2`",
            ephemeral=True
        )

    @app_commands.command(name="reset_subscribed_indicator", description="Reset your subscribed game indicator back to star (⭐)")
    async def reset_subscribed_indicator(self, interaction: discord.Interaction):
        async def do_reset(_inter: discord.Interaction):
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    """
                    INSERT INTO user_settings (user_id, guild_id, sub_indicator)
                    VALUES (?, 0, '⭐')
                    ON CONFLICT(user_id, guild_id) DO UPDATE SET sub_indicator = '⭐'
                    """,
                    (interaction.user.id,)
                )
                await db.commit()

        view = GenericConfirmationView(
            author_id=interaction.user.id,
            confirm_label="Reset Indicator",
            confirm_message="✅ Your subscribed indicator has been reset to `⭐`!",
            cancel_message="Action canceled. Your indicator setting was preserved.",
            on_confirm=do_reset
        )

        await interaction.response.send_message(
            "Are you sure you want to reset your subscribed game indicator back to default (`⭐`)?",
            view=view,
            ephemeral=True
        )

    @app_commands.command(name="quiet_mode", description="Toggle or set quiet mode notifications")
    @app_commands.describe(
        enabled="Set True to mute notifications, False to unmute, or leave empty to toggle",
        scope="Apply setting globally across all servers or only to this current server"
    )
    @app_commands.choices(scope=[
        app_commands.Choice(name="Global (All Servers)", value="global"),
        app_commands.Choice(name="Current Server Only", value="server")
    ])
    async def quiet_mode(
        self,
        interaction: discord.Interaction,
        enabled: bool = None,
        scope: app_commands.Choice[str] = None
    ):
        # 1. Immediately defer response
        await interaction.response.defer(ephemeral=True)

        user_id = interaction.user.id
        chosen_scope = scope.value if scope else "global"

        if chosen_scope == "server" and not interaction.guild_id:
            await interaction.followup.send(
                "❌ Server-scoped quiet mode can only be configured inside a server channel.",
                ephemeral=True
            )
            return

        guild_id = interaction.guild_id if chosen_scope == "server" else 0

        try:
            async with aiosqlite.connect(DB_PATH) as db:
                if enabled is None:
                    # Query existing state for this scope
                    async with db.execute(
                        "SELECT quiet_mode FROM user_settings WHERE user_id = ? AND guild_id = ?",
                        (user_id, guild_id)
                    ) as cursor:
                        row = await cursor.fetchone()
                        current_state = row[0] if row else 0
                        new_state = 1 if current_state == 0 else 0
                else:
                    new_state = 1 if enabled else 0

                if chosen_scope == "global":
                    # Update quiet_mode across all server entries for this user
                    await db.execute(
                        "UPDATE user_settings SET quiet_mode = ? WHERE user_id = ?",
                        (new_state, user_id)
                    )
                    # Ensure global row (guild_id = 0) exists
                    await db.execute(
                        """
                        INSERT INTO user_settings (user_id, guild_id, quiet_mode) 
                        VALUES (?, 0, ?)
                        ON CONFLICT(user_id, guild_id) DO UPDATE SET quiet_mode = excluded.quiet_mode
                        """,
                        (user_id, new_state)
                    )
                else:
                    # Native UPSERT targeting composite key (user_id, guild_id) for specific server
                    await db.execute(
                        """
                        INSERT INTO user_settings (user_id, guild_id, quiet_mode) 
                        VALUES (?, ?, ?)
                        ON CONFLICT(user_id, guild_id) DO UPDATE SET quiet_mode = excluded.quiet_mode
                        """,
                        (user_id, guild_id, new_state)
                    )

                await db.commit()

            status_text = "enabled 🔕" if new_state == 1 else "disabled 🔔"
            scope_text = "this server" if chosen_scope == "server" else "all servers (Global)"

            await interaction.followup.send(
                f"Quiet mode is now **{status_text}** for **{scope_text}**.",
                ephemeral=True
            )

        except Exception as e:
            # Catch exceptions so the interaction completes instead of hanging indefinitely
            await interaction.followup.send(
                f"❌ An error occurred while updating quiet mode: `{e}`",
                ephemeral=True
            )

    @app_commands.command(name="help", description="Show information about available bot commands")
    @app_commands.describe(public="Set to True to send response publicly in channel (Default: False)")
    async def help_command(self, interaction: discord.Interaction, public: bool = False):
        # Lookup helper: supports standalone commands or group subcommands
        def fmt(name: str, sub: str = None) -> str:
            cid = getattr(self.bot, "command_ids", {}).get(name)
            if sub:
                full_cmd = f"{name} {sub}"
                return f"</{full_cmd}:{cid}>" if cid else f"`/{full_cmd}`"
            return f"</{name}:{cid}>" if cid else f"`/{name}`"

        embed = discord.Embed(
            title="Bot Command Guide",
            description=(
                "Here is everything you can do with the bot:\n"
                "-# Options marked `[public]` are visible only to you by default. Set `public: True` to share them in channel."
            ),
            color=discord.Color.blue()
        )

        embed.add_field(
            name="Game Info & Subscriptions",
            value=(
                f"• {fmt('game_info')} `<game>` `[public]` — Get details, genres, and cover image for a game.\n"
                f"• {fmt('subscribe')} `<game>` — Subscribe to LFG notifications.\n"
                f"• {fmt('unsubscribe')} `<game>` — Unsubscribe from active notifications.\n"
                f"• {fmt('user_subscriptions')} `[user]` `[public]` — View active game subscriptions."
            ),
            inline=False
        )

        embed.add_field(
            name="Server Activity & Social",
            value=(
                f"• {fmt('ping_game')} `<game>` `[public]` — Send an LFG ping mentioning subscribers.\n"
                f"• {fmt('quiet_mode')} `[enabled]` `[scope]` — Toggle or set quiet mode notifications globally or per-server.\n"
                f"• {fmt('set_ping_template')} `<template>` — Set custom template for `/ping_game` notifications.\n"
                f"• {fmt('view_ping_template')} — View your saved custom ping format.\n"
                f"• {fmt('reset_ping_template')} — Reset ping template to default.\n"
                f"• {fmt('set_subscribed_indicator')} `<indicator>` — Set custom icon for subscribed games in autocomplete.\n"
                f"• {fmt('view_subscribed_indicator')} — View saved autocomplete indicator.\n"
                f"• {fmt('reset_subscribed_indicator')} — Reset autocomplete indicator to default (`⭐`).\n"
                f"• {fmt('top_games')} `[limit]` `[public]` — View popular server games.\n"
                f"• {fmt('show_subscribers')} `<game>` — List subscribers for a game."
            ),
            inline=False
        )

        embed.add_field(
            name="Data & Privacy",
            value=(
                f"• {fmt('delete_my_subscriptions')} — Purge all your active game subscriptions.\n"
                f"• {fmt('delete_all_my_data')} — Purge all game subscriptions and user settings/ping templates."
            ),
            inline=False
        )

        # Conditionally display admin section if user has Administrator permissions or is bot owner
        is_admin = interaction.permissions and interaction.permissions.administrator
        raw_owner_id = os.getenv("BOT_OWNER_ID")
        is_owner = raw_owner_id and raw_owner_id.isdigit() and interaction.user.id == int(raw_owner_id)

        if is_admin or is_owner:
            embed.add_field(name="\u200b", value="─────────────────────────────", inline=False)
            embed.add_field(
                name="Server Administration",
                value=(
                    f"• {fmt('config', 'setup_top_games')} `[limit]` `[interval]` — Post a live-updating top games message.\n"
                    f"• {fmt('config', 'edit_top_games')} `<message>` `[limit]` `[interval]` — Edit tracker settings or force a refresh.\n"
                    f"• {fmt('config', 'force_update_top_games')} `<message>` — Force an immediate refresh on a live tracker.\n"
                    f"• {fmt('config', 'remove_top_games')} `<message>` — Stop tracking and delete a live top games message.\n"
                    f"• {fmt('config', 'set_lfg_channels')} `<mode>` `[channels]` — Restrict, allow, or disable `/ping_game` server-wide.\n"
                    f"• {fmt('config', 'server_overview')} — View subscription details for members in the server."
                ),
                inline=False
            )

        embed.set_footer(text="Looking for a missing game or DLC? Type its full name in /subscribe!")

        await interaction.response.send_message(embed=embed, ephemeral=not public)

async def setup(bot):
    await bot.add_cog(UserCommands(bot))