import time
import typing

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

from cogs.background_tasks import refresh_live_top_games
from database import DB_PATH
from utils import (
    format_interval,
    get_bot_owner_id,
    GenericConfirmationView,
    autocomplete_set_lfg_channels,
    autocomplete_whitelisted_channels_only,
    build_quickstart_embed
)


def is_owner_or_moderator():
    async def predicate(interaction: discord.Interaction) -> bool:
        # 1. Allow Bot Owner anywhere (bypasses missing server permissions)
        if interaction.user.id == get_bot_owner_id():
            return True

            # 2. Allow Server Moderators in their guild
        if interaction.permissions and interaction.permissions.manage_messages:
            return True

        return False

    check_decor = app_commands.check(predicate)
    check_decor.predicate = predicate  # Expose predicate for testing
    return check_decor

async def _autocomplete_guild_messages(
        interaction: discord.Interaction,
        current: str,
        table_name: str,
        empty_label: str,
        extra_fields: str = "",
        extra_label_fn=None,
) -> list[app_commands.Choice[str]]:
    if not interaction.guild:
        return []

    fields = f"message_id, channel_id, created_at{f', {extra_fields}' if extra_fields else ''}"

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
                f"SELECT {fields} FROM {table_name} WHERE guild_id = ? ORDER BY created_at DESC",
                (interaction.guild.id,)
        ) as cursor:
            rows = await cursor.fetchall()

    if not rows:
        return [app_commands.Choice(name=empty_label, value="__NONE__")]

    choices = []
    for row in rows:
        msg_id, ch_id, created_at = row[0], row[1], row[2]
        channel = interaction.guild.get_channel(ch_id)
        channel_name = f"#{channel.name}" if channel else f"Channel {ch_id}"
        time_str = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(created_at)) if created_at else "Unknown date"

        extra_info = extra_label_fn(row) if extra_label_fn else ""
        label = f"{time_str} | {channel_name}{extra_info} | ID: {msg_id}"

        if not current or current.lower() in label.lower() or current in str(msg_id):
            choices.append(app_commands.Choice(name=label[:100], value=str(msg_id)))

    return choices[:25]


async def autocomplete_live_messages(interaction: discord.Interaction, current: str):
    return await _autocomplete_guild_messages(
        interaction=interaction,
        current=current,
        table_name="live_top_games",
        empty_label="No active live top game messages found",
        extra_fields="entry_limit, update_interval",
        extra_label_fn=lambda row: f" | Top {row[3]} ({row[4]}h)",
    )

async def autocomplete_quickstart_messages(interaction: discord.Interaction, current: str):
    return await _autocomplete_guild_messages(
        interaction=interaction,
        current=current,
        table_name="quickstart_messages",
        empty_label="No active quickstart messages found",
    )

async def update_quickstart_messages(
        bot: commands.Bot,
        guild_id: typing.Optional[int] = None,
        target_message_id: typing.Optional[int] = None
) -> int:
    """Edits posted quickstart guide message(s) with the latest embed format."""
    embed = build_quickstart_embed(bot)
    updated_count = 0

    async with aiosqlite.connect(DB_PATH) as db:
        if target_message_id:
            query = "SELECT message_id, channel_id FROM quickstart_messages WHERE message_id = ?"
            params = (target_message_id,)
        elif guild_id:
            query = "SELECT message_id, channel_id FROM quickstart_messages WHERE guild_id = ?"
            params = (guild_id,)
        else:
            query = "SELECT message_id, channel_id FROM quickstart_messages"
            params = ()

        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()

    for msg_id, ch_id in rows:
        channel = bot.get_channel(ch_id)
        if not channel:
            try:
                channel = await bot.fetch_channel(ch_id)
            except (discord.NotFound, discord.Forbidden):
                continue

        if channel:
            try:
                msg = await channel.fetch_message(msg_id)
                await msg.edit(embed=embed)
                updated_count += 1
            except (discord.NotFound, discord.Forbidden):
                pass

    return updated_count

class AdminCommands(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # Declare group inside the cog
    mod_group = app_commands.Group(
        name="config",
        description="Server configuration commands"
    )

    @mod_group.command(name="setup_top_games", description="Post a live-updating top games message in this channel")
    @app_commands.describe(
        limit="Number of top games to display (1-25, default: 10)",
        interval="Update frequency in hours (e.g., 0.25 for 15m, 0.5 for 30m, default: 1)"
    )
    @is_owner_or_moderator()
    async def setup_top_games(
            self,
            interaction: discord.Interaction,
            limit: app_commands.Range[int, 1, 25] = 10,
            interval: app_commands.Range[float, 0.25, 168.0] = 1.0
    ):
        if not interaction.guild:
            await interaction.response.send_message("This command must be run inside a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        clean_limit = max(1, min(limit, 25))
        clean_interval = max(0.25, min(interval, 168.0))

        # Internal helper to create the tracker message and DB entry
        async def create_tracker() -> str:
            embed = discord.Embed(
                title=f"📊 Live Top {clean_limit} Games",
                description="Initializing...",
                color=discord.Color.gold()
            )

            try:
                message = await interaction.channel.send(embed=embed)
            except discord.Forbidden:
                return "❌ Bot lacks permission to send messages or embeds in this channel."

            try:
                async with aiosqlite.connect(DB_PATH) as inner_db:
                    await inner_db.execute(
                        """
                        INSERT INTO live_top_games (message_id, guild_id, channel_id, entry_limit, update_interval, last_updated, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (message.id, interaction.guild.id, interaction.channel.id, clean_limit, clean_interval, 0, int(time.time()))
                    )
                    await inner_db.commit()

                await refresh_live_top_games(self.bot, force=True)
                return f"✅ Live top games tracker posted! (Top {clean_limit}, {format_interval(clean_interval)})"
            except Exception as e:
                print(f"[Setup Error] {e}")
                # noinspection PyBroadException
                try:
                    await message.delete()
                except Exception:
                    pass
                return f"❌ Failed to set up live tracker: `{e}`"

        # Check for existing tracker in this server
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT channel_id, created_at FROM live_top_games WHERE guild_id = ? ORDER BY created_at DESC LIMIT 1",
                (interaction.guild.id,)
            ) as cursor:
                existing = await cursor.fetchone()

        if existing:
            ch_id, created_at = existing
            time_formatted = f"<t:{created_at}:f> (<t:{created_at}:R>)" if created_at else "an unknown date"

            async def do_confirm(_inter: discord.Interaction):
                result_text = await create_tracker()
                view.confirm_message = result_text

            view = GenericConfirmationView(
                author_id=interaction.user.id,
                confirm_label="Create Tracker Anyway",
                cancel_message="❌ Setup canceled. No new tracker message was posted.",
                on_confirm=do_confirm,
                timeout=60.0
            )

            await interaction.followup.send(
                f"⚠️ A live top games tracker already exists in <#{ch_id}>, sent on {time_formatted}.\n"
                "Are you sure you want to create another one?",
                view=view,
                ephemeral=True
            )
        else:
            status_text = await create_tracker()
            await interaction.followup.send(status_text, ephemeral=True)

    @mod_group.command(name="remove_top_games", description="Delete a live top games tracker message")
    @app_commands.describe(message="Select the live tracker message to remove")
    @app_commands.autocomplete(message=autocomplete_live_messages)
    @is_owner_or_moderator()
    async def remove_top_games(self, interaction: discord.Interaction, message: str):
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

    @mod_group.command(name="force_update_top_games", description="Force an immediate refresh on a live top games tracker")
    @app_commands.describe(message="Select the tracker message to refresh")
    @app_commands.autocomplete(message=autocomplete_live_messages)
    @is_owner_or_moderator()
    async def force_update_top_games(self, interaction: discord.Interaction, message: str):
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
            async with db.execute("SELECT message_id FROM live_top_games WHERE message_id = ?", (target_msg_id,)) as cursor:
                row = await cursor.fetchone()

            if not row:
                await interaction.followup.send("Tracker message not found in database.", ephemeral=True)
                return

        await refresh_live_top_games(self.bot, force=True, target_message_id=target_msg_id)
        await interaction.followup.send("Live top games tracker refreshed successfully!", ephemeral=True)

    # --- Edit Command (with Force-Update Fallback) ---
    @mod_group.command(name="edit_top_games", description="Edit tracker settings or force a refresh if options are left empty")
    @app_commands.describe(
        message="Select the tracker message to edit",
        limit="New number of top games to display (1-25, optional)",
        interval="New update frequency in hours e.g. 0.5 (optional)"
    )
    @app_commands.autocomplete(message=autocomplete_live_messages)
    @is_owner_or_moderator()
    async def edit_top_games(
            self,
            interaction: discord.Interaction,
            message: str,
            limit: typing.Optional[app_commands.Range[int, 1, 25]] = None,
            interval: typing.Optional[app_commands.Range[float, 0.25, 168.0]] = None
    ):
        if message == "__NONE__":
            await interaction.response.send_message("No live tracker message selected.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        try:
            target_msg_id = int(message)
        except ValueError:
            await interaction.followup.send("Invalid message selection.", ephemeral=True)
            return

        # Fallback: If no settings were changed, perform a force update on this tracker
        if limit is None and interval is None:
            await refresh_live_top_games(self.bot, force=True, target_message_id=target_msg_id)
            await interaction.followup.send(
                "No configuration changes requested. Live tracker force-refreshed!",
                ephemeral=True
            )
            return

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT entry_limit, update_interval FROM live_top_games WHERE message_id = ?",
                (target_msg_id,)
            ) as cursor:
                row = await cursor.fetchone()

            if not row:
                await interaction.followup.send("Tracker message not found in database.", ephemeral=True)
                return

            current_limit, current_interval = row

            new_limit = max(1, min(limit, 25)) if limit is not None else current_limit
            new_interval = max(0.25, min(interval, 168.0)) if interval is not None else current_interval

            await db.execute(
                "UPDATE live_top_games SET entry_limit = ?, update_interval = ? WHERE message_id = ?",
                (new_limit, new_interval, target_msg_id)
            )
            await db.commit()

        await refresh_live_top_games(self.bot, force=True, target_message_id=target_msg_id)

        changes = []
        if limit is not None:
            changes.append(f"Limit ➔ **Top {new_limit}**")
        if interval is not None:
            changes.append(f"Interval ➔ **{format_interval(new_interval).capitalize()}**")

        await interaction.followup.send(
            f"✅ Tracker message updated successfully! ({', '.join(changes)})",
            ephemeral=True
        )

    @mod_group.command(
        name="server_overview",
        description="View subscription details for members in this server"
    )
    @is_owner_or_moderator()
    async def server_overview(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("This command must be run inside a server.", ephemeral=True)
            return

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
            await interaction.followup.send("📊 **Server Overview:** No active subscriptions found.", ephemeral=True)
            return

        # Aggregate subscriptions per user ID, keeping ONLY members in this guild
        user_map: dict[int, list[str]] = {}
        total_subscriptions = 0

        for user_id, game_name in rows:
            # Check if user is currently in this server cache
            member = interaction.guild.get_member(user_id)
            if member:
                user_map.setdefault(user_id, []).append(game_name)
                total_subscriptions += 1

        if not user_map:
            await interaction.followup.send(
                f"📊 **Server Overview:** No members in **{interaction.guild.name}** have active subscriptions.",
                ephemeral=True
            )
            return

        total_users = len(user_map)

        # Build description lines
        lines = [
            f"**Total Server Subscribers:** {total_users} | **Total Subscriptions:** {total_subscriptions}\n"
        ]

        for user_id, games in user_map.items():
            member = interaction.guild.get_member(user_id)
            user_label = f"**{member.display_name}** (<@{user_id}>)" if member else f"<@{user_id}>"

            games_str = ", ".join(f"`{g}`" for g in games)
            lines.append(f"• {user_label} ({len(games)}): {games_str}")

        # Chunk lines into multiple embeds if message exceeds Discord's character limits
        embeds = []
        current_embed = discord.Embed(
            title=f"🏰 Subscriptions Overview for **{interaction.guild.name}**",
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

        # Send through followup messages
        await interaction.followup.send(embed=embeds[0], ephemeral=True)
        for extra_embed in embeds[1:]:
            await interaction.followup.send(embed=extra_embed, ephemeral=True)

    # --- LFG Channel Management Commands ---

    @mod_group.command(
        name="set_lfg_channels",
        description="Allow all channels or set/add specific channels to the LFG whitelist"
    )
    @app_commands.describe(
        mode="Select channel access mode",
        channel="Select a channel to enable/whitelist (marked with ✅ if already whitelisted)"
    )
    @app_commands.choices(mode=[
        app_commands.Choice(name="Allow All Channels", value="all"),
        app_commands.Choice(name="Whitelist Specific Channels", value="whitelist")
    ])
    @app_commands.autocomplete(channel=autocomplete_set_lfg_channels)
    @is_owner_or_moderator()
    async def set_lfg_channels(
            self,
            interaction: discord.Interaction,
            mode: app_commands.Choice[str],
            channel: typing.Optional[str] = None
    ):
        if not interaction.guild:
            await interaction.response.send_message("This command must be run inside a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        selected_mode = mode.value

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                    "SELECT whitelisted_channels FROM guild_lfg_settings WHERE guild_id = ?",
                    (interaction.guild.id,)
            ) as cursor:
                row = await cursor.fetchone()

            existing_channels = set(row[0].split(",")) if row and row[0] else set()

            if selected_mode == "whitelist":
                if channel and channel != "__NONE__":
                    existing_channels.add(channel)

                if not existing_channels:
                    await interaction.followup.send(
                        "❌ You must select at least one channel when setting mode to **Whitelist Specific Channels**.",
                        ephemeral=True
                    )
                    return

                channel_str = ",".join(existing_channels)
            else:
                # Mode 'all': retain recorded whitelist string in case mode changes back
                channel_str = ",".join(existing_channels)

            await db.execute(
                """
                INSERT INTO guild_lfg_settings (guild_id, mode, whitelisted_channels)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                                                    mode = excluded.mode,
                                                    whitelisted_channels = excluded.whitelisted_channels
                """,
                (interaction.guild.id, selected_mode, channel_str)
            )
            await db.commit()

        if selected_mode == "all":
            await interaction.followup.send("✅ LFG notifications are now allowed in **all text channels**.", ephemeral=True)
        else:
            formatted_mentions = " ".join(f"<#{cid}>" for cid in existing_channels if cid)
            await interaction.followup.send(f"✅ LFG notifications set to **Whitelist**. Allowed channels: {formatted_mentions}", ephemeral=True)


    @mod_group.command(
        name="remove_lfg_channels",
        description="Disable all channels or remove a specific channel from the LFG whitelist"
    )
    @app_commands.describe(
        mode="Select action to disable all channels or remove a specific channel",
        channel="Select a whitelisted channel to remove"
    )
    @app_commands.choices(mode=[
        app_commands.Choice(name="Disable/Blacklist All Channels", value="none"),
        app_commands.Choice(name="Disable/Remove Specific Channel", value="remove_specific")
    ])
    @app_commands.autocomplete(channel=autocomplete_whitelisted_channels_only)
    @is_owner_or_moderator()
    async def remove_lfg_channels(
            self,
            interaction: discord.Interaction,
            mode: app_commands.Choice[str],
            channel: typing.Optional[str] = None
    ):
        if not interaction.guild:
            await interaction.response.send_message("This command must be run inside a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        selected_mode = mode.value

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                    "SELECT mode, whitelisted_channels FROM guild_lfg_settings WHERE guild_id = ?",
                    (interaction.guild.id,)
            ) as cursor:
                row = await cursor.fetchone()

            existing_channels = set(row[1].split(",")) if row and row[1] else set()

            if selected_mode == "none":
                # Set mode to 'none' across server
                await db.execute(
                    """
                    INSERT INTO guild_lfg_settings (guild_id, mode, whitelisted_channels)
                    VALUES (?, 'none', ?)
                    ON CONFLICT(guild_id) DO UPDATE SET mode = 'none'
                    """,
                    (interaction.guild.id, ",".join(existing_channels))
                )
                await db.commit()

                await interaction.followup.send("🚫 LFG notifications have been **disabled across all channels** for this server.", ephemeral=True)
                return

            # Removing a specific channel from the whitelist
            if not channel or channel == "__NONE__":
                await interaction.followup.send("❌ Please select a valid whitelisted channel to remove.", ephemeral=True)
                return

            if channel not in existing_channels:
                await interaction.followup.send("⚠️ The selected channel is not currently in the whitelist.", ephemeral=True)
                return

            existing_channels.remove(channel)
            new_channel_str = ",".join(existing_channels)

            await db.execute(
                "UPDATE guild_lfg_settings SET whitelisted_channels = ? WHERE guild_id = ?",
                (new_channel_str, interaction.guild.id)
            )
            await db.commit()

        if existing_channels:
            remaining_mentions = " ".join(f"<#{cid}>" for cid in existing_channels if cid)
            await interaction.followup.send(
                f"🗑️ Removed <#{channel}> from LFG whitelist.\n📌 Remaining allowed channels: {remaining_mentions}",
                ephemeral=True
            )
        else:
            await interaction.followup.send(
                f"🗑️ Removed <#{channel}> from LFG whitelist.\n⚠️ The whitelist is now empty.",
                ephemeral=True
            )

    @mod_group.command(
        name="view_lfg_channels",
        description="View current LFG notification channel configurations for this server"
    )
    @is_owner_or_moderator()
    async def view_lfg_channels(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("This command must be run inside a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                    "SELECT mode, whitelisted_channels FROM guild_lfg_settings WHERE guild_id = ?",
                    (interaction.guild.id,)
            ) as cursor:
                row = await cursor.fetchone()

        if not row:
            await interaction.followup.send("⚙️ LFG channel settings are currently set to **default (Allow All Channels)**.", ephemeral=True)
            return

        mode, channels_str = row
        channel_ids = [cid for cid in channels_str.split(",") if cid]

        if mode == "all":
            mode_desc = "✅ **Allow All Channels** (LFG notifications allowed everywhere)"
        elif mode == "none":
            mode_desc = "🚫 **Disabled Server-Wide** (No LFG notifications permitted)"
        else:
            mode_desc = "🎯 **Whitelist Mode** (LFG notifications restricted to specific channels)"

        channels_desc = " ".join(f"<#{cid}>" for cid in channel_ids) if channel_ids else "*None configured*"

        embed = discord.Embed(
            title=f"⚙️ LFG Channel Settings for {interaction.guild.name}",
            color=discord.Color.blue()
        )
        embed.add_field(name="Current Mode", value=mode_desc, inline=False)
        embed.add_field(name="Whitelisted Channels", value=channels_desc, inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)

    @mod_group.command(
        name="ping_cooldown",
        description="View or set how often the same game may be pinged in this server"
    )
    @app_commands.describe(
        minutes="Cooldown in minutes (default: 0.5 / 30s; use 0 to disable)"
    )
    @is_owner_or_moderator()
    async def ping_cooldown(
            self,
            interaction: discord.Interaction,
            minutes: typing.Optional[app_commands.Range[float, 0.0, 1440.0]] = None
    ):
        if not interaction.guild:
            await interaction.response.send_message("This command must be run inside a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        if minutes is None:
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute(
                        "SELECT game_ping_cooldown_seconds FROM guild_lfg_settings WHERE guild_id = ?",
                        (interaction.guild.id,)
                ) as cursor:
                    row = await cursor.fetchone()

            cooldown_seconds = row[0] if row and row[0] is not None else 30
            if cooldown_seconds <= 0:
                description = (
                    "Disabled — the same game may be pinged again immediately.\n"
                    "-# Default: disabled (0 minutes)."
                )
            else:
                cooldown_minutes = cooldown_seconds / 60
                display_minutes = f"{cooldown_minutes:g}"
                minute_unit = "minute" if cooldown_minutes == 1 else "minutes"
                second_unit = "second" if cooldown_seconds == 1 else "seconds"
                description = (
                    f"The same game may be pinged once every **{display_minutes} {minute_unit}** "
                    f"(**{cooldown_seconds} {second_unit}**).\n"
                    "-# Default: 30 seconds (0.5 minutes)."
                )

            await interaction.followup.send(
                f"⏱️ **Per-game ping cooldown:** {description}",
                ephemeral=True
            )
            return

        cooldown_seconds = int(round(minutes * 60))

        # SQLite stores cooldowns as whole seconds. Extremely small positive
        # values that round below one second are not useful, so reject them.
        if minutes > 0 and cooldown_seconds < 1:
            await interaction.followup.send(
                "❌ The smallest supported positive cooldown is **1 second** "
                "(`0.0167` minutes).",
                ephemeral=True
            )
            return

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """
                INSERT INTO guild_lfg_settings (guild_id, game_ping_cooldown_seconds)
                VALUES (?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    game_ping_cooldown_seconds = excluded.game_ping_cooldown_seconds
                """,
                (interaction.guild.id, cooldown_seconds)
            )

            # Disabling the feature should also reset old cooldown history so
            # re-enabling it later starts with a clean slate.
            if cooldown_seconds == 0:
                await db.execute(
                    "DELETE FROM guild_game_ping_state WHERE guild_id = ?",
                    (interaction.guild.id,)
                )

            await db.commit()

        if cooldown_seconds == 0:
            message = (
                "✅ Per-game ping cooldowns are now **disabled** for this server.\n"
                "-# Default: disabled (0 minutes)."
            )
        else:
            cooldown_minutes = cooldown_seconds / 60
            display_minutes = f"{cooldown_minutes:g}"
            minute_unit = "minute" if cooldown_minutes == 1 else "minutes"
            second_unit = "second" if cooldown_seconds == 1 else "seconds"
            message = (
                f"✅ The same game can now be pinged once every "
                f"**{display_minutes} {minute_unit}** "
                f"(**{cooldown_seconds} {second_unit}**) in this server."
            )

        await interaction.followup.send(message, ephemeral=True)

    @mod_group.command(
        name="ping_roles",
        description="Control which roles may use /ping_game (default: everyone)"
    )
    @app_commands.describe(
        action="Add/remove a role, allow everyone, or view the current setting",
        role="Role to add or remove (required for Add/Remove)"
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="Add Allowed Role", value="add"),
        app_commands.Choice(name="Remove Allowed Role", value="remove"),
        app_commands.Choice(name="Allow Everyone", value="allow_all"),
        app_commands.Choice(name="View Allowed Roles", value="view")
    ])
    @is_owner_or_moderator()
    async def ping_roles(
            self,
            interaction: discord.Interaction,
            action: app_commands.Choice[str],
            role: typing.Optional[discord.Role] = None
    ):
        if not interaction.guild:
            await interaction.response.send_message("This command must be run inside a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        selected_action = action.value

        if selected_action in {"add", "remove"} and role is None:
            await interaction.followup.send(
                "❌ Please select a role for this action.",
                ephemeral=True
            )
            return

        if role is not None and role.id == interaction.guild.id:
            await interaction.followup.send(
                "ℹ️ `@everyone` already means everyone can ping. Use **Allow Everyone** instead.",
                ephemeral=True
            )
            return

        async with aiosqlite.connect(DB_PATH) as db:
            if selected_action == "add":
                cursor = await db.execute(
                    "INSERT OR IGNORE INTO guild_ping_roles (guild_id, role_id) VALUES (?, ?)",
                    (interaction.guild.id, role.id)
                )
                await db.commit()

                if cursor.rowcount == 0:
                    message = f"ℹ️ {role.mention} is already allowed to use `/ping_game`."
                else:
                    message = (
                        f"✅ Added {role.mention}. Members with this role may now use `/ping_game`."
                    )

            elif selected_action == "remove":
                cursor = await db.execute(
                    "DELETE FROM guild_ping_roles WHERE guild_id = ? AND role_id = ?",
                    (interaction.guild.id, role.id)
                )
                await db.commit()

                if cursor.rowcount == 0:
                    message = f"ℹ️ {role.mention} was not in the allowed ping-role list."
                else:
                    async with db.execute(
                            "SELECT 1 FROM guild_ping_roles WHERE guild_id = ? LIMIT 1",
                            (interaction.guild.id,)
                    ) as check_cursor:
                        has_remaining_roles = await check_cursor.fetchone()

                    if has_remaining_roles:
                        message = f"🗑️ Removed {role.mention} from the allowed ping-role list."
                    else:
                        message = (
                            f"🗑️ Removed {role.mention}. No restricted roles remain, so "
                            "**everyone may use `/ping_game` again**."
                        )

            elif selected_action == "allow_all":
                await db.execute(
                    "DELETE FROM guild_ping_roles WHERE guild_id = ?",
                    (interaction.guild.id,)
                )
                await db.commit()
                message = "✅ Ping-role restrictions cleared. **Everyone may use `/ping_game`.**"

            else:  # view
                async with db.execute(
                        "SELECT role_id FROM guild_ping_roles WHERE guild_id = ? ORDER BY role_id",
                        (interaction.guild.id,)
                ) as cursor:
                    role_rows = await cursor.fetchall()

                configured_ids = [row[0] for row in role_rows]
                valid_roles = []
                stale_ids = []
                for role_id in configured_ids:
                    role_obj = interaction.guild.get_role(role_id)
                    if role_obj:
                        valid_roles.append(role_obj)
                    else:
                        stale_ids.append(role_id)

                if stale_ids:
                    await db.executemany(
                        "DELETE FROM guild_ping_roles WHERE guild_id = ? AND role_id = ?",
                        [(interaction.guild.id, role_id) for role_id in stale_ids]
                    )
                    await db.commit()

                if not valid_roles:
                    message = (
                        "👥 **Allowed ping roles:** Everyone may use `/ping_game`.\n"
                        "-# Default: everyone may ping until one or more allowed roles are added."
                    )
                else:
                    role_mentions = " ".join(role_obj.mention for role_obj in valid_roles)
                    message = (
                        "👥 **Allowed ping roles:** " + role_mentions +
                        "\n-# Bot owner and server moderators can always bypass this restriction."
                    )

        await interaction.followup.send(
            message,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none()
        )

    @mod_group.command(name="setup_quickstart", description="Post a permanent Quickstart Guide embed in this channel")
    @is_owner_or_moderator()
    async def setup_quickstart(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("This command must be run inside a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        async def create_quickstart() -> str:
            embed = build_quickstart_embed(self.bot)

            try:
                message = await interaction.channel.send(embed=embed)
            except discord.Forbidden:
                return "❌ Bot lacks permission to send embeds in this channel."

            try:
                async with aiosqlite.connect(DB_PATH) as inner_db:
                    await inner_db.execute(
                        """
                        INSERT INTO quickstart_messages (message_id, guild_id, channel_id, created_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (message.id, interaction.guild.id, interaction.channel.id, int(time.time()))
                    )
                    await inner_db.commit()
                return "✅ Quickstart guide embed posted successfully!"
            except Exception as e:
                print(f"[Quickstart Setup Error] {e}")
                try:
                    await message.delete()
                except Exception:
                    pass
                return f"❌ Failed to set up quickstart guide: `{e}`"

        # Check for existing quickstart message in this server
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                    "SELECT channel_id, created_at FROM quickstart_messages WHERE guild_id = ? ORDER BY created_at DESC LIMIT 1",
                    (interaction.guild.id,)
            ) as cursor:
                existing = await cursor.fetchone()

        if existing:
            ch_id, created_at = existing
            time_formatted = f"<t:{created_at}:f> (<t:{created_at}:R>)" if created_at else "an unknown date"

            async def do_confirm(_inter: discord.Interaction):
                result_text = await create_quickstart()
                view.confirm_message = result_text

            view = GenericConfirmationView(
                author_id=interaction.user.id,
                confirm_label="Create Quickstart Anyway",
                cancel_message="❌ Setup canceled. No new quickstart guide was posted.",
                on_confirm=do_confirm,
                timeout=60.0
            )

            await interaction.followup.send(
                f"⚠️ A Quickstart Guide already exists in <#{ch_id}>, sent on {time_formatted}.\n"
                "Are you sure you want to create another one?",
                view=view,
                ephemeral=True
            )
        else:
            status_text = await create_quickstart()
            await interaction.followup.send(status_text, ephemeral=True)

    @mod_group.command(name="update_quickstart", description="Refresh active Quickstart Guide message(s) to the latest format")
    @app_commands.describe(message="Select a specific quickstart message to refresh, or leave blank to update all")
    @app_commands.autocomplete(message=autocomplete_quickstart_messages)
    @is_owner_or_moderator()
    async def update_quickstart(
            self,
            interaction: discord.Interaction,
            message: typing.Optional[str] = None
    ):
        if not interaction.guild:
            await interaction.response.send_message("This command must be run inside a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        target_msg_id = None
        if message and message != "__NONE__":
            try:
                target_msg_id = int(message)
            except ValueError:
                await interaction.followup.send("Invalid message selection.", ephemeral=True)
                return

        updated = await update_quickstart_messages(
            self.bot,
            guild_id=interaction.guild.id,
            target_message_id=target_msg_id
        )

        if updated == 0:
            await interaction.followup.send("⚠️ No matching quickstart messages were updated.", ephemeral=True)
        else:
            await interaction.followup.send(f"🔄 Successfully updated {updated} Quickstart Guide message(s)!", ephemeral=True)

    @mod_group.command(name="remove_quickstart", description="Delete a Quickstart Guide message")
    @app_commands.describe(message="Select the quickstart message to remove")
    @app_commands.autocomplete(message=autocomplete_quickstart_messages)
    @is_owner_or_moderator()
    async def remove_quickstart(self, interaction: discord.Interaction, message: str):
        if message == "__NONE__":
            await interaction.response.send_message("No quickstart message selected.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        try:
            target_msg_id = int(message)
        except ValueError:
            await interaction.followup.send("Invalid message selection.", ephemeral=True)
            return

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT channel_id FROM quickstart_messages WHERE message_id = ?", (target_msg_id,)) as cursor:
                row = await cursor.fetchone()

            if not row:
                await interaction.followup.send("Quickstart message not found in database.", ephemeral=True)
                return

            channel_id = row[0]
            channel = interaction.guild.get_channel(channel_id)
            if channel:
                try:
                    msg = await channel.fetch_message(target_msg_id)
                    await msg.delete()
                except (discord.NotFound, discord.Forbidden):
                    pass

            await db.execute("DELETE FROM quickstart_messages WHERE message_id = ?", (target_msg_id,))
            await db.commit()

        await interaction.followup.send("🗑️ Quickstart guide removed successfully.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminCommands(bot))