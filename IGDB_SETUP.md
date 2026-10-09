# IGDB client update

Copy these files into your existing bot project:

- `igdb_client.py` (new, beside `bot.py`)
- `bot.py`, `database.py`, `utils.py` (replace existing files)
- `cogs/background_tasks.py` (replace existing cog)
- `Tests/test_igdb_client.py` (optional regression tests)

Your other cogs are unchanged and are not included in this patch.

## Environment

Keep `TWITCH_CLIENT_ID` and add `TWITCH_SECRET` to the bot hosting environment or `.env` file. Get the secret for the same application in the [Twitch developer console](https://dev.twitch.tv/console/apps). Keep the secret private. `TWITCH_ACCESS_TOKEN` is no longer read and can be removed. Restart the bot after changing credentials.

Tokens are requested automatically, held in memory, renewed before their reported expiry, and replaced after a 401. No new dependency is required: the bot already uses aiohttp. Python 3.10+ is required.

## Behavior

All IGDB game calls share one session, a 0.27-second request spacing (below four starts per second), and an eight-request concurrency limit. These limits apply within one running Python process. If production and a test bot share a Twitch application, their combined traffic still shares the upstream limit; use separate applications or a cross-process limiter when running multiple instances.

429 responses respect Retry-After (seconds or HTTP date) and delay all subsequent IGDB requests. Transient network failures and selected 5xx responses retry with exponential backoff. Requests have a 15-second HTTP timeout and at most four attempts. Authentication has at most three attempts. Exhausted retries surface a sanitized error; permanent HTTP errors are left to existing caller handling.

Identical search queries share an in-flight request and cache successful raw results, including empty lists, for 60 seconds. Cache capacity is 256 queries; outstanding distinct searches are limited to 64. User subscription and server sorting logic still runs separately per autocomplete interaction. Autocomplete allows two seconds for the live lookup, then uses its local candidates. A shared search may finish in the background and populate the cache for the next interaction.

The weekly sync catches API failures so its scheduled loop can run again next week. Backfill retries the same page; it skips its cleanup if a batch fails. Shutdown cancels backfill and closes the shared HTTP client.

## Verification

Run `python -m unittest discover -s Tests -p test_igdb_client.py -v` from the project directory. The 14 client tests use mocked HTTP responses and real asynchronous pacing; they require no credentials or network.

Before deploying broadly, start the bot with the real client ID and secret and check autocomplete, game info, and catalog sync. Live authentication was not exercised during this patch because no production credentials were supplied.

References: [IGDB rate limits](https://api-docs.igdb.com/#rate-limits), [Twitch client credentials](https://dev.twitch.tv/docs/authentication/getting-tokens-oauth/#client-credentials-grant-flow).

## Develop patch

This patch is based on the develop files uploaded in this conversation. It also updates Tests/test_utils.py, Tests/test_database.py and Tests/test_background_tasks.py. The admin, owner and user cogs and their tests retain their develop behavior. Run `python -m pytest Tests --asyncio-mode=auto -q`. All 170 supplied and added tests passed under Python 3.12; existing admin mock warnings remain.
