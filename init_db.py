import sqlite3

db = sqlite3.connect("db.sqlite")
cursor = db.cursor()

# Create tables
cursor.execute("""
CREATE TABLE IF NOT EXISTS games (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS subscriptions (
    user_id INTEGER,
    game_id INTEGER,
    UNIQUE(user_id, game_id)
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS igdb_games (
    id INTEGER PRIMARY KEY,
    name TEXT,
    slug TEXT,
    popularity REAL
);
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS subscriptions (
    user_id INTEGER,
    game_id INTEGER,
    UNIQUE(user_id, game_id)
)
""")
               
db.commit()
db.close()

print("Database initialized.")

