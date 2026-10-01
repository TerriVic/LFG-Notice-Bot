# LFG Notice Bot

> _"Hi! I allow you to subscribe to a list of games, and when one of those games is mentioned, you will also be automatically tagged so you can team up and start playing with people."_

A feature-rich Discord slash-command bot built with Python and `discord.py` that allows server members to subscribe to their favorite games and receive targeted pings when others are looking for a group (LFG). Powered by the IGDB database for comprehensive game search and metadata caching.

---

## Key Features

* **Targeted LFG Notifications:** Notify only players interested in a specific game without relying on mass `@everyone` or `@here` mentions.
* **IGDB Database Integration:** Autocomplete and search millions of games directly from IGDB, automatically resolving special/deluxe editions to base games.
* **Live Top Games Leaderboard:** Post dynamic, self-updating embed trackers displaying the server's most popular games.
* **Custom Ping Templates:** Customize individual ping formatting and templates to suit your style.
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
| `/game_info <game>`                     | Get details, genres, and cover image for a game.                                                  |
| `/ping_game <game> [public]`            | Send an LFG ping mentioning all subscribers of a game.                                            |
| `/show_subscribers <game>`              | View subscribers for a game without triggering a ping.                                            |
| `/top_games [limit] [public]`           | View the most subscribed games on the server.                                                     |
| `/set_ping_template`                    | Set a custom template for your `/ping_game` notifications.                                        |
| `/view_ping_template`                   | View your current custom ping template setting.                                                   |
| `/reset_ping_template`                  | Reset your custom ping template to default.                                                       |
| `/set_subscribed_indicator <indicator>` | Set a custom icon/indicator for your subscribed games in autocomplete  (no custom emote support). |
| `/view_subscribed_indicator`            | View your saved autocomplete subscribed game indicator.                                           |
| `/reset_subscribed_indicator`           | Reset your subscribed game autocomplete indicator back to default (`⭐`).                          |
| `/delete_my_subscriptions`              | Purge all your active game subscriptions                                                          |
| `/delete_all_my_data`                   | Purge all your active game subscriptions and user settings/ping templates.                        |
| `/help [public]`                        | View an interactive command guide.                                                                |

### Bot Config Commands (`/config`)
| Command                                               | Description                                                                                                                                                        |
|-------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `/config setup_top_games [limit] [interval]`          | Post a live-updating top games leaderboard message in the current channel. `interval` accepts decimal hours (e.g., `0.25` for 15m, `0.5` for 30m, default: `1.0`). |
| `/config edit_top_games <message> [limit] [interval]` | Update display limit or interval settings (e.g., `0.5` for 30m), or leave options empty to force an immediate refresh.                                             |
| `/config force_update_top_games <message>`            | Force an immediate refresh on an active live tracker message.                                                                                                      |
| `/config remove_top_games <message>`                  | Stop tracking and delete an active live top games message.                                                                                                         |
| `/config set_lfg_channels <mode> [channels]`          | Restrict LFG pings (`/ping_game`) to specific whitelisted channels, allow them server-wide, or disable them server-wide.                                           |
| `/config server_overview`                             | View subscription details for members in the server where the command is called.                                                                                   |                                                                                  |

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
| Command                                                  | Description                                                                                                                                        |
|----------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------|
| `!sync [guild_id]`                                       | Manually sync slash command trees with Discord API. Omit `guild_id` for global sync or pass a guild ID for instant test server syncing.            |
| `!reload <cog_name>`                                     | Hot-reload a specific cog/extension without restarting the bot (e.g., !reload owner_cmds or !reload cogs.owner_cmds). Restricted to the bot owner. |
| `!delmsg <message_id> [channel_id]` / `!delete_msg`      | Delete a specific message sent by the bot (in the current channel or in a specified target channel by ID). Restricted to the bot owner.            |
---
## Configuration & Environment Variables

Copy `.env.example` to `.env` and fill in your API credentials:

```bash
cp .env.example .env
```

| Variable              | Description                                                                                                           |
|-----------------------|-----------------------------------------------------------------------------------------------------------------------|
| `DISCORD_TOKEN`       | Discord Bot Token from the [Discord Developer Portal](https://discord.com/developers/applications?utm_source=gemini). |
| `TWITCH_CLIENT_ID`    | Client ID from the [Twitch Developer Console](https://dev.twitch.tv/console?utm_source=gemini) for IGDB API access.   |
| `TWITCH_ACCESS_TOKEN` | OAuth Access Token generated for IGDB API calls.                                                                      |
| `BOT_OWNER_ID`        | *(Optional)* Discord User ID of the bot owner for elevated administrative access.                                     |
| `DATA_DIR`            | Directory path where SQLite database file will be stored (defaults to `./data`).                                      |

---

## Getting Started

### Method 1: Running with Docker (Recommended)

1. **Clone the Repository:**
```bash
git clone [https://github.com/your-username/LFG-Notice-Bot.git](https://github.com/your-username/LFG-Notice-Bot.git)
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

## License & Privacy

This project stores minimal user data (Discord User IDs, game preferences, and channel IDs for server trackers) strictly required for bot functionality. Read the full details in [`Privacy Policy.md`](./Privacy%20Policy.md).

## Credits & Acknowledgements

### Human Contributors
* **[Vic](https://git.sashie.me/Vic/)** - Lead Developer & Maintainer
* **[Sasha](https://git.sashie.me/sasha)** - Support, Friendship, Infrastructure, Server Hosting & CI/CD Deployment

### AI Assistance & Attribution
This project was developed with the assistance of Generative AI tools:
* **[Google Gemini](https://gemini.google.com/)**: Used for architecture design, feature drafting, regex filtering, and documentation templates.
* **[GitHub Copilot](https://github.com/features/copilot)**: Used for Discord application setup

> **Disclaimer:** AI tools were used as assistive pair-programmers. All generated code has been manually reviewed, edited, tested, and validated by human maintainers. The maintainers take full responsibility for the security, quality, and functionality of this codebase.