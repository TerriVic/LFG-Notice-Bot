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

# Load environment variables
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

# Setup persistent data path (defaults to current dir if DATA_DIR isn't set)
DATA_DIR = os.getenv("DATA_DIR", ".")
DB_PATH = os.path.join(DATA_DIR, "db.sqlite")

# Ensure the data directory exists before opening SQLite
os.makedirs(DATA_DIR, exist_ok=True)

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

def init_db():
    """Ensures all required SQLite tables exist before any queries or tasks run."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()

        # Table 1: Cached IGDB Games (including updated_at for incremental syncing)
        cursor.execute("""
                       CREATE TABLE IF NOT EXISTS igdb_games (
                                                                 id INTEGER PRIMARY KEY,
                                                                 name TEXT NOT NULL,
                                                                 updated_at INTEGER
                       );
                       """)

        # Table 2: User Game Subscriptions
        cursor.execute("""
                       CREATE TABLE IF NOT EXISTS subscriptions (
                                                                    user_id INTEGER NOT NULL,
                                                                    game_id INTEGER NOT NULL,
                                                                    UNIQUE(user_id, game_id)
                       );
                       """)
        conn.commit()
    print("[DB Setup] Database schema initialized with updated_at column.")

# Custom Bot subclass to cleanly initialize tasks and command syncs
class GameBot(commands.Bot):
    async def setup_hook(self):
        # Start background tasks safely after the event loop is active
        # 1. Guarantee DB schema exists BEFORE background tasks run
        init_db()

        # 2. Start background tasks safely after the event loop is active
        if not update_igdb_catalog.is_running():
            update_igdb_catalog.start()

        # 3. Global command sync
        synced = await self.tree.sync()
        print(f"Synced {len(synced)} global command(s)")


client = GameBot(command_prefix="!", intents=intents)
tree = client.tree

db = sqlite3.connect(DB_PATH)
cursor = db.cursor()

class HelpSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Subscriptions", description="Subscribe, unsubscribe, and view games", emoji="🎮"),
            discord.SelectOption(label="Pings & LFG", description="Find players and notify subscribers", emoji="🔔"),
            discord.SelectOption(label="Privacy & Data", description="Manage your stored user data", emoji="🔒"),
        ]
        super().__init__(placeholder="Choose a command category...", options=options)

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "Subscriptions":
            embed = discord.Embed(title="🎮 Subscription Commands", color=discord.Color.blue())
            embed.add_field(name="/subscribe <game>", value="Subscribe to pings for a specific game.", inline=False)
            embed.add_field(name="/unsubscribe <game>", value="Remove your subscription to a game.", inline=False)
            embed.add_field(name="/usersubscriptions", value="View your active game subscriptions.", inline=False)

        elif self.values[0] == "Pings & LFG":
            embed = discord.Embed(title="🔔 Ping & LFG Commands", color=discord.Color.green())
            embed.add_field(name="/pinggame <game>", value="Ping everyone subscribed to a game.", inline=False)
            embed.add_field(name="/showsubscribers <game>", value="See who is subscribed without pinging.", inline=False)

        elif self.values[0] == "Privacy & Data":
            embed = discord.Embed(title="🔒 Privacy & Data Commands", color=discord.Color.red())
            embed.add_field(
                name="/delete_my_data",
                value="Permanently delete all your subscriptions and user data from the bot's database.",
                inline=False
            )

        await interaction.response.edit_message(embed=embed)

class HelpView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)
        self.add_item(HelpSelect())

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

    async with aiosqlite.connect(DB_PATH) as db_async:
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

class ConfirmDataDeletionView(discord.ui.View):
    def __init__(self, target_user_id: int):
        super().__init__(timeout=30.0)  # Auto-expire buttons after 30 seconds
        self.target_user_id = target_user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Ensures only the user who ran the command can press the buttons
        if interaction.user.id != self.target_user_id:
            await interaction.response.send_message("You cannot use these buttons.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm Deletion", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 1. Execute deletion query
        cursor.execute("DELETE FROM subscriptions WHERE user_id = ?", (interaction.user.id,))
        db.commit()

        # 2. Disable buttons and inform the user
        for child in self.children:
            child.disabled = True

        await interaction.response.edit_message(
            content="**All your data has been permanently deleted from our database.**",
            view=self
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True

        await interaction.response.edit_message(
            content="Action canceled. Your data gets to live another day.",
            view=self
        )

@tree.command(name="top_games", description="Show the top games on the server by subscription count")
@app_commands.describe(
    limit="Number of top games to display (default: 10, max: 25)",
    public="Set to True to share the list publicly in channel (default: False)"
)
async def top_games(interaction: discord.Interaction, limit: int = 10, public: bool = False):
    # Guild check
    if not interaction.guild:
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return

    # Cap the maximum limit to prevent huge wall-of-text messages
    limit = max(1, min(limit, 25))

    # Fetch all subscriptions joined with game names
    cursor.execute(
        """
        SELECT s.user_id, g.name
        FROM subscriptions s
                 JOIN igdb_games g ON s.game_id = g.id
        """
    )
    rows = cursor.fetchall()

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

    # Format output message with numbers
    formatted_list = []

    for idx, (game_name, count) in enumerate(sorted_games):
        rank_icon = f"`#{idx + 1}`"
        sub_label = "subscriber" if count == 1 else "subscribers"
        formatted_list.append(f"{rank_icon} **{game_name}** — {count} {sub_label}")

    embed = discord.Embed(
        title=f"Top {len(sorted_games)} Games in {interaction.guild.name}",
        description="\n".join(formatted_list),
        color=discord.Color.gold()
    )

    await interaction.response.send_message(embed=embed, ephemeral=not public)

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

@tree.command(name="delete_my_data", description="Permanently delete all your subscription data from the bot")
async def delete_my_data(interaction: discord.Interaction):
    view = ConfirmDataDeletionView(target_user_id=interaction.user.id)
    await interaction.response.send_message(
        "⚠️ **Are you sure you want to delete all your data?**\n"
        "This will immediately remove all your active game subscriptions.",
        view=view,
        ephemeral=True
    )

@tree.command(name="help", description="Learn how to use the LFG Notice Bot")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="LFG Notice Bot Help",
        description="Select a category below to view detailed command guides.",
        color=discord.Color.blurple()
    )
    await interaction.response.send_message(embed=embed, view=HelpView(), ephemeral=True)

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
    print(f"Logged in as {client.user}")


client.run(TOKEN)
    

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
## client.run(TOKEN)