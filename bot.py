import os
import asyncio
from dotenv import load_dotenv

load_dotenv()

import discord
from discord import app_commands
from discord.ext import commands

from database import init_db, auto_migrate_multiplayer_flags
from utils import get_bot_owner_id

TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

# Custom Bot subclass to cleanly initialize tasks and command syncs
class GameBot(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.command_ids = {}

    async def setup_hook(self):
        await init_db()

        # Clean extension loading across all modules
        await self.load_extension("cogs.user_cmds")
        await self.load_extension("cogs.owner_cmds")
        await self.load_extension("cogs.admin_cmds")
        await self.load_extension("cogs.background_tasks")

        asyncio.create_task(auto_migrate_multiplayer_flags())

client = GameBot(command_prefix="!", intents=intents)

# Manual owner-only sync command (prevents startup rate-limits and command invalidation)
@client.command(name="sync")
async def sync_commands(ctx: commands.Context, guild_id: int = None):
    if ctx.author.id != get_bot_owner_id():
        return

    if guild_id:
        # Guild-specific sync (instant update for testing)
        target_guild = discord.Object(id=guild_id)
        client.tree.copy_global_to(guild=target_guild)
        synced = await client.tree.sync(guild=target_guild)
        await ctx.send(f"✅ Synced {len(synced)} command(s) to guild `{guild_id}`.")
    else:
        # Global sync across all servers
        synced = await client.tree.sync()
        await ctx.send(f"🌐 Synced {len(synced)} command(s) globally.")

    # Update cached command IDs for help command mentions
    try:
        fetched_cmds = await client.tree.fetch_commands(guild=discord.Object(id=guild_id)) if guild_id else await client.tree.fetch_commands()
        client.command_ids = {cmd.name: cmd.id for cmd in fetched_cmds}
    except Exception as e:
        print(f"[Help] Failed to update cached command IDs: {e}")

@client.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    # Handle cooldowns first and return early
    if isinstance(error, app_commands.CommandOnCooldown):
        seconds = round(error.retry_after)
        msg = f"⏳ Easy there! You can use this command again in **{seconds} seconds**."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
        return

    # Handle general permission/check failures
    if isinstance(error, app_commands.CheckFailure):
        msg = "⛔ You do not have permission to run this administrative command."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
        return

@client.event
async def on_ready():
    print(f"Logged in as {client.user}")

    # Fetch existing command IDs on startup without re-syncing tree
    try:
        fetched_cmds = await client.tree.fetch_commands()
        client.command_ids = {cmd.name: cmd.id for cmd in fetched_cmds}
        print(f"[Help] Cached {len(client.command_ids)} command IDs.")
    except Exception as e:
        print(f"[Help] Failed to fetch command IDs: {e}")

client.run(TOKEN)