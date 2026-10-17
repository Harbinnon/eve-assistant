"""The session view's checklist: steps, ticks, project time, wallet change."""

import json
import sqlite3

import pytest

from eve_assistant import checklist, history, planner, projects, risk, sessions, snapshots, store
from eve_assistant.skills import Reason

CHAR = 7
START = "2026-10-17T18:00:00+00:00"
PROJECT = {"kind": "haul", "key": "k1", "title": "Haul ore", "total_minutes": 120, "isk": 5e6}


@pytest.fixture
def db():
    conn = store.connect(":memory:")
    snapshots.ensure_character(conn, CHAR, "Miner")
    yield conn
    conn.close()


def _plan(db) -> planner.Plan:
    def item(kind, title, minutes, inputs=None):
        return planner.PlanItem(kind=kind, key=title, title=title, isk=1e6, minutes=minutes,
                                risk=risk.rate("low", 1e5, 1e8), inputs=inputs or {})  # fmt: skip

    plan = planner.Plan(
        character_id=CHAR,
        minutes_available=90,
        items=(
            item("sell_hangar", "Sell the ore", 5),
            item("mining", "Mine Veldspar", 55),
            item("project_start", "Haul ore, first trip", 30, {"project": PROJECT}),
        ),
        alternatives=(),
        notes=(Reason("loss_chances_are_guesses"),),
        net_worth=1e8,
    )
    return planner.record(db, plan, {})


def _items(db, plan_id) -> list[int]:
    return [i.item_id for i in planner.load_plan(db, plan_id).items]


def _wallet(db, isk, at):
    db.execute(
        "INSERT INTO snapshots (character_id, kind, taken_at, content_hash, payload) "
        "VALUES (?, 'wallet', ?, ?, ?)",
        (CHAR, at, f"h{isk}", json.dumps(isk)),
    )
    db.execute(
        "INSERT INTO snapshot_checks (character_id, kind, checked_at, outcome) "
        "VALUES (?, 'wallet', ?, 'changed')",
        (CHAR, at),
    )
    db.commit()


def test_steps_tick_off_in_order_and_undo(db):
    plan = _plan(db)
    session = sessions.start(db, CHAR, plan.plan_id, now=START)
    first, second, third = _items(db, plan.plan_id)

    view = checklist.view(db, session)
    assert [s.current for s in view.steps] == [True, False, False]

    checklist.mark(db, session.session_id, first, done=True)
    planner.respond(db, second, "not_now")  # skipped from the session view
    view = checklist.view(db, session)
    assert [(s.done, s.skipped) for s in view.steps] == [
        (True, False),
        (False, True),
        (False, False),
    ]
    assert view.current.item.item_id == third
    assert view.done_count == 1

    checklist.mark(db, session.session_id, first, done=False)  # undo
    assert checklist.view(db, session).current.item.item_id == first
    assert checklist.ticked(db, session.session_id) == frozenset()


def test_ticks_are_append_only(db):
    plan = _plan(db)
    session = sessions.start(db, CHAR, plan.plan_id, now=START)
    checklist.mark(db, session.session_id, _items(db, plan.plan_id)[0], done=True)
    for statement in ("UPDATE session_steps SET done = 0", "DELETE FROM session_steps"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute(statement)


def test_only_running_sessions_and_their_own_steps(db):
    plan, other = _plan(db), _plan(db)
    session = sessions.start(db, CHAR, plan.plan_id, now=START)
    foreign_step, own_step = _items(db, other.plan_id)[0], _items(db, plan.plan_id)[0]
    with pytest.raises(checklist.ChecklistError, match="isn't part of"):
        checklist.mark(db, session.session_id, foreign_step, done=True)
    sessions.finish(db, session.session_id, now="2026-10-17T19:00:00+00:00")
    with pytest.raises(checklist.ChecklistError, match="already ended"):
        checklist.mark(db, session.session_id, own_step, done=True)


def test_ticking_a_project_step_logs_its_minutes_once(db):
    plan = _plan(db)
    session = sessions.start(db, CHAR, plan.plan_id, now=START)
    project_step = _items(db, plan.plan_id)[2]
    checklist.mark(db, session.session_id, project_step, done=True)
    checklist.mark(db, session.session_id, project_step, done=False)
    checklist.mark(db, session.session_id, project_step, done=True)
    (project,) = projects.open_projects(db, CHAR)
    assert project.title == "Haul ore"
    assert project.minutes_done == 30  # once, not three times


def test_start_project_first_then_tick_does_not_log_twice(db):
    plan = _plan(db)
    project_step = _items(db, plan.plan_id)[2]
    planner.respond(db, project_step, "do")  # "Start project" on the plan
    session = sessions.start(db, CHAR, plan.plan_id, now=START)
    checklist.mark(db, session.session_id, project_step, done=True)
    (project,) = projects.open_projects(db, CHAR)
    assert project.minutes_done == 30


def test_wallet_change_since_the_start(db):
    plan = _plan(db)
    _wallet(db, 1_000_000.0, "2026-10-17T17:40:00+00:00")
    _wallet(db, 1_200_000.0, "2026-10-17T18:00:40+00:00")  # read as the session started
    session = sessions.start(db, CHAR, plan.plan_id, now=START)
    _wallet(db, 1_450_000.0, "2026-10-17T18:30:00+00:00")
    view = checklist.view(db, session)
    assert view.wallet_change == pytest.approx(250_000)
    assert view.wallet_checked_at == "2026-10-17T18:30:00+00:00"


def test_wallet_change_unknown_without_a_reading_at_the_start(db):
    session = sessions.start(db, CHAR, None, now=START)
    _wallet(db, 1_450_000.0, "2026-10-17T18:30:00+00:00")
    view = checklist.view(db, session)
    assert view.wallet_change is None
    assert view.steps == []
    assert view.current is None


def test_history_shows_ticked_steps(db):
    plan = _plan(db)
    session = sessions.start(db, CHAR, plan.plan_id, now=START)
    first = _items(db, plan.plan_id)[0]
    checklist.mark(db, session.session_id, first, done=True)
    sessions.finish(db, session.session_id, now="2026-10-17T19:00:00+00:00")
    (past,) = history.sessions_history(db)
    assert past.ticked == frozenset({first})
