"""The session view: a running session's plan as a checklist.

While a session runs, Today shows its steps in order, which are ticked off,
which one is next, and how the wallet has moved since the start. "Start
this plan" is the one commitment; steps are then ticked off as they're done
("not now" skips one).

Ticking is append-only: un-ticking adds a row, and the latest row for a
step wins. Ticking off a project step also logs its minutes on the project,
once, as "Start project" does.
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from eve_assistant import planner, sessions
from eve_assistant.planner import StoredItem, StoredPlan
from eve_assistant.sessions import PlaySession
from eve_assistant.store import utc_now

PROJECT_KINDS = ("project_start", "project_continue")
SKIPPED = ("not_now", "mute")
# The wallet read as a session starts is stored just after its start time.
START_SLACK = timedelta(minutes=2)


class ChecklistError(Exception):
    pass


@dataclass(frozen=True)
class Step:
    item: StoredItem
    number: int
    done: bool
    skipped: bool  # answered "not now" or "not this kind"
    current: bool  # the first step neither done nor skipped


@dataclass(frozen=True)
class SessionView:
    session: PlaySession
    plan: StoredPlan | None
    steps: list[Step]
    wallet_change: float | None  # ISK since the start; None if either end is unknown
    wallet_checked_at: str | None  # when EVE's wallet was last read

    @property
    def current(self) -> Step | None:
        return next((s for s in self.steps if s.current), None)

    @property
    def done_count(self) -> int:
        return sum(1 for s in self.steps if s.done)


def ticked(db: sqlite3.Connection, session_id: int) -> frozenset[int]:
    """Plan items ticked off in a session (the latest mark for each)."""
    state: dict[int, bool] = {}
    for row in db.execute(
        "SELECT item_id, done FROM session_steps WHERE session_id = ? ORDER BY mark_id",
        (session_id,),
    ):
        state[row["item_id"]] = bool(row["done"])
    return frozenset(item for item, done in state.items() if done)


def _steps(plan: StoredPlan | None, done: frozenset[int]) -> list[Step]:
    if plan is None:
        return []
    found, current_seen = [], False
    for number, item in enumerate(plan.items, start=1):
        is_done = item.item_id in done
        skipped = not is_done and item.response in SKIPPED
        current = not (current_seen or is_done or skipped)
        current_seen = current_seen or current
        found.append(Step(item, number, is_done, skipped, current))
    return found


def _wallet_change(db: sqlite3.Connection, session: PlaySession):
    start = datetime.fromisoformat(session.started_at).astimezone(UTC) + START_SLACK
    at_start = db.execute(
        "SELECT payload FROM snapshots WHERE character_id = ? AND kind = 'wallet' "
        "AND taken_at <= ? ORDER BY taken_at DESC, snapshot_id DESC LIMIT 1",
        (session.character_id, start.isoformat(timespec="seconds")),
    ).fetchone()
    now = db.execute(
        "SELECT payload FROM snapshots WHERE character_id = ? AND kind = 'wallet' "
        "ORDER BY taken_at DESC, snapshot_id DESC LIMIT 1",
        (session.character_id,),
    ).fetchone()
    checked = db.execute(
        "SELECT MAX(checked_at) FROM snapshot_checks WHERE character_id = ? "
        "AND kind = 'wallet' AND outcome != 'failed'",
        (session.character_id,),
    ).fetchone()[0]
    if at_start is None or now is None:
        return None, checked
    return float(json.loads(now["payload"])) - float(json.loads(at_start["payload"])), checked


def view(db: sqlite3.Connection, session: PlaySession) -> SessionView:
    plan = None
    if session.plan_id is not None:
        try:
            plan = planner.load_plan(db, session.plan_id)
        except planner.PlanError:
            plan = None
    change, checked = _wallet_change(db, session)
    return SessionView(session, plan, _steps(plan, ticked(db, session.session_id)), change, checked)


def views(db: sqlite3.Connection) -> list[SessionView]:
    """A view for every session still running."""
    return [view(db, s) for s in sessions.open_sessions(db)]


def mark(
    db: sqlite3.Connection, session_id: int, item_id: int, done: bool, now: str | None = None
) -> None:
    """Tick a step off (done=True) or put it back. Only for a running
    session, and only for a step of its own plan."""
    session = sessions.get(db, session_id)
    if not session.is_open:
        raise ChecklistError("That session has already ended.")
    row = db.execute(
        "SELECT kind FROM plan_items WHERE item_id = ? AND plan_id = ? AND position IS NOT NULL",
        (item_id, session.plan_id),
    ).fetchone()
    if row is None:
        raise ChecklistError("That step isn't part of this session's plan.")
    with db:
        db.execute(
            "INSERT INTO session_steps (session_id, item_id, done, marked_at) VALUES (?, ?, ?, ?)",
            (session_id, item_id, int(done), now or utc_now()),
        )
    if done and row["kind"] in PROJECT_KINDS:
        started = db.execute(
            "SELECT 1 FROM plan_responses WHERE item_id = ? AND response = 'do'", (item_id,)
        ).fetchone()
        if started is None:  # "Start project" wasn't pressed: log the minutes now
            planner.respond(db, item_id, "do")
