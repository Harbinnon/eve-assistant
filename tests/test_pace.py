import json

import pytest

from eve_assistant import goals, pace, planner, projects, sessions, snapshots, store
from eve_assistant.risk import RiskRating

NOW = "2026-10-21T12:00:00+00:00"  # a Wednesday; the week started Monday 19 Oct


@pytest.fixture
def db():
    conn = store.connect(":memory:")
    snapshots.ensure_character(conn, 7, "Miner")
    snapshots.ensure_character(conn, 8, "Alt")
    snapshots.record(conn, 7, "wallet", 1_000_000.0)
    yield conn
    conn.close()


def play(db, start, minutes, character=7, how="finished", net=None):
    from datetime import datetime, timedelta

    session = sessions.start(db, character, now=start)
    end = (datetime.fromisoformat(start) + timedelta(minutes=minutes)).isoformat()
    ended = sessions.finish(db, session.session_id, how=how, now=end)
    if net is not None:
        db.execute(
            "INSERT INTO session_outcomes (session_id, computed_at, settled, content_hash, "
            "details) VALUES (?, 'x', 1, 'h', ?)",
            (session.session_id, json.dumps({"isk": {"net": net}})),
        )
        db.commit()
    return ended


def test_play_time_is_the_setting_until_sessions_exist(db):
    play_time = pace.weekly_play(db, now=NOW)
    assert play_time.minutes == projects.DEFAULT_WEEKLY_MINUTES
    assert play_time.reason().code == "play_time_setting"


def test_play_time_learns_from_complete_weeks(db):
    play(db, "2026-10-06T18:00:00+00:00", 90)  # week of 5 Oct
    play(db, "2026-10-13T18:00:00+00:00", 100)  # week of 12 Oct
    play(db, "2026-10-15T18:00:00+00:00", 50)
    play(db, "2026-10-20T18:00:00+00:00", 600)  # this week: not complete, not counted
    play(db, "2026-07-01T18:00:00+00:00", 600)  # older than eight weeks
    play(db, "2026-10-07T18:00:00+00:00", 600, how="cancelled")
    learned = pace.weekly_play(db, now=NOW)
    assert (learned.weeks, learned.observed) == (2, 120)
    assert learned.minutes == pytest.approx((360 * 2 + 120 * 2) / 4)
    assert learned.reason().code == "play_time_learned"


def test_overlapping_and_week_crossing_sessions(db):
    play(db, "2026-10-13T18:00:00+00:00", 60, character=7)
    play(db, "2026-10-13T18:30:00+00:00", 60, character=8)  # overlaps by 30 minutes
    play(db, "2026-10-11T23:30:00+00:00", 60, character=8)  # Sunday into Monday
    weeks = pace._union_minutes_by_week(pace._session_intervals(db))
    assert sorted(weeks.values()) == [30, 120]


def test_finish_rate_moves_from_the_guess(db):
    assert pace.finish_rate(db).not_finishing == projects.NOT_FINISHING_CHANCE
    for n, status in enumerate(("done", "done", "done", "dropped")):
        project_id = projects.start(db, 7, "haul", f"k{n}", "Haul", 120, 1e6, {})
        projects.close(db, project_id, status)
    rate = pace.finish_rate(db)
    assert (rate.done, rate.dropped) == (3, 1)
    assert rate.not_finishing == pytest.approx((0.10 * 3 + 1) / 7)


def test_project_items_use_the_learned_rate():
    finish = pace.FinishRate(0.25, 3, 1)
    item = planner._project_item(
        kind="haul",
        key="k",
        title="Haul",
        total_minutes=200,
        isk=1_000_000,
        minutes=50,
        rating=RiskRating("very_low", "green", 0.0, 0.0, 0.0),
        reasons=(),
        inputs={},
        finish=finish,
    )
    assert item.isk == pytest.approx(1_000_000 * 0.25 * 0.75)
    value = item.reasons[0].detail
    assert (value["not_finishing"], value["learned_from"]) == (0.25, 4)


def test_goal_dates_from_banked_isk(db):
    goal_id = goals.add_goal(db, "isk", 7, isk=13_000_000)
    (goal,) = goals.active_goals(db)
    assert goal.goal_id == goal_id
    waiting = goals.evaluate(db, goal)
    assert waiting.weeks_to_isk is None
    assert "goal_date_needs_sessions" in [r.code for r in waiting.reasons]

    play(db, "2026-10-13T18:00:00+00:00", 60, net=1_500_000)
    play(db, "2026-10-14T18:00:00+00:00", 60, net=500_000)
    status = goals.evaluate(db, goal)
    assert status.earning.isk_per_hour == pytest.approx(1_000_000)
    # 12M needed at 1M an hour; play time learned from that one week (120 min)
    weekly = pace.weekly_play(db).minutes
    assert status.weeks_to_isk == pytest.approx(12_000_000 / (1_000_000 * weekly / 60))


def test_weeks_to_needs_a_positive_rate():
    assert pace.weeks_to(1e6, None, 360) is None
    assert pace.weeks_to(1e6, pace.EarningRate(-5, 2, 2), 360) is None
