import asyncio
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from igdb_client import IGDBClient, IGDBError, Response

class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None):
        self.status, self.payload, self.headers = status, payload, headers or {}
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def json(self): return self.payload

class FakeSession:
    closed = False
    def __init__(self, games):
        self.games = list(games)
        self.calls = []
        self.tokens = 0
    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if 'oauth2/token' in url:
            self.tokens += 1
            return FakeResponse(payload={'access_token': f'token{self.tokens}', 'expires_in': 3600})
        return self.games.pop(0)
    async def close(self): self.closed = True

class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = patch.dict(os.environ, TWITCH_CLIENT_ID='id', TWITCH_SECRET='secret')
        self.env.start()
        self.client = IGDBClient()
        async def fast(): pass
        self.client._wait_turn = fast
    async def asyncTearDown(self):
        await self.client.close()
        self.env.stop()
    def session(self, *responses):
        self.client._session = FakeSession(responses)
        return self.client._session
    async def test_token_reuse_and_expiry(self):
        session = self.session(FakeResponse(payload=[]), FakeResponse(payload=[]), FakeResponse(payload=[]))
        await self.client._request('one'); await self.client._request('two')
        self.assertEqual(session.tokens, 1)
        self.client._expires = 0
        await self.client._request('three')
        self.assertEqual(session.tokens, 2)
        self.assertEqual(session.calls[0][1]['data']['grant_type'], 'client_credentials')
        self.assertNotIn('secret', session.calls[0][0])
    async def test_concurrent_auth_refresh_is_single_flight(self):
        session = self.session()
        tokens = await asyncio.gather(*(self.client._get_token() for _ in range(10)))
        self.assertEqual(session.tokens, 1)
        self.assertEqual(len(set(tokens)), 1)
        await asyncio.gather(*(self.client._get_token(rejected=tokens[0]) for _ in range(10)))
        self.assertEqual(session.tokens, 2)
    async def test_401_renews_and_retries_same_query(self):
        session = self.session(FakeResponse(401), FakeResponse(payload=[{'id': 1}]))
        result = await self.client._request('query')
        self.assertEqual(await result.json(), [{'id': 1}])
        self.assertEqual(session.tokens, 2)
        self.assertEqual([c[1]['data'] for c in session.calls if '/v4/' in c[0]], ['query', 'query'])
    async def test_second_401_is_not_infinite(self):
        session = self.session(FakeResponse(401), FakeResponse(401))
        self.assertEqual((await self.client._request('query')).status, 401)
        self.assertEqual(session.tokens, 2)
    async def test_429_retries_same_page_and_sets_cooldown(self):
        session = self.session(FakeResponse(429, headers={'Retry-After':'0'}), FakeResponse(payload=[{'id': 9}]))
        result = await self.client._request('page')
        self.assertEqual(await result.json(), [{'id':9}])
        self.assertEqual([c[1]['data'] for c in session.calls if '/v4/' in c[0]], ['page', 'page'])
        self.assertGreater(self.client._next_request, 0)
    async def test_server_retries_are_bounded(self):
        self.session(*(FakeResponse(503, headers={'Retry-After':'0'}) for _ in range(4)))
        with self.assertRaises(IGDBError): await self.client._request('page')
    async def test_cache_deduplicates_and_copies_results(self):
        session = self.session(FakeResponse(payload=[{'id': 1}]))
        results = await asyncio.gather(*(self.client._cached_request('search "game";') for _ in range(10)))
        await asyncio.sleep(0)
        data = await results[0].json(); data[0]['id'] = 999
        self.assertEqual(await results[1].json(), [{'id':1}])
        await self.client._cached_request('search "game";')
        self.assertEqual(len(session.calls), 2)
    async def test_empty_results_cached_and_expired_results_refetched(self):
        session = self.session(FakeResponse(payload=[]), FakeResponse(payload=[{'id':2}]))
        await self.client._cached_request('search "none";'); await asyncio.sleep(0)
        await self.client._cached_request('search "none";')
        self.assertEqual(len(session.calls), 2)
        self.client._cache['search "none";'] = (0, Response(200, []))
        result = await self.client._cached_request('search "none";')
        self.assertEqual(await result.json(), [{'id':2}])
    async def test_failed_requests_not_cached(self):
        self.session(FakeResponse(400), FakeResponse(payload=[]))
        self.assertEqual((await self.client._cached_request('search "x";')).status, 400)
        await asyncio.sleep(0)
        self.assertEqual((await self.client._cached_request('search "x";')).status, 200)
    async def test_timeout_does_not_cancel_other_search_waiter(self):
        async def slow(body):
            await asyncio.sleep(.03)
            return Response(200, [{'id':1}])
        self.client._request = slow
        async def short():
            async with self.client.post(data='search "x";', timeout=.005): pass
        task = asyncio.create_task(short())
        result = await self.client._cached_request('search "x";')
        with self.assertRaises(asyncio.TimeoutError): await task
        self.assertEqual(await result.json(), [{'id':1}])
    async def test_shared_pacing(self):
        client = IGDBClient()
        starts = []
        async def caller():
            await client._wait_turn(); starts.append(time.monotonic())
        await asyncio.gather(*(caller() for _ in range(5)))
        self.assertTrue(all(b-a >= .26 for a,b in zip(starts,starts[1:])))
        self.assertGreaterEqual(starts[4]-starts[0], 1.04)
    async def test_cooldown_extension_delays_already_waiting_caller(self):
        client = IGDBClient()
        begin = time.monotonic()
        client._next_request = begin + .03
        waiting = asyncio.create_task(client._wait_turn())
        await asyncio.sleep(.01)
        client._next_request = begin + .08
        await waiting
        self.assertGreaterEqual(time.monotonic() - begin, .075)

    async def test_close_cancels_background_search(self):
        async def slow(body):
            await asyncio.sleep(10)
        self.client._request = slow
        task = asyncio.create_task(self.client._cached_request('search "x";'))
        await asyncio.sleep(0)
        await self.client.close()
        with self.assertRaises(asyncio.CancelledError): await task
    def test_retry_after_formats(self):
        self.assertEqual(self.client._retry_after('3', 0), 3)
        self.assertEqual(self.client._retry_after(None, 2), 4)
        self.assertEqual(self.client._retry_after('Thu, 01 Jan 1970 00:00:00 GMT', 0), 0)

if __name__ == '__main__': unittest.main()
