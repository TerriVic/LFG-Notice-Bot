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
import re

# Load environment variables
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

# Setup persistent data path
DATA_DIR = os.getenv("DATA_DIR", ".")
DB_PATH = os.path.join(DATA_DIR, "db.sqlite")

# Ensure the data directory exists before opening SQLite
os.makedirs(DATA_DIR, exist_ok=True)

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

# Regex pattern for searching/matching fallback games
EDITION_PATTERN = re.compile(
    r'[\:\-\–\—\s]*\b(Game of the Year|GOTY|Ultimate|Deluxe|Collector\'s|Enhanced|Definitive|Gold|Complete|Special|Remastered|Edition|Director\'s Cut)\b.*$',
    re.IGNORECASE
)

def normalize_game_name(name: str) -> str:
    """Strips edition suffixes and trailing punctuation from search terms."""
    clean_name = EDITION_PATTERN.sub('', name)
    clean_name = re.sub(r'[\:\-\–\—\s]+$', '', clean_name)
    return clean_name.strip()

def init_db():
    """Ensures required tables and schema setup exist."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS igdb_games (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                updated_at INTEGER,
                first_release_date INTEGER DEFAULT NULL,
                category INTEGER DEFAULT 0,
                parent_game_id INTEGER DEFAULT NULL,
                version_parent_id INTEGER DEFAULT NULL
            );
        """)

        # Migration Check: Ensure new columns exist in pre-existing tables
        cursor.execute("PRAGMA table_info(igdb_games);")
        columns = [column[1] for column in cursor.fetchall()]
        if "category" not in columns:
            cursor.execute("ALTER TABLE igdb_games ADD COLUMN category INTEGER DEFAULT 0;")
        if "first_release_date" not in columns:
            cursor.execute("ALTER TABLE igdb_games ADD COLUMN first_release_date INTEGER DEFAULT NULL;")
        if "parent_game_id" not in columns:
            cursor.execute("ALTER TABLE igdb_games ADD COLUMN parent_game_id INTEGER DEFAULT NULL;")
        if "version_parent_id" not in columns:
            cursor.execute("ALTER TABLE igdb_games ADD COLUMN version_parent_id INTEGER DEFAULT NULL;")

        # Retain Main Games (0), DLCs/Addons (1), Expansions (2), Remakes/Remasters (8, 9, 10, 11)
        cursor.execute("""
            DELETE FROM igdb_games 
            WHERE category NOT IN (0, 1, 2, 8, 9, 10, 11) 
               OR version_parent_id IS NOT NULL;
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
    print("[DB Setup] Database schema initialized.")

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

@tasks.loop(hours=168)
async def update_igdb_catalog():
    print("[IGDB Sync] Starting scheduled catalog update...")

    client_id = os.getenv("TWITCH_CLIENT_ID")
    access_token = os.getenv("TWITCH_ACCESS_TOKEN")

    if not client_id or not access_token:
        print("[IGDB Sync] Error: Missing TWITCH_CLIENT_ID or TWITCH_ACCESS_TOKEN.")
        return

    headers = {
        "Client-ID": client_id,
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json"
    }

    async with aiosqlite.connect(DB_PATH) as db_async:
        async with db_async.execute("SELECT MAX(id) FROM igdb_games") as async_cursor:
            row = await async_cursor.fetchone()
            last_id = row[0] if (row and row[0] is not None) else 0

        limit = 500
        total_upserted = 0

        async with aiohttp.ClientSession() as session:
            while True:
                # Filter BOTH parent_game AND version_parent directly in the API query
                body = (
                    f"fields id, name, updated_at, first_release_date, category, version_parent, parent_game; "
                    f"where id > {last_id} & version_parent = null; "
                    f"sort id asc; "
                    f"limit {limit};"
                )

                async with session.post(
                        "https://api.igdb.com/v4/games",
                        headers=headers,
                        data=body
                ) as response:
                    if response.status != 200:
                        err_text = await response.text()
                        print(f"[IGDB Sync] API Error {response.status}: {err_text}")
                        break

                    games = await response.json()

                if not isinstance(games, list) or len(games) == 0:
                    break

                last_id = games[-1]["id"]

                # Allow Main Game (0), DLC (1), Expansion (2), Remaster/Remake (8, 9, 10, 11)
                valid_categories = {0, 1, 2, 8, 9, 10, 11}
                filtered_games = [
                    g for g in games
                    if (g.get("category") in valid_categories or g.get("category") is None)
                    and g.get("parent_game") is None
                    and g.get("version_parent") is None
                ]

                if not filtered_games:
                    await asyncio.sleep(0.25)
                    continue

                records = []
                for g in filtered_games:
                    records.append((
                        g["id"],
                        g["name"],
                        g.get("updated_at", int(time.time())),
                        g.get("first_release_date"),
                        g.get("category", 0),
                        g.get("parent_game"),
                        None
                    ))

                await db_async.executemany(
                    """
                    INSERT INTO igdb_games (id, name, updated_at, first_release_date, category, parent_game_id, version_parent_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        name = excluded.name,
                        updated_at = excluded.updated_at,
                        first_release_date = excluded.first_release_date,
                        category = excluded.category,
                        parent_game_id = excluded.parent_game_id,
                        version_parent_id = excluded.version_parent_id
                    """,
                    records
                )
                await db_async.commit()

                total_upserted += len(records)
                print(f"[IGDB Sync] Batch synced: {len(records)} games | Current Max ID: {last_id}")
                await asyncio.sleep(0.25)

        print(f"[IGDB Sync] Complete! Updated/Added {total_upserted} games.")


@update_igdb_catalog.before_loop
async def before_catalog_update():
    await client.wait_until_ready()

async def autocomplete_games(interaction: discord.Interaction, current: str):
    clean_current = current.strip()

    async with aiosqlite.connect(DB_PATH) as db_async:
        if not clean_current:
            async with db_async.execute(
                    """
                    SELECT name, first_release_date, COUNT(*) OVER (PARTITION BY name) as name_count
                    FROM igdb_games
                    GROUP BY name
                    ORDER BY first_release_date DESC, name ASC
                    LIMIT 25
                    """
            ) as async_cursor:
                rows = await async_cursor.fetchall()
        else:
            # 1. Exact match tiering guarantees base titles aren't squeezed out
            # 2. Starts-with match comes next
            # 3. Contains match comes last
            async with db_async.execute(
                    """
                    SELECT name, first_release_date, COUNT(*) OVER (PARTITION BY name) as name_count
                    FROM igdb_games
                    WHERE name LIKE ?
                    GROUP BY name
                    ORDER BY
                        CASE
                            WHEN LOWER(name) = LOWER(?) THEN 1
                            WHEN name LIKE ? THEN 2
                            ELSE 3
                            END,
                        LENGTH(name) ASC,
                        first_release_date ASC
                    LIMIT 25
                    """,
                    (f"%{clean_current}%", clean_current, f"{clean_current}%")
            ) as async_cursor:
                rows = await async_cursor.fetchall()

    choices = []
    for name, release_date, name_count in rows:
        # Append release year if multiple entries share the exact title
        if name_count > 1 and release_date:
            year = time.strftime("%Y", time.gmtime(release_date))
            display_name = f"{name} ({year})"
        else:
            display_name = name

        choices.append(
            app_commands.Choice(
                name=display_name[:100],
                value=name[:100]
            )
        )

    # Sort retrieved items alphabetically (A-Z)
    choices.sort(key=lambda choice: choice.name.lower())

    return choices

async def autocomplete_user_subscriptions(interaction: discord.Interaction, current: str):
    cursor.execute(
        """
        SELECT g.name
        FROM subscriptions s
                 JOIN igdb_games g ON s.game_id = g.id
        WHERE s.user_id = ? AND g.name LIKE ?
        ORDER BY g.name ASC
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
        app_commands.Choice(
            name=row[0][:100],
            value=row[0][:100]
        )
        for row in rows
    ]


async def fetch_and_cache_game_from_igdb(game_name: str) -> tuple[int, str] | None:
    client_id = os.getenv("TWITCH_CLIENT_ID")
    access_token = os.getenv("TWITCH_ACCESS_TOKEN")

    if not client_id or not access_token:
        return None

    headers = {
        "Client-ID": client_id,
        "Authorization": f"Bearer {access_token}"
    }

    # 1. Apply keyword normalization to the search phrase
    clean_search = normalize_game_name(game_name)
    body = f'fields id, name, category, first_release_date, version_parent, parent_game; search "{clean_search}"; limit 5;'

    async with aiohttp.ClientSession() as session:
        async with session.post("https://api.igdb.com/v4/games", headers=headers, data=body) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()

    if not data:
        return None

    # Find the first entry that is a true base game
    selected = None
    for item in data:
        if item.get("parent_game") is None and item.get("version_parent") is None:
            selected = item
            break

    if not selected:
        selected = data[0]

    game_id = selected["id"]
    official_name = selected["name"]
    category = selected.get("category", 0)
    first_release_date = selected.get("first_release_date")
    vp = selected.get("version_parent")
    version_parent_id = vp.get("id") if isinstance(vp, dict) else vp

    cursor.execute(
        """
        INSERT OR IGNORE INTO igdb_games 
        (id, name, updated_at, first_release_date, category, parent_game_id, version_parent_id) 
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (game_id, official_name, int(time.time()), first_release_date, category, None, version_parent_id)
    )
    db.commit()

    return game_id, official_name


class ConfirmDataDeletionView(discord.ui.View):
    def __init__(self, target_user_id: int):
        super().__init__(timeout=30.0)
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

def resolve_to_base_game(game_id: int, official_name: str) -> tuple[int, str]:
    """Recursively resolves special edition IDs to their true base game entry."""
    cursor.execute("SELECT version_parent_id FROM igdb_games WHERE id = ?", (game_id,))
    row = cursor.fetchone()

    if row and row[0]:
        parent_id = row[0]
        cursor.execute("SELECT id, name FROM igdb_games WHERE id = ?", (parent_id,))
        parent_row = cursor.fetchone()
        if parent_row:
            # Re-resolve in case the parent itself points higher
            return resolve_to_base_game(parent_row[0], parent_row[1])

    return game_id, official_name

@tree.command(name="subscribe", description="Subscribe to a game")
@app_commands.autocomplete(game=autocomplete_games)
async def subscribe(interaction: discord.Interaction, game: str):
    cursor.execute("SELECT id, name FROM igdb_games WHERE name = ?", (game,))
    row = cursor.fetchone()

    if row:
        raw_id, raw_name = row[0], row[1]
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

        raw_id, raw_name = result

    # Resolve special editions to base game
    game_id, official_name = resolve_to_base_game(raw_id, raw_name)

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

    cursor.execute("SELECT id, name FROM igdb_games WHERE name = ?", (game,))
    row = cursor.fetchone()

    if not row:
        await interaction.response.send_message("Game not found.", ephemeral=True)
        return

    game_id, official_name = resolve_to_base_game(row[0], row[1])

    # Directly delete the subscription
    cursor.execute(
        "DELETE FROM subscriptions WHERE user_id = ? AND game_id = ?",
        (interaction.user.id, game_id)
    )
    db.commit()

    await interaction.response.send_message(f"You unsubscribed from **{official_name}**", ephemeral=True)

@tree.command(name="pinggame", description="Ping all users subscribed to a game")
@app_commands.checks.cooldown(1, 30.0)
@app_commands.autocomplete(game=autocomplete_games)
async def pinggame(interaction: discord.Interaction, game: str):
    cursor.execute("SELECT id, name FROM igdb_games WHERE name = ?", (game,))
    row = cursor.fetchone()

    if not row:
        await interaction.response.send_message("Game not found.")
        return

    game_id, official_name = resolve_to_base_game(row[0], row[1])

    cursor.execute("SELECT user_id FROM subscriptions WHERE game_id = ?", (game_id,))
    users = cursor.fetchall()

    if not users:
        await interaction.response.send_message(f"No subscribers for **{official_name}**.")
        return

    mentions = " ".join(f"<@{u[0]}>" for u in users)
    await interaction.response.send_message(f"Players interested in **{official_name}**: {mentions}")

@tree.command(name="usersubscriptions", description="View subscriptions for yourself (default) or another user.")
async def usersubscriptions(interaction: discord.Interaction, user: discord.User = None, public: bool = False):
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

    cursor.execute("SELECT id, name FROM igdb_games WHERE name = ?", (game,))
    row = cursor.fetchone()

    if not row:
        await interaction.response.send_message("Game not found.", ephemeral=True)
        return

    game_id, official_name = resolve_to_base_game(row[0], row[1])

    cursor.execute("SELECT user_id FROM subscriptions WHERE game_id = ?", (game_id,))
    users = cursor.fetchall()

    if not users:
        await interaction.response.send_message(f"No subscribers for **{official_name}**.", ephemeral=not public)
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
            f"No users in **{interaction.guild.name}** are subscribed to **{official_name}**.",
            ephemeral=not public
        )
        return

    mentions = " ".join(local_mentions)

    await interaction.response.send_message(
        f"Players in this server interested in **{official_name}** ({len(local_mentions)}):\n{mentions}",
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


