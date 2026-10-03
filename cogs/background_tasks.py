import asyncio
import os
import time
import typing

import aiohttp
import aiosqlite
import discord
from discord.ext import commands, tasks

from database import DB_PATH
from utils import (
    extract_id,
    GENERIC_DLC_PATTERN, format_interval,
    format_command_mention as fmt
)


async def refresh_live_top_games(
    bot: discord.Client,
    force: bool = False,
    target_message_id: typing.Optional[int] = None
):
    now = int(time.time())

    async with aiosqlite.connect(DB_PATH) as db:
        if target_message_id:
            query = "SELECT message_id, guild_id, channel_id, entry_limit, update_interval, last_updated FROM live_top_games WHERE message_id = ?"
            params = (target_message_id,)
        else:
            query = "SELECT message_id, guild_id, channel_id, entry_limit, update_interval, last_updated FROM live_top_games"
            params = ()

        async with db.execute(query, params) as cursor:
            tracked = await cursor.fetchall()

        if not tracked:
            return

        async with db.execute("SELECT s.user_id, g.name FROM subscriptions s JOIN igdb_games g ON s.game_id = g.id") as cursor:
            sub_rows = await cursor.fetchall()

        for message_id, guild_id, channel_id, entry_limit, update_interval, last_updated in tracked:
            elapsed_seconds = now - (last_updated or 0)
            required_seconds = (update_interval * 3600) - 10

            if not force and target_message_id is None and elapsed_seconds < required_seconds:
                continue

            guild = bot.get_guild(guild_id)
            if not guild:
                continue

            channel = guild.get_channel(channel_id)
            if not channel:
                continue

            try:
                message = await channel.fetch_message(message_id)
            except (discord.NotFound, discord.Forbidden):
                await db.execute("DELETE FROM live_top_games WHERE message_id = ?", (message_id,))
                await db.commit()
                continue

            game_counts = {}
            for user_id, game_name in sub_rows:
                if guild.get_member(user_id):
                    game_counts[game_name] = game_counts.get(game_name, 0) + 1

            if not game_counts:
                description = "No active game subscriptions in this server."
            else:
                sorted_games = sorted(game_counts.items(), key=lambda x: x[1], reverse=True)[:entry_limit]
                formatted_list = [
                    f"`#{idx + 1}` **{gname}** — {cnt} {'subscriber' if cnt == 1 else 'subscribers'}"
                    for idx, (gname, cnt) in enumerate(sorted_games)
                ]
                description = "\n".join(formatted_list)

            interval_str = format_interval(update_interval)
            description += f"\n\n*Last updated <t:{now}:R> • Schedule: {interval_str}*"
            description += f"\n-# Tip: Use {fmt(bot, 'subscribe')} to subscribe to a game!"

            embed = discord.Embed(
                title=f"📊 Live Top {entry_limit} Games in {guild.name}",
                description=description,
                color=discord.Color.green()
            )

            await message.edit(content=None, embed=embed)

            await db.execute("UPDATE live_top_games SET last_updated = ? WHERE message_id = ?", (now, message_id))
            await db.commit()


class BackgroundTasks(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def cog_load(self):
        """Automatically start tasks when the Cog loads."""
        if not self.update_igdb_catalog.is_running():
            self.update_igdb_catalog.start()
        if not self.update_live_top_games_loop.is_running():
            self.update_live_top_games_loop.start()

    def cog_unload(self):
        """Cleanly cancel tasks if the Cog is unmonitored or reloaded."""
        self.update_igdb_catalog.cancel()
        self.update_live_top_games_loop.cancel()

    @tasks.loop(hours=168)
    async def update_igdb_catalog(self):
        print("[IGDB Sync] Starting catalog sync...")

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
            page_count = 0

            async with aiohttp.ClientSession() as session:
                while True:
                    body = (
                        f"fields id, name, updated_at, first_release_date, category, version_parent, parent_game, game_modes, total_rating_count; "
                        f"where id > {last_id} & version_parent = null & parent_game = null & game_modes = (2, 3, 4, 5, 6); "
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
                    page_count += 1

                    valid_categories = {0, 8, 9, 10, 11}
                    filtered_games = []

                    for g in games:
                        cat = g.get("category")
                        p_game = extract_id(g.get("parent_game"))
                        v_parent = extract_id(g.get("version_parent"))
                        g_name = g.get("name", "")

                        if (cat in valid_categories or cat is None) and p_game is None and v_parent is None:
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
                            v_parent,
                            1,
                            g.get("total_rating_count", 0)
                        ))

                    await db_async.executemany(
                        """
                        INSERT INTO igdb_games (id, name, updated_at, first_release_date, category, parent_game_id, version_parent_id, is_multiplayer, total_rating_count)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(id) DO UPDATE SET
                            name = excluded.name,
                            updated_at = excluded.updated_at,
                            first_release_date = excluded.first_release_date,
                            category = excluded.category,
                            parent_game_id = excluded.parent_game_id,
                            version_parent_id = excluded.version_parent_id,
                            is_multiplayer = excluded.is_multiplayer,
                            total_rating_count = excluded.total_rating_count
                        """,
                        records
                    )
                    await db_async.commit()

                    total_upserted += len(records)

                    if page_count % 5 == 0:
                        print(f"[IGDB Sync] Batch {page_count}: Upserted {len(records)} games (Total added/updated: {total_upserted}). Last ID: {last_id}")

                    await asyncio.sleep(0.3)

            print(f"[IGDB Sync] Finished catalog sync. Added/Updated {total_upserted} games across {page_count} batches.")

    @update_igdb_catalog.before_loop
    async def before_catalog_update(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=1)
    async def update_live_top_games_loop(self):
        await refresh_live_top_games(self.bot, force=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(BackgroundTasks(bot))