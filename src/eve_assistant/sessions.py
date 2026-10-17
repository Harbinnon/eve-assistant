"""Play sessions: when the player started and stopped playing.

A session is started (optionally from a plan) and later finished or
cancelled. The caller takes a snapshot at each end and passes in any
problems it had, so outcomes can be worked out from the data either side.
Both ends are append-only rows; a session with no end row is open.

Some ESI data is cached for up to an hour (wallet journal, assets), so the
snapshot at the end may not show the whole session yet. That is fine: the
entries carry their own timestamps, and outcomes are worked out later.
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from eve_assistant.store import utc_now

HOW = ("finished", "cancelled")
# An open session older than this probably means "I'm done" was never
# pressed; the dashboard then asks how long the player actually played.
STALE_AFTER = timedelta(hours=12)


class SessionError(Exception):
    pass


@dataclass(frozen=True)
class PlaySession:
    session_id: int
    character_id: int
    character_name: str
    plan_id: int | None
    started_at: str
    ended_at: str | None
    how: str | None
    start_problems: list[str]
    end_problems: list[str]

    @property
    def is_open(self) -> bool:
        return self.ended_at is None

    def minutes(self, now: str | None = None) -> float:
        """Minutes played: to the end, or so far if still open."""
        end = _parse(self.ended_at or now or utc_now())
        return max(0.0, (end - _parse(self.started_at)).total_seconds() / 60)

    def is_stale(self, now: str | None = None) -> bool:
        return self.is_open and self.minutes(now) * 60 > STALE_AFTER.total_seconds()


def _parse(stamp: str) -> datetime:
    value = datetime.fromisoformat(stamp)
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


_SELECT = (
    "SELECT s.session_id, s.character_id, c.name, s.plan_id, s.started_at, "
    "s.start_problems, e.ended_at, e.how, e.end_problems "
    "FROM play_sessions s JOIN characters c USING (character_id) "
    "LEFT JOIN play_session_ends e USING (session_id)"
)


def _load(row) -> PlaySession:
    return PlaySession(
        session_id=row["session_id"],
        character_id=row["character_id"],
        character_name=row["name"],
        plan_id=row["plan_id"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        how=row["how"],
        start_problems=json.loads(row["start_problems"]),
        end_problems=json.loads(row["end_problems"]) if row["end_problems"] else [],
    )


def get(db: sqlite3.Connection, session_id: int) -> PlaySession:
    row = db.execute(f"{_SELECT} WHERE s.session_id = ?", (session_id,)).fetchone()
    if row is None:
        raise SessionError(f"No session {session_id}.")
    return _load(row)


def open_sessions(db: sqlite3.Connection, character_id: int | None = None) -> list[PlaySession]:
    query = f"{_SELECT} WHERE e.session_id IS NULL"
    params: tuple = ()
    if character_id is not None:
        query += " AND s.character_id = ?"
        params = (character_id,)
    return [_load(r) for r in db.execute(query + " ORDER BY s.started_at", params)]


def open_session(db: sqlite3.Connection, character_id: int) -> PlaySession | None:
    found = open_sessions(db, character_id)
    return found[0] if found else None


def recent(db: sqlite3.Connection, limit: int = 20) -> list[PlaySession]:
    rows = db.execute(f"{_SELECT} ORDER BY s.started_at DESC, s.session_id DESC LIMIT ?", (limit,))
    return [_load(r) for r in rows]


def start(
    db: sqlite3.Connection,
    character_id: int,
    plan_id: int | None = None,
    problems: list[str] | tuple[str, ...] = (),
    now: str | None = None,
) -> PlaySession:
    """Record the start of a session. `now` is when the player pressed start,
    taken before the snapshot so the snapshot's few seconds don't count."""
    known = db.execute("SELECT 1 FROM characters WHERE character_id = ?", (character_id,))
    if known.fetchone() is None:
        raise SessionError(f"Unknown character {character_id}.")
    if plan_id is not None:
        row = db.execute("SELECT character_id FROM plans WHERE plan_id = ?", (plan_id,)).fetchone()
        if row is None:
            raise SessionError(f"No plan {plan_id}.")
        if row["character_id"] != character_id:
            raise SessionError(f"Plan {plan_id} is for another character.")
    already = open_session(db, character_id)
    if already is not None:
        raise SessionError(
            f"{already.character_name} already has a session running since "
            f"{already.started_at}. Finish or cancel it first."
        )
    cursor = db.execute(
        "INSERT INTO play_sessions (character_id, plan_id, started_at, start_problems) "
        "VALUES (?, ?, ?, ?)",
        (character_id, plan_id, now or utc_now(), json.dumps(list(problems))),
    )
    db.commit()
    return get(db, cursor.lastrowid)


def check_end(
    session: PlaySession,
    minutes_played: float | None,
    how: str = "finished",
    now: str | None = None,
) -> None:
    """Raise SessionError if this session can't be ended this way. Callers use it
    before taking the end snapshot, so a mistake doesn't cost a snapshot."""
    if how not in HOW:
        raise SessionError("A session ends as finished or cancelled.")
    if not session.is_open:
        raise SessionError(f"Session {session.session_id} has already ended.")
    if how == "cancelled":
        return
    elapsed = session.minutes(now)
    if minutes_played is None:
        if session.is_stale(now):
            raise SessionError(
                f"This session started {elapsed / 60:.0f} hours ago. "
                "Say how many minutes you actually played."
            )
        return
    if minutes_played <= 0:
        raise SessionError("Minutes played must be more than zero.")
    if minutes_played > elapsed + 1:
        raise SessionError(
            f"The session started {elapsed:.0f} minutes ago, so it can't have "
            f"lasted {minutes_played:.0f}."
        )


def finish(
    db: sqlite3.Connection,
    session_id: int,
    minutes_played: float | None = None,
    how: str = "finished",
    problems: list[str] | tuple[str, ...] = (),
    now: str | None = None,
) -> PlaySession:
    """Record the end of a session. `minutes_played` corrects the end time
    when the player stopped earlier (e.g. forgot to press "I'm done")."""
    session = get(db, session_id)
    now = now or utc_now()
    if how == "cancelled":
        minutes_played = None
    check_end(session, minutes_played, how, now)
    if minutes_played is None:
        ended_at = now
    else:
        ended_at = _stamp(_parse(session.started_at) + timedelta(minutes=minutes_played))
    db.execute(
        "INSERT INTO play_session_ends (session_id, ended_at, recorded_at, how, end_problems) "
        "VALUES (?, ?, ?, ?, ?)",
        (session_id, ended_at, now, how, json.dumps(list(problems))),
    )
    db.commit()
    return get(db, session_id)
