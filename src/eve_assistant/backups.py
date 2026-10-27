"""Backups of the database.

The history this tool builds can't be fetched again: ESI keeps only about
30 days of wallet and mining data, and plans, sessions and outcomes exist
nowhere else. So the dashboard makes a backup once a day, and
`eve-assistant backup` makes one on demand.

A backup is a consistent copy taken with SQLite's online-backup API (safe
while the dashboard is writing), with the parts that can be downloaded
again left out: CCP's static data and the ESI response cache. That keeps
each backup small. After a restore, `eve-assistant sde-update` reloads the
static data.

Login tokens are not in the database (they are in the encrypted secret
store), so they are never in a backup either; on another PC, log in again.

Kept: the newest backup of each of the last KEEP_DAILY days and of each of
the last KEEP_MONTHLY months. A restore never deletes the current
database; it is moved aside first.
"""

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from eve_assistant import store

KEEP_DAILY = 14
KEEP_MONTHLY = 12
PREFIX = "eve-assistant-"
NAME = re.compile(r"^eve-assistant-(\d{8})-(\d{6})\.sqlite3$")
# Downloaded again on demand, so not worth keeping in every backup.
REGENERABLE_PREFIXES = ("sde_",)
REGENERABLE_TABLES = ("esi_cache",)
# Beside the backups: what the newest one is, so a copy taken from the folder
# (the lab's nightly pull) can tell a fresh backup from a stale one, and a
# restore can be checked against its row counts.
STATE_FILE = "latest.json"
COUNTED = ("snapshots", "plans", "plan_items", "plan_responses", "play_sessions", "goals")


class BackupError(Exception):
    pass


@dataclass(frozen=True)
class Backup:
    path: Path
    taken_at: datetime
    size: int


def default_folder(db_path: Path | str = store.DEFAULT_DB_PATH) -> Path:
    """var/backups, next to the database being backed up."""
    return Path(db_path).parent / "backups"


def _parse_name(path: Path) -> datetime | None:
    match = NAME.match(path.name)
    if not match:
        return None
    return datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S").replace(tzinfo=UTC)


def list_backups(folder: Path | str) -> list[Backup]:
    """Backups in the folder, newest first."""
    folder = Path(folder)
    if not folder.is_dir():
        return []
    found = []
    for path in folder.glob(f"{PREFIX}*.sqlite3"):
        taken = _parse_name(path)
        if taken is not None:
            found.append(Backup(path, taken, path.stat().st_size))
    return sorted(found, key=lambda b: b.taken_at, reverse=True)


def _strip_regenerable(db: sqlite3.Connection) -> None:
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    for table in tables:
        if table.startswith(REGENERABLE_PREFIXES) or table in REGENERABLE_TABLES:
            db.execute(f'DELETE FROM "{table}"')  # noqa: S608  # names from sqlite_master
    db.commit()


def _check(db: sqlite3.Connection, what: str) -> None:
    result = db.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        raise BackupError(f"{what} failed its integrity check: {result}")
    has_history = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if has_history is None:
        raise BackupError(f"{what} isn't an EVE Assistant database.")


def make(
    db_path: Path | str = store.DEFAULT_DB_PATH,
    folder: Path | str | None = None,
    now: datetime | None = None,
) -> Backup:
    """Take a backup now and prune old ones. Returns the new backup."""
    folder = Path(folder) if folder is not None else default_folder(db_path)
    folder.mkdir(parents=True, exist_ok=True)
    now = now or datetime.now(UTC)
    final = folder / f"{PREFIX}{now:%Y%m%d-%H%M%S}.sqlite3"
    partial = final.with_suffix(".partial")
    source = sqlite3.connect(db_path)
    try:
        copy = sqlite3.connect(partial)
        try:
            source.backup(copy)
            _strip_regenerable(copy)
            copy.execute("VACUUM")
            _check(copy, "The new backup")
            rows = _row_counts(copy)
        finally:
            copy.close()
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    finally:
        source.close()
    os.replace(partial, final)
    _write_state(folder, final, now, rows)
    prune(folder, now)
    return Backup(final, now, final.stat().st_size)


def _row_counts(db: sqlite3.Connection) -> dict:
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    counts = {
        t: db.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]  # noqa: S608  # fixed names
        for t in COUNTED
        if t in tables
    }
    counts["schema_version"] = db.execute("SELECT MAX(number) FROM schema_migrations").fetchone()[0]
    return counts


def _write_state(folder: Path, final: Path, now: datetime, rows: dict) -> None:
    """latest.json: when the newest backup finished, its name, size, SHA-256 and
    row counts. Written to a temporary file and swapped in."""
    digest = hashlib.sha256()
    with final.open("rb") as backup:
        for chunk in iter(lambda: backup.read(1 << 20), b""):
            digest.update(chunk)
    state = {
        "completed_at": now.isoformat(timespec="seconds"),
        "file": final.name,
        "bytes": final.stat().st_size,
        "sha256": digest.hexdigest(),
        "rows": rows,
    }
    temp = folder / f"{STATE_FILE}.tmp"
    temp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(temp, folder / STATE_FILE)


def prune(folder: Path | str, now: datetime | None = None) -> list[Path]:
    """Delete backups no longer kept. Returns what was deleted."""
    now = now or datetime.now(UTC)
    keep: set[Path] = set()
    days: set[str] = set()
    months: set[str] = set()
    for backup in list_backups(folder):  # newest first: the first seen per day/month wins
        day, month = f"{backup.taken_at:%Y%m%d}", f"{backup.taken_at:%Y%m}"
        age_days = (now - backup.taken_at).days
        if age_days < KEEP_DAILY and day not in days:
            days.add(day)
            keep.add(backup.path)
        if month not in months and len(months) < KEEP_MONTHLY:
            months.add(month)
            keep.add(backup.path)
    removed = [b.path for b in list_backups(folder) if b.path not in keep]
    for path in removed:
        path.unlink(missing_ok=True)
    return removed


def due(folder: Path | str, now: datetime | None = None) -> bool:
    """True when there is no backup from today (UTC) yet."""
    now = now or datetime.now(UTC)
    newest = list_backups(folder)
    return not newest or newest[0].taken_at.date() < now.date()


def run_if_due(
    folder: Path | str | None,
    db_path: Path | str = store.DEFAULT_DB_PATH,
    now: datetime | None = None,
) -> list[str]:
    """The daily backup, called from the background refresh. Returns problem lines."""
    folder = Path(folder) if folder is not None else default_folder(db_path)
    if not due(folder, now):
        return []
    try:
        make(db_path, folder, now)
    except (BackupError, sqlite3.Error, OSError) as exc:
        return [f"Daily backup failed: {exc}"]
    return []


def restore(
    backup_path: Path | str,
    db_path: Path | str = store.DEFAULT_DB_PATH,
    now: datetime | None = None,
) -> Path:
    """Put a backup in place of the database. The current database is moved
    aside, never deleted; returns where it went. The dashboard must be closed."""
    backup_path, db_path = Path(backup_path), Path(db_path)
    if not backup_path.is_file():
        raise BackupError(f"{backup_path} not found.")
    check = sqlite3.connect(f"file:{backup_path.as_posix()}?mode=ro", uri=True)
    try:
        _check(check, str(backup_path.name))
    finally:
        check.close()
    now = now or datetime.now(UTC)
    aside = db_path.with_name(f"{db_path.stem}.before-restore-{now:%Y%m%d-%H%M%S}{db_path.suffix}")
    try:
        if db_path.exists():
            os.replace(db_path, aside)
        for extra in ("-wal", "-shm"):
            sidecar = Path(f"{db_path}{extra}")
            if sidecar.exists():
                os.replace(sidecar, Path(f"{aside}{extra}"))
    except PermissionError as exc:
        raise BackupError(
            "The database is in use. Close the dashboard (Ctrl+C in its window) and try again."
        ) from exc
    temporary = db_path.with_suffix(".restoring")
    try:
        source = sqlite3.connect(backup_path)
        try:
            target = sqlite3.connect(temporary)
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
        os.replace(temporary, db_path)
        store.connect(db_path).close()  # applies any newer migrations
    except Exception:
        temporary.unlink(missing_ok=True)
        db_path.unlink(missing_ok=True)
        if aside.exists():
            os.replace(aside, db_path)  # put the original back
        raise
    return aside
