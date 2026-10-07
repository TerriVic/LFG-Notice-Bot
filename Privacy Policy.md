# Privacy Policy for LFG Notice Bot

**Last Updated:** October 2026

## 1. Information We Collect
LFG Notice Bot collects minimal data necessary to provide game subscription, ping notification, quiet mode, server anti-spam controls, and server leaderboard services:
* **Discord User IDs:** Stored when you subscribe to a game (`/subscribe`), configure quiet mode (`/quiet_mode`), or set custom user settings.
* **Game Subscription Preferences:** Mapped to your Discord User ID in our local database.
* **User Settings:** Custom message formatting templates, custom autocomplete indicators, and quiet mode preferences stored alongside your Discord User ID and optional Server (Guild) ID.
* **Server (Guild), Channel, & Message IDs:** Stored to process server-specific lookup commands (e.g., `/show_subscribers`, `/top_games`) and maintain live-updating leaderboard trackers setup by server administrators.
* **Server LFG Configuration:** Server administrators may configure allowed LFG channels, a per-game ping cooldown, and optional Discord Role IDs that are allowed to use `/ping_game`.
* **Per-Game Cooldown State:** To enforce the server/game cooldown, the bot stores the Server (Guild) ID, game ID, and timestamp of the most recent successful ping for that game in that server. This cooldown record is not tied to the caller's Discord User ID and does not store the ping message or note.

We do **not** collect, store, or log general message contents, personal identification (names, emails), or IP addresses.

## 2. How We Use Your Data
Stored data is used exclusively to:
* Send mentions and render customized notifications when a user runs `/ping_game`.
* Mute pings for users who have enabled Quiet Mode (`/quiet_mode`).
* Display active game subscriptions when using `/user_subscriptions`.
* Generate server-specific top game lists and live tracker displays (`/top_games`, `/config setup_top_games`).
* Enforce per-user and per-game command cooldowns, configured ping-role restrictions, and other command safety checks.

## 3. Data Storage & Security
* All data is stored in a self-contained SQLite database hosted on our private server.
* Data is never sold, rented, or shared with third-party services.
* External requests to the IGDB/Twitch API are strictly for fetching game catalog metadata and do not contain user information.

## 4. Data Retention & Removal
* **Self-Service Subscription Removal:** Running `/unsubscribe` for a game immediately deletes that subscription entry from the database.
* **Template Reset:** Running `/reset_ping_template` clears your stored custom message template.
* **Indicator Reset:** Running `/reset_subscribed_indicator` resets your autocomplete indicator to default (`⭐`).
* **Subscription Data Deletion:** Running `/delete_my_subscriptions` allows you to immediately purge all active game subscriptions from the bot's database via an interactive confirmation.
* **Full User Data Deletion:** Running `/delete_all_my_data` allows you to immediately purge all active game subscriptions and all custom settings (quiet mode preferences, custom templates, indicators) from the bot's database via an interactive confirmation.
* **Server Anti-Spam Configuration:** Allowed ping-role settings and the per-game cooldown value remain until a server administrator changes them. Setting the ping cooldown to `0` disables it and clears stored per-game cooldown state for that server. Cooldown state is otherwise replaced as new successful pings occur.

## 5. Contact
If you have questions or privacy concerns regarding LFG Notice Bot, please contact the bot developer or server administrator.

**Email Address:** badhuntilian@gmail.com
