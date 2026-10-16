"""The SQLite database: opening it and applying migrations.

Migrations are numbered SQL files in store/migrations, applied in order,
each in its own transaction. Never edit an applied migration; add a new one.
"""

import sqlite3
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

from eve_assistant.config import PROJECT_ROOT

DEFAULT_DB_PATH = PROJECT_ROOT / "var" / "eve-assistant.sqlite3"


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
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    migrate(conn)
    return conn
