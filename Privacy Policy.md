# Privacy Policy for LFG Notice Bot

**Last Updated:** October 9, 2026

## 1. Information We Collect
LFG Notice Bot collects minimal data necessary to provide game subscription, ping notification, quiet mode, server anti-spam controls, and server leaderboard services:
* **Discord User IDs:** Stored when you subscribe to a game (`/subscribe`), configure quiet mode (`/quiet_mode`), or set custom user settings.
* **Game Subscription Preferences:** Mapped to your Discord User ID in our local database.
* **User Settings:** Custom message formatting templates, custom autocomplete indicators, and quiet mode preferences stored alongside your Discord User ID and optional Server (Guild) ID.
* **Server (Guild), Channel, & Message IDs:** Stored to process server-specific lookup commands (e.g., `/show_subscribers`, `/top_games`) and maintain live-updating leaderboard trackers and Quickstart guide messages posted by server moderators.
* **Server LFG Configuration:** Through `/config setup_bot` or individual configuration commands, server moderators may configure allowed LFG channels, a per-game ping cooldown, and optional Discord Role IDs that are allowed to use `/ping_game`.
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
* Stored user preferences and server configuration are kept in a self-contained SQLite database hosted on our private server. Temporary game-search caches and Twitch app access tokens are held in the bot's memory.
* Stored user preferences and server configuration are not sold or rented. The bot does not send Discord User IDs, subscription lists, server/channel IDs, custom templates, or ping notes to IGDB/Twitch.
* Game catalog requests to IGDB include game search text, game names, or game IDs needed for autocomplete, game lookups, and catalog maintenance. Twitch authentication requests include the bot operator's application client ID and client secret; IGDB requests include the application client ID and access token. These application credentials are not credentials belonging to Discord users.
* Requests to IGDB/Twitch are subject to those services' own data handling practices.

## 4. Data Retention & Removal
* **Temporary Search Cache:** Successful raw game-search results and their query text are cached in memory for 60 seconds, with up to 256 query entries. Expired results are not reused. Entries may remain in memory until the next lookup, cache eviction, or shutdown; the cache is cleared on shutdown. This cache does not associate queries with Discord User IDs. Application access tokens are replaced when renewed and are not stored in the SQLite database.
* **Self-Service Subscription Removal:** Running `/unsubscribe` for a game immediately deletes that subscription entry from the database.
* **Template Reset:** Running `/reset_ping_template` clears your stored custom message template.
* **Indicator Reset:** Running `/reset_subscribed_indicator` resets your autocomplete indicator to default (`⭐`).
* **Subscription Data Deletion:** Running `/delete_my_subscriptions` allows you to immediately purge all active game subscriptions from the bot's database via an interactive confirmation.
* **Full User Data Deletion:** Running `/delete_all_my_data` allows you to immediately purge all active game subscriptions and all custom settings (quiet mode preferences, custom templates, indicators) from the bot's database via an interactive confirmation.
* **Setup Drafts:** The `/config setup_bot` wizard temporarily holds the initiating moderator's Discord User ID and selected channels, roles, cooldown and optional guide channel in memory. Draft settings are not written to the database until Save setup is confirmed. Canceling or timing out leaves existing settings unchanged; the wizard expires after five minutes of inactivity. If a guide is posted, its Message, Channel and Server IDs are stored for later updates or removal.
* **Server Anti-Spam Configuration:** Allowed ping-role settings and the per-game cooldown value remain until a server administrator changes them. Setting the ping cooldown to `0` disables it and clears stored per-game cooldown state for that server. Cooldown state is otherwise replaced as new successful pings occur.

* **Server Removal:** When the bot observes removal from a server, it deletes that server's configuration, ping-role restrictions, cooldown state, guide/tracker registrations and server-specific user preferences. Global game subscriptions and global preferences remain until removed by the user. Temporary server outages do not trigger this cleanup.
* **Operational Statistics:** Aggregate process counters cover completed LFG pings, IGDB requests/final failures and unexpected command errors. They are held in memory, reset at restart, and do not retain User IDs, Server IDs, game names or message contents. The bot owner can view these counters along with current server count and uptime.
* **Diagnostic Logs:** Unexpected command failures are logged with command names and exception tracebacks for troubleshooting. Exception text may incidentally include IDs or other context. Diagnostic logs are separate from user preferences and are not erased by `/delete_all_my_data`; their retention is controlled by the hosting operator.
* **Privacy Information:** `/privacy` provides a summary, data-deletion options and contact details, plus a link to the full policy when configured by the operator.

## 5. Contact
If you have questions or privacy concerns regarding LFG Notice Bot, please contact the bot developer or server administrator.

**Email Address:** badhuntilian@gmail.com
