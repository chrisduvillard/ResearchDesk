import contextlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 4
MAX_ID = 2**63 - 1
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))


def utcnow():
    return datetime.now(timezone.utc)


def iso(value=None):
    return (value or utcnow()).astimezone(timezone.utc).isoformat(timespec="seconds")


def connect(path=None):
    path = Path(path) if path else DATA_DIR / "tracker.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextlib.contextmanager
def database():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def set_setting(conn, key, value):
    conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))


def initialize(conn):
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise RuntimeError("Database was created by a newer version of the tracker")
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS instruments(
      symbol TEXT PRIMARY KEY, name TEXT NOT NULL, asset_class TEXT NOT NULL DEFAULT 'Unverified',
      description TEXT NOT NULL DEFAULT '', provider_symbol TEXT NOT NULL, tv_symbol TEXT,
      verified INTEGER NOT NULL DEFAULT 0, currency TEXT, exchange TEXT,
      price_checked_at TEXT, price_error TEXT, name_source TEXT);
    CREATE TABLE IF NOT EXISTS runs(
      id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
      status TEXT NOT NULL, error TEXT, snapshot_id INTEGER, price_errors TEXT);
    CREATE TABLE IF NOT EXISTS snapshots(
      id INTEGER PRIMARY KEY, fetched_at TEXT NOT NULL, source_as_of TEXT,
      source_url TEXT NOT NULL, raw_path TEXT NOT NULL, content_hash TEXT NOT NULL,
      disclosure TEXT, status TEXT NOT NULL, error TEXT,
      parser_version TEXT NOT NULL, positions_json TEXT);
    CREATE TABLE IF NOT EXISTS events(
      id INTEGER PRIMARY KEY, snapshot_id INTEGER REFERENCES snapshots(id),
      symbol TEXT NOT NULL REFERENCES instruments(symbol), observed_at TEXT NOT NULL,
      source_as_of TEXT NOT NULL, chart_date TEXT NOT NULL, kind TEXT NOT NULL,
      direction TEXT NOT NULL, previous_direction TEXT NOT NULL, eligible INTEGER NOT NULL,
      confidence TEXT NOT NULL, before_json TEXT NOT NULL, after_json TEXT NOT NULL,
      raw_text TEXT NOT NULL, reason TEXT, parser_version TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS events_symbol_date ON events(symbol,observed_at,id);
    CREATE TABLE IF NOT EXISTS prices(
      symbol TEXT NOT NULL REFERENCES instruments(symbol), date TEXT NOT NULL,
      open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
      volume REAL, dividend REAL, split REAL, complete INTEGER NOT NULL,
      fetched_at TEXT NOT NULL, provider TEXT NOT NULL, PRIMARY KEY(symbol,date));
    CREATE TABLE IF NOT EXISTS reviews(
      id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, signature TEXT NOT NULL,
      direction TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS strategy_details(
      id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, signature TEXT NOT NULL,
      model_json TEXT NOT NULL, source TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS details_signature ON strategy_details(symbol,signature,id);
    CREATE TABLE IF NOT EXISTS name_revisions(
      id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, old_name TEXT NOT NULL,
      new_name TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL);
    ''')
    from .dbmf.schema import initialize as initialize_dbmf
    initialize_dbmf(conn)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    from .instruments import SEEDS
    for item in SEEDS:
        cols = list(item)
        conn.execute(f"INSERT OR IGNORE INTO instruments({','.join(cols)}) VALUES({','.join('?' for _ in cols)})", tuple(item.values()))
    conn.commit()
