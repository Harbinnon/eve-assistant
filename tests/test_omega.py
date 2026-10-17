"""What Omega would change, and a goal for Omega via PLEX (roadmap 3.5)."""

import json

import pytest

from eve_assistant import goals, omega, snapshots, store
from eve_assistant.omega import Mining, OmegaComparison

PLEX = goals.PLEX_TYPE_ID


@pytest.fixture
def db():
    conn = store.connect(":memory:")
    snapshots.ensure_character(conn, 7, "Miner")
    conn.execute("INSERT INTO market_prices VALUES (?, ?, NULL, 'x')", (PLEX, 4_735_090.0))
    conn.execute(
        "INSERT INTO sde_types (type_id, name, group_id, published, volume) "
        "VALUES (17471, 'Veldspar III-Grade', 1, 1, 0.1)"
    )
    conn.commit()
    yield conn
    conn.close()


def test_an_omega_goal_follows_the_plex_price(db):
    goal_id = goals.add_goal(db, "omega", 7, level=1)
    (goal,) = [g for g in goals.active_goals(db) if g.goal_id == goal_id]
    assert goal.label == "Omega for 1 month (500 PLEX)"
    status = goals.evaluate(db, goal)
    assert status.target_isk == pytest.approx(500 * 4_735_090)
    db.execute("UPDATE market_prices SET average_price = 5_000_000 WHERE type_id = ?", (PLEX,))
    assert goals.evaluate(db, goal).target_isk == pytest.approx(2_500_000_000)  # not fixed


def test_omega_goal_months_and_a_missing_price(db):
    with pytest.raises(goals.GoalError, match="1 to 12 months"):
        goals.add_goal(db, "omega", 7, level=13)
    goal_id = goals.add_goal(db, "omega", 7, level=3)
    db.execute("DELETE FROM market_prices")
    (goal,) = [g for g in goals.active_goals(db) if g.goal_id == goal_id]
    assert goal.label == "Omega for 3 months (1,500 PLEX)"
    status = goals.evaluate(db, goal)
    assert status.target_isk is None
    assert any(r.code == "no_price" for r in status.reasons)


def test_the_comparison_says_what_omega_adds_and_costs():
    o = OmegaComparison("Miner", Mining("Venture", 384.0), Mining("Procurer", 833.0), 92.0,
                        4_735_090.0)  # fmt: skip
    assert o.per_hour(o.alpha) == pytest.approx(384 * 60 * 92)
    assert o.gain_per_hour == pytest.approx((833 - 384) * 60 * 92)
    assert o.month_cost == pytest.approx(500 * 4_735_090)
    assert o.hours_to_pay == pytest.approx(o.month_cost / o.gain_per_hour)
    assert o.extra_a_month(6) == pytest.approx(o.gain_per_hour * 6 * 52 / 12)
    unpriced = OmegaComparison("Miner", None, Mining("Procurer", 833.0), None, None)
    assert unpriced.gain_per_hour is None
    assert unpriced.hours_to_pay is None


def test_isk_per_m3_comes_from_the_latest_mining_plan(db):
    plan_id = db.execute(
        "INSERT INTO plans (character_id, created_at, minutes_available, inputs) "
        "VALUES (7, '2026-10-17T00:00:00+00:00', 60, '{}')"
    ).lastrowid
    db.execute(
        "INSERT INTO plan_items (plan_id, position, kind, key, title, isk, minutes, risk_rating, "
        "expected_loss, details) VALUES (?, 0, 'mining', 'k', 'Mine', 3681253, 56, 'green', 0, ?)",
        (plan_id, json.dumps({"inputs": {"type_id": 17471, "quantity": 400_000}})),
    )
    assert omega.isk_per_m3(db, 7) == pytest.approx(3_681_253 / 40_000)
    assert omega.compare(db, 7) is None  # no skills data: no comparison
