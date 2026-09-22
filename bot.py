import os
import time
import asyncio
import re
import aiohttp
import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

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

# Regex patterns
EDITION_PATTERN = re.compile(
    r'[\:\-\–\—\s]*\b(Game of the Year|GOTY|Ultimate|Deluxe|Collector\'s|Enhanced|Definitive|Gold|Complete|Special|Remastered|Edition|Director\'s Cut)\b.*$',
    re.IGNORECASE
)

# GENERIC DLC REGEX: Catches any title with a colon/dash followed by common DLC/expansion keywords
GENERIC_DLC_PATTERN = re.compile(
    r'[\:\-\–\—]\s*.*?\b(DLC|Expansion|Pack|Add-on|Season Pass|Bonus Content|Soundtrack|Artbook|Episode|Chapter|Mission|Quest|Map Pack|Skin|Bundle)\b',
    re.IGNORECASE
)

def normalize_game_name(name: str) -> str:
    """Strips edition suffixes, standardizes mobile quotes, and removes trailing punctuation."""
    clean_name = name.replace("’", "'").replace("‘", "'")
    clean_name = EDITION_PATTERN.sub('', clean_name)
    clean_name = re.sub(r'[\:\-\–\—\s]+$', '', clean_name)
    return clean_name.strip()

def extract_id(val):
    """Utility to safely extract ID integer whether IGDB returns int or dict."""
    if isinstance(val, dict):
        return val.get("id")
    return val

async def init_db():
    """Ensures required tables and schema setup exist asynchronously."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
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

        async with db.execute("PRAGMA table_info(igdb_games);") as cursor:
            columns = [column[1] for column in await cursor.fetchall()]

        if "category" not in columns:
            await db.execute("ALTER TABLE igdb_games ADD COLUMN category INTEGER DEFAULT 0;")
        if "first_release_date" not in columns:
            await db.execute("ALTER TABLE igdb_games ADD COLUMN first_release_date INTEGER DEFAULT NULL;")
        if "parent_game_id" not in columns:
            await db.execute("ALTER TABLE igdb_games ADD COLUMN parent_game_id INTEGER DEFAULT NULL;")
        if "version_parent_id" not in columns:
            await db.execute("ALTER TABLE igdb_games ADD COLUMN version_parent_id INTEGER DEFAULT NULL;")

        # Purge non-main entries
        await db.execute("""
               DELETE FROM igdb_games
               WHERE category NOT IN (0, 8, 9, 10, 11)
               OR version_parent_id IS NOT NULL
               OR parent_game_id IS NOT NULL;
        """)

        await db.execute("""
            DELETE FROM igdb_games 
            WHERE name LIKE '%:% DLC%' 
               OR name LIKE '%-% DLC%'
               OR name LIKE '%:% Expansion%'
               OR name LIKE '%-% Expansion%'
               OR name LIKE '%:% Pack%'
               OR name LIKE '%-% Pack%'
               OR name LIKE '%:% Season Pass%'
               OR name LIKE '%:% Add-on%';
               """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                user_id INTEGER NOT NULL,
                game_id INTEGER NOT NULL,
                UNIQUE(user_id, game_id)
            );
        """)
        await db.commit()
    print("[DB Setup] Database schema initialized and cleaned.")

# Custom Bot subclass to cleanly initialize tasks and command syncs
class GameBot(commands.Bot):
    async def setup_hook(self):
        await init_db()
        if not update_igdb_catalog.is_running():
            update_igdb_catalog.start()

        # Global command sync
        synced = await self.tree.sync()
        print(f"Synced {len(synced)} global command(s)")


client = GameBot(command_prefix="!", intents=intents)
tree = client.tree

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
                    f"fields id, name, updated_at, first_release_date, category, version_parent, parent_game, game_modes; "
                    f"where id > {last_id} & version_parent = null & parent_game = null & game_modes = (2, 3, 4, 5); "
                    f"sort id asc; "
                    f"limit {limit};"
                )

                async with session.post(
                        "https://api.igdb.com/v4/games",
                        headers=headers,
                        data=body
                ) as response:
                    if response.status == 429:
                        print("[IGDB Sync] Rate limited. Waiting 2 seconds...")
                        await asyncio.sleep(2.0)
                        continue
                    elif response.status != 200:
                        err_text = await response.text()
                        print(f"[IGDB Sync] API Error {response.status}: {err_text}")
                        break

                    games = await response.json()

                if not isinstance(games, list) or len(games) == 0:
                    break

                last_id = games[-1]["id"]

                # Allow Main Game (0), Remaster/Remake (8, 9, 10, 11)
                valid_categories = {0, 8, 9, 10, 11}
                filtered_games = []

                for g in games:
                    cat = g.get("category")
                    p_game = extract_id(g.get("parent_game"))
                    v_parent = extract_id(g.get("version_parent"))
                    g_name = g.get("name", "")

                    if (cat in valid_categories or cat is None) and p_game is None and v_parent is None:
                        # Generic Regex Check: Skip titles matching DLC/Expansion keywords in subtitles
                        if GENERIC_DLC_PATTERN.search(g_name):
                            continue
                        filtered_games.append((g, p_game, v_parent))

                if not filtered_games:
                    await asyncio.sleep(0.3)
                    continue

                records = []
                for g, p_game, v_parent in filtered_games:
                    records.append((
                        g["id"],
                        g["name"],
                        g.get("updated_at", int(time.time())),
                        g.get("first_release_date"),
                        g.get("category", 0),
                        p_game,
                        v_parent
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
                await asyncio.sleep(0.3)

        print(f"[IGDB Sync] Complete! Updated/Added {total_upserted} games.")


@update_igdb_catalog.before_loop
async def before_catalog_update():
    await client.wait_until_ready()

async def autocomplete_games(interaction: discord.Interaction, current: str):
    clean_current = current.strip().replace("’", "'").replace("‘", "'")

    # Detect if user explicitly typed a subtitle delimiter or DLC keyword
    user_explicitly_searching_dlc = any(char in clean_current for char in [":", "-", "–", "—"]) or \
                                    any(k in clean_current.lower() for k in ["dlc", "expansion", "pack"])

    base_search_term = re.split(r'[\:\-\–\—]', clean_current)[0].strip()

    choices = []

    # 1. DELIMITER SEARCH: Query IGDB live if user explicitly types DLC delimiters/keywords
    if user_explicitly_searching_dlc and len(base_search_term) > 2:
        client_id = os.getenv("TWITCH_CLIENT_ID")
        access_token = os.getenv("TWITCH_ACCESS_TOKEN")

        if client_id and access_token:
            headers = {
                "Client-ID": client_id,
                "Authorization": f"Bearer {access_token}"
            }
            body = f'fields name; search "{clean_current}"; limit 25;'

            try:
                timeout = aiohttp.ClientTimeout(total=1.5)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post("https://api.igdb.com/v4/games", headers=headers, data=body) as resp:
                        if resp.status == 200:
                            igdb_results = await resp.json()
                            for item in igdb_results:
                                g_name = item.get("name", "")
                                choices.append(app_commands.Choice(name=g_name[:100], value=g_name[:100]))
            except Exception:
                pass

    # 2. LOCAL MULTIPLAYER DATABASE SEARCH
    if not user_explicitly_searching_dlc:
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

        for name, release_date, name_count in rows:
            if name_count > 1 and release_date:
                year = time.strftime("%Y", time.gmtime(release_date))
                display_name = f"{name} ({year})"
            else:
                display_name = name

            choices.append(app_commands.Choice(name=display_name[:100], value=name[:100]))

    # 3. LIVE IGDB FALLBACK & SUPPLEMENT: Run if search has 3+ chars and fewer than 25 local results
    if len(choices) < 25 and clean_current and len(clean_current) >= 3 and not user_explicitly_searching_dlc:
        client_id = os.getenv("TWITCH_CLIENT_ID")
        access_token = os.getenv("TWITCH_ACCESS_TOKEN")

        if client_id and access_token:
            headers = {
                "Client-ID": client_id,
                "Authorization": f"Bearer {access_token}"
            }
            # Query main games (category = 0, 8, 9, 10, 11) and filter out parents
            body = (
                f'fields name, category, version_parent, parent_game; '
                f'search "{clean_current}"; '
                f'where version_parent = null & parent_game = null; '
                f'limit 25;'
            )

            try:
                timeout = aiohttp.ClientTimeout(total=1.5)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post("https://api.igdb.com/v4/games", headers=headers, data=body) as resp:
                        if resp.status == 200:
                            igdb_results = await resp.json()
                            for item in igdb_results:
                                g_name = item.get("name", "")

                                # Skip generic DLC patterns unless user explicitly searched for DLCs
                                if GENERIC_DLC_PATTERN.search(g_name):
                                    continue

                                choices.append(app_commands.Choice(name=g_name[:100], value=g_name[:100]))
            except Exception:
                pass

    # Deduplicate while preserving unique display choices
    unique_choices = {choice.name: choice for choice in choices}.values()
    sorted_choices = sorted(list(unique_choices), key=lambda choice: choice.name.lower())
    return sorted_choices[:25]

async def autocomplete_user_subscriptions(interaction: discord.Interaction, current: str):
    clean_current = current.strip().replace("’", "'").replace("‘", "'")
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
        """
        SELECT g.name
        FROM subscriptions s
                 JOIN igdb_games g ON s.game_id = g.id
        WHERE s.user_id = ? AND g.name LIKE ?
        ORDER BY g.name ASC
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


async def fetch_and_cache_game_from_igdb(game_name: str, allow_dlc: bool = False) -> tuple[int, str] | None:
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
    body = f'fields id, name, category, first_release_date, version_parent, parent_game; search "{clean_search}"; limit 10;'

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
        p_game = extract_id(item.get("parent_game"))
        v_parent = extract_id(item.get("version_parent"))
        g_name = item.get("name", "")

        # Ignore DLC items unless explicitly permitted by search
        if not allow_dlc and GENERIC_DLC_PATTERN.search(g_name):
            continue

        if p_game is None and v_parent is None:
            selected = item
            break

    if not selected:
        selected = data[0]

    game_id = selected["id"]
    official_name = selected["name"]
    category = selected.get("category", 0)
    first_release_date = selected.get("first_release_date")
    vp = extract_id(selected.get("version_parent"))
    p_game = extract_id(selected.get("parent_game"))

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
        """
        INSERT OR IGNORE INTO igdb_games 
        (id, name, updated_at, first_release_date, category, parent_game_id, version_parent_id) 
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (game_id, official_name, int(time.time()), first_release_date, category, p_game, vp)
    )
        await db.commit()

    return game_id, official_name

async def resolve_to_base_game(game_id: int, official_name: str) -> tuple[int, str]:
    """Recursively resolves special edition IDs to their true base game entry asynchronously."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT version_parent_id FROM igdb_games WHERE id = ?", (game_id,)) as cursor:
            row = await cursor.fetchone()

        if row and row[0]:
            parent_id = row[0]
            async with db.execute("SELECT id, name FROM igdb_games WHERE id = ?", (parent_id,)) as cursor:
                parent_row = await cursor.fetchone()
            if parent_row:
                return await resolve_to_base_game(parent_row[0], parent_row[1])

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
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM subscriptions WHERE user_id = ?", (interaction.user.id,))
            await db.commit()

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

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
        """
        SELECT s.user_id, g.name
        FROM subscriptions s
                 JOIN igdb_games g ON s.game_id = g.id
        """
        ) as cursor:
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
            already_subbed = await cursor.fetchone()

        if already_subbed:
            await interaction.followup.send(f"You are already subscribed to **{official_name}**!", ephemeral=True)
            return

        await db.execute(
            "INSERT INTO subscriptions (user_id, game_id) VALUES (?, ?)",
            (interaction.user.id, game_id)
        )
        await db.commit()

    await interaction.followup.send(f"You subscribed to **{official_name}**!", ephemeral=True)

@tree.command(name="unsubscribe", description="Unsubscribe from a game")
@app_commands.autocomplete(game=autocomplete_user_subscriptions)
async def unsubscribe(interaction: discord.Interaction, game: str):
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

@tree.command(name="pinggame", description="Ping all users subscribed to a game")
@app_commands.checks.cooldown(1, 30.0)
@app_commands.autocomplete(game=autocomplete_games)
async def pinggame(interaction: discord.Interaction, game: str):
    clean_game = game.strip().replace("’", "'").replace("‘", "'")
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, name FROM igdb_games WHERE name = ?", (clean_game,)) as cursor:
            row = await cursor.fetchone()

    if not row:
        await interaction.response.send_message("Game not found.")
        return

    game_id, official_name = await resolve_to_base_game(row[0], row[1])

    async with db.execute("SELECT user_id FROM subscriptions WHERE game_id = ?", (game_id,)) as cursor:
        users = await cursor.fetchall()

    if not users:
        await interaction.response.send_message(f"No subscribers for **{official_name}**.")
        return

    mentions = " ".join(f"<@{u[0]}>" for u in users)
    await interaction.response.send_message(f"Players interested in **{official_name}**: {mentions}")

@tree.command(name="usersubscriptions", description="View subscriptions for yourself (default) or another user.")
async def usersubscriptions(interaction: discord.Interaction, user: discord.User = None, public: bool = False):
    target_user = user or interaction.user

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
        """
        SELECT g.name
        FROM subscriptions s
        JOIN igdb_games g ON s.game_id = g.id
        WHERE s.user_id = ?
        ORDER BY g.name ASC
        """,
        (target_user.id,)
        ) as cursor:
            rows = await cursor.fetchall()

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

    clean_game = game.strip().replace("’", "'").replace("‘", "'")
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, name FROM igdb_games WHERE name = ?", (clean_game,)) as cursor:
            row = await cursor.fetchone()

    if not row:
        await interaction.response.send_message("Game not found.", ephemeral=True)
        return

    game_id, official_name = await resolve_to_base_game(row[0], row[1])

    async with db.execute("SELECT user_id FROM subscriptions WHERE game_id = ?", (game_id,)) as cursor:
        users = await cursor.fetchall()

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


