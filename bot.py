import os
from dotenv import load_dotenv

load_dotenv()

import asyncio
import discord
from discord import app_commands
from discord.ext import commands

from database import init_db, auto_migrate_multiplayer_flags
from cogs.admin_cmds import admin_group
from cogs.background_tasks import update_igdb_catalog, update_live_top_games_loop

TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

# Cache for command IDs used by clickable help mentions
COMMAND_IDS = {}

# Custom Bot subclass to cleanly initialize tasks and command syncs
class GameBot(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.command_ids = {}

    async def setup_hook(self):
        await init_db()

        # Load cog extension
        await self.load_extension("cogs.user_cmds")

        # Register admin command group
        self.tree.add_command(admin_group)

        # Launch automatic DB classification and purge in background
        asyncio.create_task(auto_migrate_multiplayer_flags())

        if not update_igdb_catalog.is_running():
            update_igdb_catalog.start()

        # Start the hourly live top games task
        if not update_live_top_games_loop.is_running():
            update_live_top_games_loop.start(self)

        synced = await self.tree.sync()
        print(f"Synced {len(synced)} global command(s)")


client = GameBot(command_prefix="!", intents=intents)


@update_igdb_catalog.before_loop
async def before_catalog_update():
    await client.wait_until_ready()

@client.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        msg = "⛔ Only the bot owner can use administrative commands."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
        return

    if isinstance(error, app_commands.CommandOnCooldown):
        seconds = round(error.retry_after)
        msg = f"⏳ Easy there! You can ping a game again in **{seconds} seconds**."

        # Check if the interaction was already responded to or deferred
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

@client.event
async def on_ready():
    print(f"Logged in as {client.user}")

    # Populate command IDs on client instance for clickable help mentions
    try:
        fetched_cmds = await client.tree.fetch_commands()
        client.command_ids = {cmd.name: cmd.id for cmd in fetched_cmds}
        print(f"[Help] Cached {len(client.command_ids)} command IDs.")
    except Exception as e:
        print(f"[Help] Failed to fetch command IDs: {e}")

client.run(TOKEN)