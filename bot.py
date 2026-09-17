import os
import sqlite3
import time
import asyncio
import aiohttp
import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Retrieve the token safely
TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.guilds = True
intents.members = True


# Custom Bot subclass to cleanly initialize tasks and command syncs
class GameBot(commands.Bot):
    async def setup_hook(self):
        # Start background tasks safely after the event loop is active
        if not update_igdb_catalog.is_running():
            update_igdb_catalog.start()

        # Global command sync
        synced = await self.tree.sync()
        print(f"Synced {len(synced)} global command(s)")


client = GameBot(command_prefix="!", intents=intents)
tree = client.tree

db = sqlite3.connect("db.sqlite")
cursor = db.cursor()


@tasks.loop(hours=168)  # Runs automatically every 7 days
async def update_igdb_catalog():
    print("[IGDB Sync] Starting scheduled catalog update...")

    client_id = os.getenv("TWITCH_CLIENT_ID")
    access_token = os.getenv("TWITCH_ACCESS_TOKEN")

    if not client_id or not access_token:
        print("[IGDB Sync] Error: Missing TWITCH_CLIENT_ID or TWITCH_ACCESS_TOKEN.")
        return

    headers = {
        "Client-ID": client_id,
        "Authorization": f"Bearer {access_token}"
    }

    async with aiosqlite.connect("db.sqlite") as db_async:
        # 1. Fetch the max updated_at timestamp from local DB
        async with db_async.execute("SELECT MAX(updated_at) FROM igdb_games") as async_cursor:
            row = await async_cursor.fetchone()
            last_updated = row[0] if row and row[0] else 0

        offset = 0
        limit = 500  # IGDB maximum batch limit
        total_upserted = 0

        async with aiohttp.ClientSession() as session:
            while True:
                body = (
                    f"fields id, name, updated_at; "
                    f"where updated_at > {last_updated}; "
                    f"sort updated_at asc; "
                    f"limit {limit}; "
                    f"offset {offset};"
                )

                async with session.post(
                        "https://api.igdb.com/v4/games",
                        headers=headers,
                        data=body
                ) as response:
                    if response.status != 200:
                        print(f"[IGDB Sync] API error {response.status}: {await response.text()}")
                        break

                    games = await response.json()

                if not games:
                    break  # No more new or modified games to fetch

                records = [
                    (g["id"], g["name"], g.get("updated_at", int(time.time())))
                    for g in games
                ]

                # 2. Bulk upsert into local SQLite catalog
                await db_async.executemany(
                    """
                    INSERT INTO igdb_games (id, name, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                                                  name = excluded.name,
                                                  updated_at = excluded.updated_at
                    """,
                    records
                )
                await db_async.commit()

                total_upserted += len(records)
                offset += limit

                # Rate-limit pause to respect IGDB's 4 requests/sec limit
                await asyncio.sleep(0.25)

        print(f"[IGDB Sync] Complete! Updated/Added {total_upserted} games.")


@update_igdb_catalog.before_loop
async def before_catalog_update():
    await client.wait_until_ready()

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

async def autocomplete_user_subscriptions(interaction: discord.Interaction, current: str):
    cursor.execute(
        """
        SELECT g.name
        FROM subscriptions s
                 JOIN igdb_games g ON s.game_id = g.id
        WHERE s.user_id = ? AND g.name LIKE ?
        LIMIT 25
        """,
        (interaction.user.id, f"%{current}%")
    )
    rows = cursor.fetchall()

    if not rows:
        return [
            app_commands.Choice(
                name="No subscribed games found",
                value="__NONE__"
            )
        ]

    return [
        app_commands.Choice(name=row[0], value=row[0])
        for row in rows
    ]

async def fetch_and_cache_game_from_igdb(game_name: str) -> tuple[int, str] | None:
    """
    Queries IGDB live for a missing game, caches it locally in SQLite,
    and returns a tuple of (game_id, official_name) if found.
    """
    client_id = os.getenv("TWITCH_CLIENT_ID")
    access_token = os.getenv("TWITCH_ACCESS_TOKEN")

    if not client_id or not access_token:
        return None

    headers = {
        "Client-ID": client_id,
        "Authorization": f"Bearer {access_token}"
    }

    # Search IGDB for a matching game title
    body = f'fields id, name; search "{game_name}"; limit 1;'

    async with aiohttp.ClientSession() as session:
        async with session.post("https://api.igdb.com/v4/games", headers=headers, data=body) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()

    if not data:
        return None

    game_id = data[0]["id"]
    official_name = data[0]["name"]

    # Cache locally using your synchronous sqlite connection
    cursor.execute(
        "INSERT OR IGNORE INTO igdb_games (id, name, updated_at) VALUES (?, ?, ?)",
        (game_id, official_name, int(time.time()))
    )
    db.commit()

    return game_id, official_name

@tree.command(name="subscribe", description="Subscribe to a game")
@app_commands.autocomplete(game=autocomplete_games)
async def subscribe(interaction: discord.Interaction, game: str):
    # 1. Look up in local SQLite cache
    cursor.execute("SELECT id, name FROM igdb_games WHERE name = ?", (game,))
    row = cursor.fetchone()

    if row:
        game_id, official_name = row[0], row[1]
    else:
        # Defer response slightly in case the live API network request takes a moment
        await interaction.response.defer(ephemeral=True)

        # 2. Live IGDB Fallback: Fetch missing game on the fly
        result = await fetch_and_cache_game_from_igdb(game)
        if not result:
            await interaction.followup.send(
                f"Could not find **{game}** in local cache or on IGDB.",
                ephemeral=True
            )
            return

        game_id, official_name = result

    # 3. Check for existing subscription
    cursor.execute(
        "SELECT 1 FROM subscriptions WHERE user_id = ? AND game_id = ?",
        (interaction.user.id, game_id)
    )
    if cursor.fetchone():
        msg = f"You are already subscribed to **{official_name}**!"
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
        return

    # 4. Insert new subscription
    cursor.execute(
        "INSERT INTO subscriptions (user_id, game_id) VALUES (?, ?)",
        (interaction.user.id, game_id)
    )
    db.commit()

    msg = f"You subscribed to **{official_name}**!"
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


@tree.command(name="unsubscribe", description="Unsubscribe from a game")
@app_commands.autocomplete(game=autocomplete_user_subscriptions)
async def unsubscribe(interaction: discord.Interaction, game: str):
    # Handle the dummy autocomplete choice
    if game == "__NONE__":
        await interaction.response.send_message(
            "You are not subscribed to any games.",
            ephemeral=True
        )
        return

    # Get game ID
    cursor.execute("SELECT id FROM igdb_games WHERE name = ?", (game,))
    row = cursor.fetchone()

    if not row:
        await interaction.response.send_message("Game not found.", ephemeral=True)
        return

    game_id = row[0]

    # Directly delete the subscription (redundant existence check removed)
    cursor.execute(
        "DELETE FROM subscriptions WHERE user_id = ? AND game_id = ?",
        (interaction.user.id, game_id)
    )
    db.commit()

    await interaction.response.send_message(f"You unsubscribed from **{game}**", ephemeral=True)

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

@tree.command(name="usersubscriptions", description="View subscriptions for yourself (default) or another user.")
async def usersubscriptions(interaction: discord.Interaction, user: discord.User = None, public: bool = False):
    # Default to the command caller if no user was provided
    target_user = user or interaction.user

    cursor.execute(
        """
        SELECT g.name
        FROM subscriptions s
                 JOIN igdb_games g ON s.game_id = g.id
        WHERE s.user_id = ?
        ORDER BY g.name ASC
        """,
        (target_user.id,)
    )
    rows = cursor.fetchall()

    # Determine pronoun/name framing for the header
    if target_user.id == interaction.user.id:
        header = "Your subscribed games"
        empty_msg = "You are not subscribed to any games."
    else:
        header = f"Subscribed games for **{target_user.display_name}**"
        empty_msg = f"**{target_user.display_name}** is not subscribed to any games."

    if not rows:
        await interaction.response.send_message(empty_msg, ephemeral=not public)
        return

    game_list = "\n".join(f"• {row[0]}" for row in rows)

    await interaction.response.send_message(
        f"**{header}** ({len(rows)}):\n{game_list}",
        ephemeral=not public
    )

@tree.command(name="showsubscribers", description="View all users in this server subscribed to a game without pinging them")
@app_commands.autocomplete(game=autocomplete_games)
async def showsubscribers(interaction: discord.Interaction, game: str, public: bool = False):
    # Ensure this command is run inside a server
    if not interaction.guild:
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return

    cursor.execute("SELECT id FROM igdb_games WHERE name = ?", (game,))
    row = cursor.fetchone()

    if not row:
        await interaction.response.send_message("Game not found.", ephemeral=True)
        return

    game_id = row[0]

    cursor.execute("SELECT user_id FROM subscriptions WHERE game_id = ?", (game_id,))
    users = cursor.fetchall()

    if not users:
        await interaction.response.send_message(f"No subscribers for **{game}**.", ephemeral=not public)
        return

    # Filter: Keep ONLY users who are currently members of this server
    local_mentions = []
    for u in users:
        user_id = u[0]
        member = interaction.guild.get_member(user_id)
        if member:
            local_mentions.append(f"<@{user_id}>")

    if not local_mentions:
        await interaction.response.send_message(
            f"No users in **{interaction.guild.name}** are subscribed to **{game}**.",
            ephemeral=not public
        )
        return

    mentions = " ".join(local_mentions)

    await interaction.response.send_message(
        f"Players in this server interested in **{game}** ({len(local_mentions)}):\n{mentions}",
        ephemeral=not public,
        allowed_mentions=discord.AllowedMentions.none()
    )

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

## GUILD_ID = os.getenv("GUILD_ID")
##@client.event
##async def on_ready():
##    guild = discord.Object(id=GUILD_ID)
##
##    tree.copy_global_to(guild=guild)  # Copies definitions to the guild
##
##    synced = await tree.sync(guild=guild)
##    print(f"Synced {len(synced)} commands to guild {GUILD_ID}")
##    print(f"Logged in as {client.user}")

client.run(TOKEN)


