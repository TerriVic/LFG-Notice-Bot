import asyncio
import os
import time

import aiohttp
import aiosqlite
import discord
from discord.ext import tasks

from database import DB_PATH
from utils import (
    extract_id,
    GENERIC_DLC_PATTERN
)


@tasks.loop(hours=168)
async def update_igdb_catalog():
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
                # Filter BOTH parent_game AND version_parent directly in the API query
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

                # Periodic progress logging (every 5 batches / 2,500 evaluated items)
                if page_count % 5 == 0:
                    print(f"[IGDB Sync] Batch {page_count}: Upserted {len(records)} games (Total added/updated: {total_upserted}). Last ID: {last_id}")

                await asyncio.sleep(0.3)

        print(f"[IGDB Sync] Finished catalog sync. Added/Updated {total_upserted} games across {page_count} batches.")

async def refresh_live_top_games(bot: discord.Client):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT message_id, guild_id, channel_id, entry_limit FROM live_top_games") as cursor:
            tracked = await cursor.fetchall()

        if not tracked:
            return

        async with db.execute("SELECT s.user_id, g.name FROM subscriptions s JOIN igdb_games g ON s.game_id = g.id") as cursor:
            sub_rows = await cursor.fetchall()

        for message_id, guild_id, channel_id, entry_limit in tracked:
            guild = bot.get_guild(guild_id)
            if not guild:
                continue

            channel = guild.get_channel(channel_id)
            if not channel:
                continue

            try:
                message = await channel.fetch_message(message_id)
            except (discord.NotFound, discord.Forbidden):
                # Target message was deleted or channel permissions lost
                await db.execute("DELETE FROM live_top_games WHERE message_id = ?", (message_id,))
                await db.commit()
                continue

            # Calculate game counts for users in this guild
            game_counts = {}
            for user_id, game_name in sub_rows:
                if guild.get_member(user_id):
                    game_counts[game_name] = game_counts.get(game_name, 0) + 1

            if not game_counts:
                description = "No active game subscriptions in this server."
            else:
                # Limit per message configuration
                sorted_games = sorted(game_counts.items(), key=lambda x: x[1], reverse=True)[:entry_limit]
                formatted_list = [
                    f"`#{idx + 1}` **{gname}** — {cnt} {'subscriber' if cnt == 1 else 'subscribers'}"
                    for idx, (gname, cnt) in enumerate(sorted_games)
                ]
                description = "\n".join(formatted_list)

            embed = discord.Embed(
                title=f"📊 Live Top {entry_limit} Games in {guild.name}",
                description=description,
                color=discord.Color.green()
            )
            embed.set_footer(text="Updates automatically every hour")

            await message.edit(content=None, embed=embed)

@tasks.loop(minutes=30)
async def update_live_top_games_loop(bot: discord.Client):
    await refresh_live_top_games(bot)