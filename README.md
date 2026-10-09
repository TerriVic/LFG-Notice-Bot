# LFG Notice Bot

> _"Hi! I allow you to subscribe to a list of games, and when one of those games is mentioned, you will also be automatically tagged so you can team up and start playing with people."_

A feature-rich Discord slash-command bot built with Python and `discord.py` that allows server members to subscribe to their favorite games and receive targeted pings when others are looking for a group (LFG). Powered by the IGDB database for comprehensive game search and metadata caching.

---

## Key Features

* **Targeted LFG Notifications:** Notify only players interested in a specific game without relying on mass `@everyone` or `@here` mentions.
* **IGDB Database Integration:** Autocomplete and search millions of games directly from IGDB, automatically resolving special/deluxe editions to base games.
* **Live Top Games Leaderboard:** Post dynamic, self-updating embed trackers displaying the server's most popular games.
* **Custom Ping Templates:** Customize individual ping formatting and templates to suit your style.
* **Guided Server Setup:** `/config setup_bot` walks moderators through channels, ping roles, cooldown and an optional Quickstart guide, with a review before saving.
* **Server Anti-Spam Controls:** Per-game ping cooldowns and optional allowed-ping roles help servers prevent repeated LFG notification spam.
* **Privacy & Data Ownership:** Native `/delete_all_my_data`, `/delete_my_subscriptions`, and `/unsubscribe` commands give users total control over their stored preferences.
* **Docker Ready:** Multi-stage Docker deployment setup with SQLite database persistence.

---

## 🛠Command Reference

### User Commands
| Command                                 | Description                                                                                       |
|-----------------------------------------|---------------------------------------------------------------------------------------------------|
| `/subscribe <game>`                     | Subscribe to notifications for a game (with autocomplete & IGDB lookup).                          |
| `/unsubscribe <game>`                   | Unsubscribe from active notifications for a game.                                                 |
| `/user_subscriptions [user] [public]`   | View active game subscriptions for yourself or another member.                                    |
| `/game_info <game> [public]`            | Get details, genres, and cover image for a game.                                                  |
| `/ping_game <game> [note]`              | Send an LFG ping mentioning all subscribers of a game, with an optional note.                       |
| `/quiet_mode [enabled] [scope]`         | Mute/unmute LFG notifications globally or specifically for the current server.                    |
| `/show_subscribers <game>`              | View subscribers for a game without triggering a ping.                                            |
| `/top_games [limit] [public]`           | View the most subscribed games on the server.                                                     |
| `/set_ping_template <template>`         | Set a custom template for your `/ping_game` notifications.                                        |
| `/view_ping_template`                   | View your current custom ping template setting.                                                   |
| `/reset_ping_template`                  | Reset your custom ping template to default.                                                       |
| `/set_subscribed_indicator <indicator>` | Set a custom icon/indicator for your subscribed games in autocomplete. (no custom emote support). |
| `/view_subscribed_indicator`            | View your saved autocomplete subscribed game indicator.                                           |
| `/reset_subscribed_indicator`           | Reset your subscribed game autocomplete indicator back to default (`⭐`).                          |
| `/delete_my_subscriptions`              | Purge all your active game subscriptions.                                                         |
| `/delete_all_my_data`                   | Purge all your active game subscriptions and user settings/ping templates.                        |
| `/quickstart`                           | View the quickstart guide.                                                                        |
| `/help`                                 | View the command guide.                                                                           |

### Bot Config Commands (`/config`)
| Command                                               | Description                                                                                                                                                        |
|-------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `/config setup_bot` | Start the private server setup wizard (requires Manage Messages permission or bot-owner access). |
| `/config setup_top_games [limit] [interval]`          | Post a live-updating top games leaderboard message in the current channel. `interval` accepts decimal hours (e.g., `0.25` for 15m, `0.5` for 30m, default: `1.0`). |
| `/config edit_top_games <message> [limit] [interval]` | Update display limit or interval settings (e.g., `0.5` for 30m), or leave options empty to force an immediate refresh.                                             |
| `/config force_update_top_games <message>`            | Force an immediate refresh on an active live tracker message.                                                                                                      |
| `/config remove_top_games <message>`                  | Stop tracking and delete an active live top games message.                                                                                                         |
| `/config set_lfg_channels <mode> [channel]`           | Configure LFG mode (`all` or `whitelist`) and add channels marked with `✅`.                                                                                        |
| `/config remove_lfg_channels <mode> [channel]`        | Set mode to disable LFG server-wide (`none`) or remove specific whitelisted channels.                                                                              |
| `/config view_lfg_channels`                           | View current LFG channel configuration embed.                                                                                                                      |
| `/config ping_cooldown [minutes]`                     | View or set the per-game server ping cooldown. Default: `0.5` minutes (30 seconds). Decimal values are supported; `0` disables it.                                 |
| `/config ping_roles <action> [role]`                  | Restrict `/ping_game` to configured server roles, remove/view restrictions, or allow everyone. Default: everyone may ping.                                         |
| `/config server_overview`                             | View subscription details for members in the server where the command is called.                                                                                   |
| `/config setup_quickstart`                            | Post a Quickstart Guide embed.                                                                                                                                     |
| `/config remove_quickstart <message>`                 | Remove a Quickstart Guide message.                                                                                                                                 |
| `/config update_quickstart [message]`                 | Reload the contents of all quickstart messages, or a specific message (in case of an update)                                                                       |


### First-Run Server Setup

Run `/config setup_bot` inside the server with **Manage Messages** permission (or as the bot owner):

1. Select up to 25 text channels where LFG pings are allowed.
2. Choose allowed ping roles, or **Everyone**.
3. Choose a per-game cooldown; **10 minutes** is the recommended wizard option. Use `/config ping_cooldown` afterward for custom values.
4. Choose a text channel for a new Quickstart guide, or skip it.
5. Review the settings and click **Save setup**.

Only the moderator who opened the wizard can use it while retaining permission. Canceling or timing out after five minutes of inactivity leaves settings unchanged. Saving replaces the allowed-channel whitelist, allowed ping roles and cooldown; existing Quickstart messages and other settings are retained. The bot checks channel permissions before saving. A guide channel needs View Channel, Send Messages and Embed Links permissions.

Unconfigured servers block `/ping_game` until a moderator enables channels through the wizard or `/config set_lfg_channels`. Existing saved configurations, including explicit allow-all settings, are retained. The individual configuration commands remain available for later adjustments.

### LFG Ping Defaults

* **Channel access:** Pings are disabled in unconfigured servers. Setup enables only the selected channels; `/config set_lfg_channels` can explicitly allow all channels.

* **Per-game cooldown:** By default, the same game can be pinged once every **30 seconds** per server. Administrators can change this with `/config ping_cooldown`; setting it to `0` disables the per-game cooldown.
* **Who may ping:** By default, **everyone** may use `/ping_game` in allowed LFG channels. Administrators can restrict the command to one or more roles with `/config ping_roles`.
* These server/game protections are separate from any per-user command cooldowns and from LFG channel restrictions.

---

### Bot Owner Commands

#### Slash Commands (`/owner`)
| Command                                   | Description                                                                                                       |
|-------------------------------------------|-------------------------------------------------------------------------------------------------------------------|
| `/owner overview`                         | View a global subscription overview across all servers.                                                           |
| `/owner subscribe_user <user> <game>`     | Force-subscribe a target user to a specific game (supports autocomplete).                                         |
| `/owner unsubscribe_user <user> <game>`   | Force-unsubscribe a target user from a game (autocompletes only games that the user is subscribed to).            |
| `/owner delete_user_subscriptions <user>` | Permanently purge all stored subscription data for a specific user (triggers an interactive confirmation prompt). |
| `/owner delete_all_user_data <user>`      | Permanently purge all stored data for a specific user (triggers an interactive confirmation prompt).              |

#### Prefix Commands
| Command                                             | Description                                                                                                                                        |
|-----------------------------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------|
| `!sync [guild_id]`                                  | Manually sync slash command trees with Discord API. Omit `guild_id` for global sync or pass a guild ID for instant test server syncing.            |
| `!getdb`                                            | Sends a copy of the active SQLite database (db.sqlite) directly to the bot owner via DM (or channel fallback) for local debugging.                 |
| `!reload <cog_name>`                                | Hot-reload a specific cog/extension without restarting the bot (e.g., !reload owner_cmds or !reload cogs.owner_cmds). Restricted to the bot owner. |
| `!delmsg <message_id> [channel_id]` / `!delete_msg` | Delete a specific message sent by the bot (in the current channel or in a specified target channel by ID). Restricted to the bot owner.            |
---
## Configuration & Environment Variables

Copy `.env.example` to `.env` and fill in your API credentials:

```bash
cp .env.example .env
```

| Variable           | Description                                                                                                           |
|--------------------|-----------------------------------------------------------------------------------------------------------------------|
| `DISCORD_TOKEN`    | Discord Bot Token from the [Discord Developer Portal](https://discord.com/developers/applications?utm_source=gemini). |
| `TWITCH_CLIENT_ID` | Client ID from the [Twitch Developer Console](https://dev.twitch.tv/console?utm_source=gemini) for IGDB API access.   |
| `TWITCH_SECRET`    | Client secret for the same Twitch application as `TWITCH_CLIENT_ID`. Used to obtain app access tokens automatically.  |
| `BOT_OWNER_ID`     | *(Optional)* Discord User ID of the bot owner for elevated administrative access.                                     |
| `DATA_DIR`         | Directory path where SQLite database file will be stored (defaults to the current directory; Docker may set `/data`). |

### IGDB Authentication & Request Handling

Keep `TWITCH_SECRET` private and configure it alongside `TWITCH_CLIENT_ID`. The bot obtains Twitch app access tokens automatically, renews them before their reported expiry, and replaces them after an authentication failure (HTTP 401). Tokens are held in memory. `TWITCH_ACCESS_TOKEN` is no longer read; existing deployments should replace it with `TWITCH_CLIENT_SECRET` and restart the bot. Update `.env.example` and any deployment environment configuration to use the new variable name too.

All IGDB calls share one HTTP session, with requests spaced at least 0.27 seconds apart and at most eight concurrent requests. HTTP 429 responses delay subsequent requests according to `Retry-After`; temporary network and server failures use bounded retries. These limits cover one running Python process. Multiple bot instances using the same Twitch application need coordinated limits or separate Twitch applications.

Successful search results, including empty results, are cached in memory for 60 seconds (up to 256 queries). Simultaneous identical searches share one request; at most 64 distinct searches may be pending. Autocomplete gives the live search two seconds before falling back to local results. The shared request may finish in the background and populate the cache for the next lookup.

---

## Getting Started

### Method 1: Running with Docker (Recommended)

1. **Clone the Repository:**
```bash
git clone https://github.com/Vic/LFG-Notice-Bot.git
cd LFG-Notice-Bot
```

2. **Configure Environment:**
   Set up your `.env` file as described in the configuration section.

3. **Launch Container with Docker Compose:**
```bash
docker compose up -d
```

Note: Persistent storage for the SQLite database is handled automatically via container volume mapping to `/data`.

---

### Method 2: Manual Local Setup

1. **Create and Activate Virtual Environment:**
```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

2. **Install Dependencies:**
```bash
pip install -r requirements.txt
```

3. **Run the Bot:**
```bash
python bot.py
```

---

## Running Tests

Install `pytest` and `pytest-asyncio` if they are not already installed, then run from the project root:

```bash
python -m pytest Tests --asyncio-mode=auto -q
```

The IGDB client tests mock HTTP responses and do not require real Twitch credentials. Tests for game lookups, database repair, and catalog sync mock the shared `igdb.post` transport.

## License & Privacy

This project stores minimal user and server configuration data (including Discord User IDs, game preferences, tracker/channel IDs, configured role IDs, and per-game cooldown state) strictly required for bot functionality. Read the full details in [`Privacy Policy.md`](./Privacy%20Policy.md).

## Credits & Acknowledgements

### Human Contributors
* **[Vic](https://git.sashie.me/Vic/)** - Lead Developer & Maintainer
* **[Sasha](https://git.sashie.me/sasha)** - Support, Friendship, Infrastructure, Server Hosting & CI/CD Deployment

### AI Assistance & Attribution
This project was developed with the assistance of Generative AI tools:
* **[Google Gemini](https://gemini.google.com/)**: Used for architecture design, feature drafting, regex filtering, and documentation templates.
* **[GitHub Copilot](https://github.com/features/copilot)**: Used for Discord application setup

> **Disclaimer:** AI tools were used as assistive pair-programmers. All generated code has been manually reviewed, edited, tested, and validated by human maintainers. The maintainers take full responsibility for the security, quality, and functionality of this codebase.