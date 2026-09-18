# Privacy Policy for LFG Notice Bot

**Last Updated:** September 2026

## 1. Information We Collect
LFG Notice Bot collects minimal data necessary to provide game subscription and ping notifications:
* **Discord User IDs:** Stored when you subscribe to a game using `/subscribe`.
* **Game Subscription Preferences:** Mapped to your Discord User ID in our local database.
* **Server (Guild) IDs:** Processed temporarily to check membership for server-specific commands like `/showsubscribers`.

We do **not** collect, store, or log message contents, personal identification (names, emails), or IP addresses.

## 2. How We Use Your Data
Stored data is used exclusively to:
* Send mentions/notifications when a user runs `/pinggame`.
* Display your active game subscriptions when you run `/usersubscriptions`.
* Ensure command cooldowns work as expected.

## 3. Data Storage & Security
* All data is stored in a self-contained SQLite database hosted on our private server.
* Data is never sold, rented, or shared with third-party services.
* External requests to the IGDB/Twitch API are strictly for fetching game metadata and do not contain user information.

## 4. Data Retention & Removal
* **Self-Service Removal:** Running `/unsubscribe` for a game immediately deletes that subscription entry from the database.
* **Full Data Deletion:** If you wish to have all your data deleted from the bot's database, contact the bot administrator or open an issue on the repository.

## 5. Contact
If you have questions or privacy concerns regarding LFG Notice Bot, please contact the bot developer or server administrator.