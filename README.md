# LFG Notice Bot

_"Hi! I allow you to subscribe to a list of games, and when one of those games is mentioned, you will also be automatically tagged so you can team up and start playing with people."_

A feature-rich Discord slash-command bot built with Python and `discord.py` that allows server members to subscribe to their favorite games and receive targeted pings when others are looking for a group (LFG)[cite: 9, 10]. Powered by the IGDB database for comprehensive game search and metadata caching.

---

## Key Features

* **Targeted LFG Notifications:** Notify only players interested in a specific game without relying on mass `@everyone` or `@here` mentions.
* **IGDB Database Integration:** Autocomplete and search millions of games directly from IGDB, automatically resolving special/deluxe editions to base games.
* **Live Top Games Leaderboard:** Post dynamic, self-updating embed trackers displaying the server's most popular games[cite: 10, 11].
* **Custom Ping Templates:** Customize individual ping formatting and templates to suit your style.
* **Privacy & Data Ownership:** Native `/delete_my_data` and `/unsubscribe` commands give users total control over their stored preferences.
* **Docker Ready:** Multi-stage Docker deployment setup with SQLite database persistence.

---

## 🛠Command Reference

### User Commands
| Command                               | Description                                                              |
|---------------------------------------|--------------------------------------------------------------------------|
| `/subscribe <game>`                   | Subscribe to notifications for a game (with autocomplete & IGDB lookup). |
| `/unsubscribe <game>`                 | Unsubscribe from active notifications for a game.                        |
| `/user_subscriptions [user] [public]` | View active game subscriptions for yourself or another member.           |
| `/game_info <game>`                   | Get details, genres, and cover image for a game.                         |
| `/ping_game <game> [public]`          | Send an LFG ping mentioning all subscribers of a game.                   |
| `/show_subscribers <game>`            | View subscribers for a game without triggering a ping.                   |
| `/top_games [limit] [public]`         | View the most subscribed games on the server.                            |
| `/set_ping_template`                  | Set a custom template for your `/ping_game` notifications.               |
| `/view_ping_template`                 | View your current custom ping template setting.                          |
| `/reset_ping_template`                | Reset your custom ping template to default.                              |
| `/delete_my_data`                     | Purge all your active game subscriptions and settings.                   |
| `/help`                               | View an interactive command guide.                                       |

### Bot Config Commands (`/config`)
| Command                                               | Description                                                                                                                                                        |
|-------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `/config setup_top_games [limit] [interval]`          | Post a live-updating top games leaderboard message in the current channel. `interval` accepts decimal hours (e.g., `0.25` for 15m, `0.5` for 30m, default: `1.0`). |
| `/config edit_top_games <message> [limit] [interval]` | Update display limit or interval settings (e.g., `0.5` for 30m), or leave options empty to force an immediate refresh.                                             |
| `/config force_update_top_games <message>`            | Force an immediate refresh on an active live tracker message.                                                                                                      |
| `/config remove_top_games <message>`                  | Stop tracking and delete an active live top games message.                                                                                                         |

---

### Bot Owner Commands

#### Slash Commands (`/owner`)
| Command                                 | Description                                                                                                       |
|-----------------------------------------|-------------------------------------------------------------------------------------------------------------------|
| `/owner overview`                       | View a global subscription overview across all servers.                                                           |
| `/owner subscribe_user <user> <game>`   | Force-subscribe a target user to a specific game (supports autocomplete).                                         |
| `/owner unsubscribe_user <user> <game>` | Force-unsubscribe a target user from a game (autocompletes only games that the user is subscribed to).            |
| `/owner delete_user_data <user>`        | Permanently purge all stored subscription data for a specific user (triggers an interactive confirmation prompt). |

#### Prefix Commands
| Command                                                 | Description                                                                                                                                 |
|---------------------------------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------|
| `!delmsg <message_id> [channel_id]` / `!delete_msg`     | Delete a specific message sent by the bot (in the current channel or in a specified target channel by ID). Restricted to the bot owner.     |

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