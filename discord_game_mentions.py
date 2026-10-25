"""Best-effort Discord game mentions. The detectable-games API is undocumented."""
import asyncio
import logging
import re
import time
import unicodedata

import aiohttp
import aiosqlite

CATALOG_URL = "https://discord.com/api/v10/games/detectable"
_CATALOG_TTL = 6 * 60 * 60
_catalog = None
_catalog_expires = 0.0
_catalog_lock = asyncio.Lock()
_log = logging.getLogger("lfg.operations")


def _key(name):
    return " ".join(unicodedata.normalize("NFKC", name).casefold().split())


def _build_index(games):
    """Only unambiguous canonical titles; aliases are too risky for auto-tagging."""
    names = {}
    collisions = set()
    for game in games:
        if not isinstance(game, dict):
            continue
        name, ident = game.get("name"), str(game.get("id", ""))
        if not isinstance(name, str) or not re.fullmatch(r"[1-9]\d{15,21}", ident):
            continue
        key = _key(name)
        if not key:
            continue
        if key in names and names[key] != ident:
            collisions.add(key)
        names[key] = ident
    for key in collisions:
        names.pop(key, None)
    return names


async def _catalog_index():
    global _catalog, _catalog_expires
    if _catalog is not None and time.monotonic() < _catalog_expires:
        return _catalog
    async with _catalog_lock:
        if _catalog is not None and time.monotonic() < _catalog_expires:
            return _catalog
        try:
            timeout = aiohttp.ClientTimeout(total=4)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(CATALOG_URL) as response:
                    if response.status != 200:
                        raise RuntimeError(f"Discord catalog HTTP {response.status}")
                    data = await response.json()
            if not isinstance(data, list):
                raise ValueError("Unexpected Discord catalog shape")
            _catalog = _build_index(data)
            _catalog_expires = time.monotonic() + _CATALOG_TTL
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError, ValueError) as exc:
            _log.warning("Discord game catalog unavailable: %s", exc)
            # Keep a previously successful catalog if present; retry later.
            _catalog_expires = time.monotonic() + 300
            if _catalog is None:
                _catalog = {}
        return _catalog


async def game_mention(igdb_id: int, name: str, db_path: str) -> str:
    """Return a game tag if confidently resolved, otherwise a safe bold name."""
    fallback = f"**{discord_safe_name(name)}**"
    try:
        async with aiosqlite.connect(db_path) as db:
            async with db.execute(
                "SELECT discord_game_id FROM igdb_games WHERE id = ?", (igdb_id,)
            ) as cursor:
                row = await cursor.fetchone()
        if row and row[0] and re.fullmatch(r"[1-9]\d{15,21}", str(row[0])):
            return f"<@{chr(36)}{row[0]}>"
        discord_id = (await _catalog_index()).get(_key(name))
        if not discord_id:
            return fallback
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "UPDATE igdb_games SET discord_game_id = ? WHERE id = ?",
                (discord_id, igdb_id),
            )
            await db.commit()
        return f"<@{chr(36)}{discord_id}>"
    except (aiosqlite.Error, OSError) as exc:
        _log.warning("Discord game mapping failed: %s", exc)
        return fallback


def discord_safe_name(name):
    # Avoid markdown or user mentions in an external game title.
    import discord
    return discord.utils.escape_mentions(discord.utils.escape_markdown(name))
