"""SQLite storage layer for the portal."""

import aiosqlite
from pathlib import Path

DB_PATH = Path.home() / ".nanobot" / "portal.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    display_name TEXT DEFAULT '',
    role TEXT NOT NULL DEFAULT 'user',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS xiandou_accounts (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    balance INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS agent_listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL REFERENCES users(id),
    employee_id TEXT NOT NULL,
    employee_name TEXT NOT NULL,
    skill TEXT NOT NULL DEFAULT '',
    price INTEGER NOT NULL DEFAULT 0,
    description TEXT DEFAULT '',
    listed INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(owner_id, employee_id)
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_user_id INTEGER REFERENCES users(id),
    to_user_id INTEGER REFERENCES users(id),
    amount INTEGER NOT NULL,
    tx_type TEXT NOT NULL,
    listing_id INTEGER REFERENCES agent_listings(id),
    memo TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_tx_from ON transactions(from_user_id);
CREATE INDEX IF NOT EXISTS idx_tx_to ON transactions(to_user_id);
CREATE INDEX IF NOT EXISTS idx_listings_owner ON agent_listings(owner_id);
"""


async def get_db() -> aiosqlite.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(str(DB_PATH))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def init_db() -> None:
    db = await get_db()
    await db.executescript(_SCHEMA)
    await db.close()
