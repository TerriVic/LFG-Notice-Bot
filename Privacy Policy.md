# Privacy Policy for LFG Notice Bot

**Last Updated:** September 2026

## 1. Information We Collect
LFG Notice Bot collects minimal data necessary to provide game subscription, ping notification, and server leaderboard services:
* **Discord User IDs:** Stored when you subscribe to a game (`/subscribe`) or set a custom ping template (`/set_ping_template`).
* **Game Subscription Preferences:** Mapped to your Discord User ID in our local database.
* **User Settings:** Custom message formatting templates stored alongside your Discord User ID.
* **Server (Guild), Channel, & Message IDs:** Stored to process server-specific lookup commands (e.g., `/show_subscribers`, `/top_games`) and maintain live-updating leaderboard trackers setup by server administrators.

We do **not** collect, store, or log general message contents, personal identification (names, emails), or IP addresses.

## 2. How We Use Your Data
Stored data is used exclusively to:
* Send mentions and render customized notifications when a user runs `/ping_game`.
* Display active game subscriptions when using `/user_subscriptions`.
* Generate server-specific top game lists and live tracker displays (`/top_games`, `/admin setup_top_games`).
* Enforce command cooldowns and command safety checks.

## 3. Data Storage & Security
* All data is stored in a self-contained SQLite database hosted on our private server.
* Data is never sold, rented, or shared with third-party services.
* External requests to the IGDB/Twitch API are strictly for fetching game catalog metadata and do not contain user information.

## 4. Data Retention & Removal
* **Self-Service Subscription Removal:** Running `/unsubscribe` for a game immediately deletes that subscription entry from the database.
* **Template Reset:** Running `/reset_ping_template` clears your stored custom message template.
* **Full User Data Deletion:** Running `/delete_my_data` allows you to immediately purge all active game subscriptions from the bot's database via an interactive confirmation.

## 5. Contact
If you have questions or privacy concerns regarding LFG Notice Bot, please contact the bot developer or server administrator.

**Email Address:** badhuntilian@gmail.com