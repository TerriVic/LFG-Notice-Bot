"""One process-wide IGDB transport. Requires Python 3.10+ and aiohttp."""
import asyncio
import copy
import os
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import timezone
from email.utils import parsedate_to_datetime

import aiohttp
from operations import stats


class IGDBError(RuntimeError):
    pass


class Response:
    """Buffered response: sockets are released before callers process results."""
    def __init__(self, status, payload):
        self.status = status
        self.payload = payload

    async def json(self):
        return copy.deepcopy(self.payload)

    async def text(self):
        # Do not include remote response bodies in logs (may contain credentials).
        return f"IGDB HTTP {self.status}"


class IGDBClient:
    def __init__(self):
        self._session = None
        self._token = None
        self._expires = 0
        self._auth_lock = asyncio.Lock()
        self._rate_lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(8)
        self._next_request = 0
        self._cache = OrderedDict()
        self._pending = {}

    @property
    def configured(self):
        return bool(os.getenv("TWITCH_CLIENT_ID") and os.getenv("TWITCH_SECRET"))

    async def _get_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        return self._session

    async def _get_token(self, rejected=None):
        async with self._auth_lock:
            if rejected is not None and self._token == rejected:
                self._token = None
            if self._token and time.monotonic() < self._expires:
                return self._token
            if not self.configured:
                raise IGDBError("Set TWITCH_CLIENT_ID and TWITCH_SECRET")
            session = await self._get_session()
            for attempt in range(3):
                try:
                    async with session.post("https://id.twitch.tv/oauth2/token", data={
                        "client_id": os.environ["TWITCH_CLIENT_ID"],
                        "client_secret": os.environ["TWITCH_SECRET"],
                        "grant_type": "client_credentials",
                    }) as response:
                        if response.status == 200:
                            data = await response.json()
                            lifetime = float(data["expires_in"])
                            if not data.get("access_token") or lifetime <= 0:
                                raise IGDBError("Invalid Twitch token response")
                            self._token = data["access_token"]
                            self._expires = time.monotonic() + lifetime - min(60, lifetime / 10)
                            return self._token
                        if response.status not in (429, 500, 502, 503, 504):
                            raise IGDBError(f"Twitch authentication failed (HTTP {response.status})")
                        delay = self._retry_after(response.headers.get("Retry-After"), attempt)
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    delay = 2 ** attempt
                if attempt < 2:
                    await asyncio.sleep(delay)
            raise IGDBError("Twitch authentication temporarily unavailable")

    @staticmethod
    def _retry_after(value, attempt):
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            try:
                date = parsedate_to_datetime(value)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                return max(0.0, date.timestamp() - time.time())
            except (TypeError, ValueError, OverflowError):
                return float(2 ** attempt)

    async def _wait_turn(self):
        async with self._rate_lock:
            while self._next_request > time.monotonic():
                await asyncio.sleep(self._next_request - time.monotonic())
            # A small margin keeps four starts from straddling a one-second window.
            self._next_request = time.monotonic() + 0.27

    async def _request(self, body):
        # Count final failures once per underlying request, after bounded retries.
        # Shared cached searches count once; cache hits do not count as requests.
        stats.increment("igdb_requests")
        try:
            result = await self._request_impl(body)
        except Exception:
            stats.increment("igdb_failures")
            raise
        if result.status != 200:
            stats.increment("igdb_failures")
        return result

    async def _request_impl(self, body):
        refreshed = False
        for attempt in range(4):
            token = await self._get_token()
            session = await self._get_session()
            try:
                async with self._slots:
                    await self._wait_turn()
                    async with session.post("https://api.igdb.com/v4/games", data=body, headers={
                        "Client-ID": os.environ["TWITCH_CLIENT_ID"],
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json",
                    }) as response:
                        status = response.status
                        if status == 200:
                            payload = await response.json()
                            if not isinstance(payload, list):
                                raise IGDBError("Unexpected IGDB response")
                            return Response(status, payload)
                        if status == 401 and not refreshed:
                            refreshed = True
                            await self._get_token(rejected=token)
                            continue
                        if status not in (429, 500, 502, 503, 504):
                            return Response(status, None)
                        delay = self._retry_after(response.headers.get("Retry-After"), attempt)
                        if status == 429:
                            # No await: apply the cooldown immediately, ahead of queued callers.
                            self._next_request = max(self._next_request, time.monotonic() + delay)
            except (aiohttp.ClientError, asyncio.TimeoutError):
                delay = float(2 ** attempt)
            if attempt < 3:
                await asyncio.sleep(delay)
        raise IGDBError("IGDB unavailable after bounded retries")

    async def _cached_request(self, body):
        entry = self._cache.get(body)
        if entry and entry[0] > time.monotonic():
            self._cache.move_to_end(body)
            return entry[1]
        self._cache.pop(body, None)
        if body in self._pending:
            return await asyncio.shield(self._pending[body])
        if len(self._pending) >= 64:
            raise IGDBError("IGDB search queue is busy")
        task = asyncio.create_task(self._request(body))
        self._pending[body] = task

        def finished(done):
            self._pending.pop(body, None)
            if done.cancelled():
                return
            error = done.exception()  # Retrieve even when all autocomplete callers timed out.
            if error is None and done.result().status == 200:
                self._cache[body] = (time.monotonic() + 60, done.result())
                self._cache.move_to_end(body)
                while len(self._cache) > 256:
                    self._cache.popitem(last=False)

        task.add_done_callback(finished)
        return await asyncio.shield(task)

    @asynccontextmanager
    async def post(self, url=None, *, data, headers=None, timeout=None):
        # Compatibility arguments let existing result processing stay unchanged.
        request = self._cached_request(data) if 'search "' in data else self._request(data)
        if timeout is None:
            result = await request
        else:
            result = await asyncio.wait_for(request, timeout)
        yield result

    async def close(self):
        pending = list(self._pending.values())
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self._pending.clear()
        self._cache.clear()
        if self._session is not None:
            await self._session.close()


igdb = IGDBClient()
