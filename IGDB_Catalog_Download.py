import os
import requests
import sqlite3
import time
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Retrieve the token safely
CLIENT_ID = os.getenv("IGDB_CLIENT_ID")
ACCESS_TOKEN = os.getenv("IGDB_TOKEN")

headers = {
    "Client-ID": CLIENT_ID,
    "Authorization": f"Bearer {ACCESS_TOKEN}"
}

db = sqlite3.connect("db.sqlite")
cursor = db.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS igdb_games (
    id INTEGER PRIMARY KEY,
    name TEXT,
    slug TEXT
)
""")

# Optional: clear old data
# cursor.execute("DELETE FROM igdb_games")
# db.commit()

offset = 0
batch_size = 500

def fetch_batch(offset):
    query = f"""
        fields id, name, slug;
        where id != null & name != null;
        limit {batch_size};
        offset {offset};
    """

    response = requests.post(
        "https://api.igdb.com/v4/games",
        headers=headers,
        data=query
    )

    # Handle rate limits
    if response.status_code == 429:
        print("Rate limited. Waiting 1 second...")
        time.sleep(1)
        return fetch_batch(offset)

    if response.status_code != 200:
        print("Error:", response.status_code, response.text)
        return []

    return response.json()


while True:
    games = fetch_batch(offset)

    if not games:
        print("No more games returned. Finished.")
        break

    for game in games:
        game_id = game.get("id")
        name = game.get("name")
        slug = game.get("slug")

        if game_id is None or name is None:
            print("Skipping malformed entry:", game)
            continue

        cursor.execute(
            "INSERT OR IGNORE INTO igdb_games (id, name, slug) VALUES (?, ?, ?)",
            (game_id, name, slug)
        )

    db.commit()
    offset += batch_size
    print(f"Fetched {offset} entries...")

db.close()
print("Done.")
