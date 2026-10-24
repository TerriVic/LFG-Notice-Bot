import pytest
import aiosqlite
from unittest.mock import AsyncMock, patch

import discord_game_mentions as gm
from database import init_db


@pytest.fixture
async def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / 'games.sqlite')
    monkeypatch.setattr('database.DB_PATH', path)
    await init_db()
    async with aiosqlite.connect(path) as db:
        await db.execute('INSERT INTO igdb_games (id,name,updated_at) VALUES (1,?,1)', ('Deadlock',))
        await db.commit()
    return path


def test_index_rejects_ambiguous_names():
    index = gm._build_index([
        {'name': 'Deadlock', 'id': '1276737795012165766'},
        {'name': ' Same Game ', 'id': '123456789012345678'},
        {'name': 'same game', 'id': '223456789012345678'},
        {'name': 'Fake', 'id': 'not-an-id'},
    ])
    assert index == {'deadlock': '1276737795012165766'}


@pytest.mark.asyncio
async def test_mapping_is_persisted_and_reused(db_path):
    with patch.object(gm, '_catalog_index', new=AsyncMock(return_value={'deadlock': '1276737795012165766'})) as fetch:
        assert await gm.game_mention(1, 'Deadlock', db_path) == '<@$1276737795012165766>'
        assert await gm.game_mention(1, 'Deadlock', db_path) == '<@$1276737795012165766>'
        fetch.assert_awaited_once()
    async with aiosqlite.connect(db_path) as db:
        async with db.execute('SELECT discord_game_id FROM igdb_games WHERE id=1') as cur:
            assert (await cur.fetchone())[0] == '1276737795012165766'


@pytest.mark.asyncio
async def test_unknown_game_falls_back(db_path):
    with patch.object(gm, '_catalog_index', new=AsyncMock(return_value={})):
        assert await gm.game_mention(1, 'Deadlock', db_path) == '**Deadlock**'


@pytest.mark.asyncio
async def test_catalog_failure_falls_back(db_path):
    gm._catalog, gm._catalog_expires = None, 0
    with patch.object(gm, '_catalog_index', new=AsyncMock(return_value={})):
        assert await gm.game_mention(1, 'Deadlock', db_path) == '**Deadlock**'
