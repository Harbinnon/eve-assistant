"""Multi-session projects: storage, play time, and how the planner starts and continues them."""

import sqlite3

import pytest

from eve_assistant import planner, projects, risk, snapshots, store


@pytest.fixture
def db():
    conn = store.connect(":memory:")
    snapshots.ensure_character(conn, 7, "Hauler")
    yield conn
    conn.close()


def test_play_time_defaults_and_can_be_changed(db):
    assert projects.weekly_minutes(db) == projects.DEFAULT_WEEKLY_MINUTES
    projects.set_weekly_minutes(db, 600)
    assert projects.weekly_minutes(db) == 600
    with pytest.raises(projects.ProjectError):
        projects.set_weekly_minutes(db, 0)


def test_deadline_check_uses_the_weekly_pace():
    assert projects.finishes_in_time(100, days=7, weekly=360)
    assert not projects.finishes_in_time(400, days=7, weekly=360)
    assert projects.finishes_in_time(400, days=14, weekly=360)


def test_start_progress_close_and_limits(db):
    first = projects.start(db, 7, "haul", "1:2:x", "Haul ore", 120, 5e6, {}, days_to_deadline=3)
    assert projects.start(db, 7, "haul", "1:2:x", "Haul ore", 120, 5e6, {}) == first  # no dupes
    projects.record_progress(db, first, 45)
    (project,) = projects.open_projects(db, 7)
    assert project.minutes_left == 75 and project.share_done == pytest.approx(0.375)
    assert project.deadline is not None

    projects.start(db, 7, "courier", "99", "Courier", 90, 1e6, {})
    with pytest.raises(projects.ProjectError, match="already has"):
        projects.start(db, 7, "courier", "100", "Another", 90, 1e6, {})

    projects.close(db, first, "done")
    assert [p.key for p in projects.open_projects(db, 7)] == ["99"]
    with pytest.raises(projects.ProjectError):
        projects.record_progress(db, first, 10)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("DELETE FROM project_progress")


def _project_start_item(total_minutes=180.0, minutes=60.0):
    return planner._project_item(
        kind="haul",
        key="61000001:1230:sell_raw_jita",
        title="Haul ore to Jita",
        total_minutes=total_minutes,
        isk=9_000_000,
        minutes=minutes,
        rating=risk.rate("low", 2_000_000, 100_000_000),
        reasons=(),
        inputs={"station_id": 61000001, "type_id": 1230, "quantity": 100},
    )


def test_a_long_job_becomes_a_discounted_project_start():
    item = _project_start_item()
    assert item.kind == "project_start"
    assert item.minutes == 60
    expected = 9_000_000 * (60 / 180) * (1 - projects.NOT_FINISHING_CHANCE)
    assert item.isk == pytest.approx(expected)
    assert item.inputs["project"]["total_minutes"] == 180
    assert item.reasons[0].code == "project_value"


def test_a_job_that_fits_is_an_ordinary_item():
    item = _project_start_item(total_minutes=40)
    assert item.kind == "sell_hangar" and item.minutes == 40 and item.isk == 9_000_000


def test_doing_a_project_start_creates_it_and_later_plans_continue_it(db):
    plan = planner.Plan(
        character_id=7,
        minutes_available=60,
        items=(_project_start_item(),),
        alternatives=(),
        notes=(),
        net_worth=100_000_000,
    )
    plan = planner.record(db, plan, {})
    item_id = db.execute("SELECT item_id FROM plan_items WHERE plan_id = ?", (plan.plan_id,))
    item_id = item_id.fetchone()[0]

    planner.respond(db, item_id, "do")
    (project,) = projects.open_projects(db, 7)
    assert project.title == "Haul ore to Jita" and project.minutes_done == 60

    notes = []
    (cont,) = planner.project_candidates(db, 7, 90, 100_000_000, notes)
    assert cont.kind == "project_continue"
    assert cont.minutes == 90  # 120 left, 90 available
    assert cont.isk == pytest.approx(9_000_000 * (90 / 180) * (1 - projects.NOT_FINISHING_CHANCE))
    assert cont.inputs["project_id"] == project.project_id


def test_doing_a_continue_records_progress(db):
    project_id = projects.start(
        db, 7, "courier", "5", "Courier", 100, 2e6, {"risk": {"band": "low", "exposure": 1e6}}
    )
    (cont,) = planner.project_candidates(db, 7, 30, 1e8, [])
    plan = planner.record(
        db,
        planner.Plan(7, 30, (cont,), (), (), 1e8),
        {},
    )
    item_id = db.execute(
        "SELECT item_id FROM plan_items WHERE plan_id = ?", (plan.plan_id,)
    ).fetchone()[0]
    planner.respond(db, item_id, "do")
    assert projects.get(db, project_id).minutes_done == 30
