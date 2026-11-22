"""SQLite storage shared by all agents: schema, connection, chat history, kv."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

from app import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS planned_sessions (
    date TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    description TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS session_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    completed INTEGER NOT NULL,
    done TEXT NOT NULL,
    rpe INTEGER,
    notes TEXT,
    distance_m INTEGER,
    duration_min INTEGER,
    efforts INTEGER,
    effort_s INTEGER,
    rest_s INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS session_logs_date ON session_logs(date);
CREATE TABLE IF NOT EXISTS benchmarks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    distance_m INTEGER NOT NULL,
    avg_split_s REAL,
    notes TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_channel ON messages(channel, id);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS exercises (
    key TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    aliases TEXT NOT NULL DEFAULT '',
    explanation TEXT NOT NULL DEFAULT '',
    url TEXT
);
CREATE TABLE IF NOT EXISTS fridge_items (
    name TEXT PRIMARY KEY,
    quantity TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS away_days (
    day TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS draft_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL,
    new_recipe_count INTEGER NOT NULL,
    shopping_written INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO draft_meta (id, version, new_recipe_count, shopping_written) VALUES (1, 0, 0, 0);
CREATE TABLE IF NOT EXISTS draft_dinners (
    day TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    name TEXT NOT NULL,
    dinner_type TEXT,
    recipe_slug TEXT,
    ingredients TEXT,
    steps TEXT,
    recipe_created INTEGER NOT NULL DEFAULT 0,
    plan_written INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS draft_shopping (
    item TEXT PRIMARY KEY
);
"""


def now() -> str:
    return datetime.now(config.TZ).isoformat(timespec="seconds")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def init() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


def save_message(channel: str, role: str, content: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO messages (channel, role, content, created_at) VALUES (?, ?, ?, ?)",
            (channel, role, content, now()),
        )


def load_messages(channel: str, limit: int) -> list[dict]:
    """Last `limit` messages of `channel`, oldest first, starting with a user turn."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE channel = ? ORDER BY id DESC LIMIT ?",
            (channel, limit),
        ).fetchall()
    messages = [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
    while messages and messages[0]["role"] != "user":
        messages.pop(0)
    return messages


def kv_get(key: str) -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def kv_set(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
