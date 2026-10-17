"""Starting and ending a play session, with a snapshot at each end.

The one place the dashboard, the CLI (and later the MCP server) go through:

- **begin**: check the session can start, note the time, take the start
  snapshot, record the start.
- **end**: check the ending is valid, take the end snapshot (not for a
  cancelled session), record the end, work out the outcome.

Checks come before snapshots, so a mistake never costs one. A snapshot that
fails doesn't stop the session; its problems are recorded with it.
Connections are short, because a snapshot takes a few seconds and has its
own.
"""

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from eve_assistant import detection, outcomes, sessions, store
from eve_assistant.sessions import PlaySession

# Snapshots one character (needs ESI); returns problem lines, empty when all went well.
SnapshotTaker = Callable[[int], list[str]]


@dataclass(frozen=True)
class Result:
    session: PlaySession
    problems: list[str]  # from the snapshot; the session is recorded regardless


def _take(snapshot: SnapshotTaker | None, character_id: int) -> list[str]:
    if snapshot is None:
        return ["Snapshots are not available here, so none was taken."]
    try:
        return list(snapshot(character_id))
    except Exception as exc:  # noqa: BLE001  # the session is still recorded
        return [f"Snapshot failed: {exc}"]


def _connect(db_path: Path | str) -> sqlite3.Connection:
    return store.connect(db_path)


def begin(
    db_path: Path | str,
    snapshot: SnapshotTaker | None,
    *,
    character_id: int | None = None,
    plan_id: int | None = None,
) -> Result:
    """Start a session for a character, or for the character a plan belongs to."""
    db = _connect(db_path)
    try:
        if character_id is None:
            if plan_id is None:
                raise sessions.SessionError("Say which character, or which plan to follow.")
            row = db.execute(
                "SELECT character_id FROM plans WHERE plan_id = ?", (plan_id,)
            ).fetchone()
            if row is None:
                raise sessions.SessionNotFound(f"No plan {plan_id}.")
            character_id = row["character_id"]
        sessions.check_start(db, character_id, plan_id)
    finally:
        db.close()
    now = store.utc_now()  # when the player pressed start, before the snapshot
    problems = _take(snapshot, character_id)
    db = _connect(db_path)
    try:
        session = sessions.start(db, character_id, plan_id, problems, now=now)
    finally:
        db.close()
    return Result(session, problems)


def end(
    db_path: Path | str,
    snapshot: SnapshotTaker | None,
    session_id: int,
    *,
    how: str = "finished",
    minutes_played: float | None = None,
) -> Result:
    """Finish or cancel a running session; a finished one gets an end snapshot
    and its outcome worked out."""
    now = store.utc_now()
    db = _connect(db_path)
    try:
        session = sessions.get(db, session_id)
        sessions.check_end(session, minutes_played, how, now)
    finally:
        db.close()
    problems = _take(snapshot, session.character_id) if how == "finished" else []
    db = _connect(db_path)
    try:
        ended = sessions.finish(db, session_id, minutes_played, how, problems, now=now)
        outcomes.update(db, ended.character_id)
    finally:
        db.close()
    return Result(ended, problems)


def running_session_id(db_path: Path | str, character_id: int) -> int:
    """The running session's ID for a character; SessionError if none."""
    db = _connect(db_path)
    try:
        session = sessions.open_session(db, character_id)
    finally:
        db.close()
    if session is None:
        raise sessions.SessionError("No session is running for that character.")
    return session.session_id


def record_detected(db_path: Path | str, character_id: int, login: str) -> PlaySession:
    """Record a session noticed from EVE's login times, after the player said
    yes, with the plan made just before it; then work out its outcome."""
    db = _connect(db_path)
    try:
        found = detection.find(db, character_id, login)
        session = sessions.record_past(
            db, found.character_id, found.login, found.logout, found.plan_id, detection.NOTE
        )
        outcomes.update(db, found.character_id)
    finally:
        db.close()
    return session


def dismiss_detected(db_path: Path | str, character_id: int, login: str) -> None:
    db = _connect(db_path)
    try:
        detection.dismiss(db, character_id, login)
    finally:
        db.close()
