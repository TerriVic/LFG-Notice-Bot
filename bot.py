import os
import sqlite3
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Retrieve the token safely
TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.guilds = True
intents.members = True

client = commands.Bot(command_prefix="!", intents=intents)
tree = client.tree

db = sqlite3.connect("db.sqlite")
cursor = db.cursor()


async def autocomplete_games(interaction: discord.Interaction, current: str):
    cursor.execute(
        "SELECT name FROM igdb_games WHERE name LIKE ? LIMIT 25",
        (f"%{current}%",)
    )
    rows = cursor.fetchall()

    return [
        app_commands.Choice(name=row[0], value=row[0])
        for row in rows
    ]


@tree.command(name="subscribe", description="Subscribe to a game")
@app_commands.autocomplete(game=autocomplete_games)
async def subscribe(interaction: discord.Interaction, game: str):
    cursor.execute("SELECT id FROM igdb_games WHERE name = ?", (game,))
    row = cursor.fetchone()

    if not row:
        await interaction.response.send_message("Game not found.")
        return

    game_id = row[0]

    cursor.execute(
        "INSERT OR IGNORE INTO subscriptions (user_id, game_id) VALUES (?, ?)",
        (interaction.user.id, game_id)
    )
    db.commit()

    await interaction.response.send_message(f"You subscribed to **{game}**")

@tree.command(name="unsubscribe", description="Unsubscribe from a game")
@app_commands.autocomplete(game=autocomplete_games)
async def unsubscribe(interaction: discord.Interaction, game: str):
    # Convert game name → IGDB ID
    cursor.execute("SELECT id FROM igdb_games WHERE name = ?", (game,))
    row = cursor.fetchone()

    if not row:
        await interaction.response.send_message("Game not found.")
        return

    game_id = row[0]

    # Delete using the IGDB ID
    cursor.execute(
        "DELETE FROM subscriptions WHERE user_id = ? AND game_id = ?",
        (interaction.user.id, game_id)
    )
    db.commit()

    await interaction.response.send_message(f"You unsubscribed from **{game}**")

@tree.command(name="pinggame", description="Ping all users subscribed to a game")
@app_commands.checks.cooldown(1, 30.0)  # 1 use every 30 seconds per user
@app_commands.autocomplete(game=autocomplete_games)
async def pinggame(interaction: discord.Interaction, game: str):
    cursor.execute("SELECT id FROM igdb_games WHERE name = ?", (game,))
    row = cursor.fetchone()

    if not row:
        await interaction.response.send_message("Game not found.")
        return

    game_id = row[0]

    cursor.execute("SELECT user_id FROM subscriptions WHERE game_id = ?", (game_id,))
    users = cursor.fetchall()

    if not users:
        await interaction.response.send_message("No subscribers for this game.")
        return

    mentions = " ".join(f"<@{u[0]}>" for u in users)
    await interaction.response.send_message(f"Players interested in **{game}**: {mentions}")

@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    # Unwrap original command errors if wrapped
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
    synced = await tree.sync()  # Syncing with no guild argument syncs globally
    print(f"Synced {len(synced)} global command(s)")
    print(f"Logged in as {client.user}")
    

#-------for test server---------
        
##@client.event
##async def on_ready():
##    guild = discord.Object(id=863692047860105216)
##
##    tree.copy_global_to(guild=guild)  # Copies definitions to the guild
##
##    synced = await tree.sync(guild=guild)
##    print(f"Synced {len(synced)} commands to guild {863692047860105216}")
##    print(f"Logged in as {client.user}")

client.run(TOKEN)


