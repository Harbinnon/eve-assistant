"""The SQLite database: opening it and applying migrations.

Migrations are numbered SQL files in store/migrations, applied in order,
each in its own transaction. Never edit an applied migration; add a new one.

The trade tool's market data lives in a second file next to it
(market.sqlite3), attached to every connection as "market": it's bulky and
can always be fetched again, so it isn't migrated or backed up. When its
layout changes (MARKET_SCHEMA_VERSION), its tables are recreated empty.
"""

import sqlite3
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

from eve_assistant.config import DATA_DIR

DEFAULT_DB_PATH = DATA_DIR / "eve-assistant.sqlite3"
MARKET_FILE = "market.sqlite3"
MEMORY = ":memory:"  # an in-memory database, as SQLite names it
MARKET_SCHEMA_VERSION = "1"


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _migrations() -> list[tuple[int, str, str]]:
    folder = resources.files("eve_assistant.store") / "migrations"
    found = []
    for entry in folder.iterdir():
        if entry.name.endswith(".sql"):
            number = int(entry.name.split("_", 1)[0])
            found.append((number, entry.name, entry.read_text(encoding="utf-8")))
    return sorted(found)


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply any migrations not yet applied. Returns the names applied."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(number INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    done = {row[0] for row in conn.execute("SELECT number FROM schema_migrations")}
    applied = []
    for number, name, sql in _migrations():
        if number in done:
            continue
        # executescript commits first; wrap explicitly so a failure rolls back.
        conn.executescript(
            f"BEGIN;\n{sql}\nINSERT INTO schema_migrations VALUES "
            f"({number}, '{name}', '{utc_now()}');\nCOMMIT;"
        )
        applied.append(name)
    return applied


def connect(path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open the database, creating and migrating it if needed."""
    if str(path) != MEMORY:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    migrate(conn)
    attach_market(conn, path)
    return conn


def market_path(path: Path | str) -> str:
    """The market file next to a database file (in memory for an in-memory one)."""
    return MEMORY if str(path) == MEMORY else str(Path(path).with_name(MARKET_FILE))


def attach_market(conn: sqlite3.Connection, path: Path | str) -> None:
    """Attach the market data as "market" (its tables come with ensure_market)."""
    conn.execute("ATTACH DATABASE ? AS market", (market_path(path),))
    if str(path) != MEMORY:
        conn.execute("PRAGMA market.journal_mode = WAL")


def ensure_market(conn: sqlite3.Connection) -> None:
    """Create the market tables, or recreate them empty after a layout change."""
    conn.execute("CREATE TABLE IF NOT EXISTS market.meta (key TEXT PRIMARY KEY, value TEXT)")
    row = conn.execute("SELECT value FROM market.meta WHERE key = 'schema'").fetchone()
    if row is not None and row[0] == MARKET_SCHEMA_VERSION:
        return
    for (table,) in conn.execute(
        "SELECT name FROM market.sqlite_master WHERE type = 'table'"
    ).fetchall():
        conn.execute(f'DROP TABLE market."{table}"')  # noqa: S608  # names from sqlite_master
    schema = (resources.files("eve_assistant.store") / "market_schema.sql").read_text("utf-8")
    conn.executescript(schema)
    conn.execute(
        "INSERT OR REPLACE INTO market.meta VALUES ('schema', ?)", (MARKET_SCHEMA_VERSION,)
    )
    conn.commit()
