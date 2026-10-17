"""What to train next (roadmap 3.6), on test_mining's fake universe.

There the frigate's factor is 2 x Mining 4 (1.20) x Astrogeology 0 (1.00)
x Mining Frigate 2 (1.10). One level more of each gives x1.25/1.20,
x1.05 and x1.15/1.10: Astrogeology first, then Mining Frigate, then Mining.
"""

import json

import pytest

from eve_assistant import goals, mining, snapshots, train_next
from tests.test_mining import ASTRO, FRIG_SKILL, FRIGATE, LASER, MINING, make_db

ORE = 1230


@pytest.fixture
def db():
    conn = make_db()
    conn.executemany(
        "INSERT INTO sde_clone_grade_skills VALUES (1, 'Alpha Test', ?, ?)",
        [(MINING, 5), (ASTRO, 3), (FRIG_SKILL, 4)],
    )
    conn.execute(
        "INSERT INTO sde_types (type_id, name, group_id, published, volume) "
        "VALUES (?, 'Veldspar', 1, 1, 0.1)",
        (ORE,),
    )
    snapshots.record(conn, 7, "ship", {"ship_type_id": FRIGATE, "ship_item_id": 9001})
    snapshots.record(
        conn,
        7,
        "assets",
        [
            {"type_id": LASER, "location_id": 9001, "location_flag": f"HiSlot{n}", "quantity": 1}
            for n in range(2)
        ],
    )
    conn.commit()
    yield conn
    conn.close()


def _priced_plan(db, isk_per_m3: float):
    plan_id = db.execute(
        "INSERT INTO plans (character_id, created_at, minutes_available, inputs) "
        "VALUES (7, '2026-10-17T00:00:00+00:00', 60, '{}')"
    ).lastrowid
    db.execute(
        "INSERT INTO plan_items (plan_id, position, kind, key, title, isk, minutes, risk_rating, "
        "expected_loss, details) VALUES (?, 0, 'mining', 'k', 'Mine', ?, 60, 'green', 0, ?)",
        (
            plan_id,
            1000 * 0.1 * isk_per_m3,
            json.dumps({"inputs": {"type_id": ORE, "quantity": 1000}}),
        ),
    )


def test_skills_are_ranked_by_the_mining_they_add(db):
    now = mining.estimate_current(db, 7).m3_per_minute
    found = train_next.suggestions(db, 7)
    assert [(s.skill, s.level) for s in found] == [
        ("Astrogeology", 1),
        ("Mining Frigate", 3),
        ("Mining", 5),
    ]
    assert found[0].m3_gain == pytest.approx(now * 0.05)
    assert found[1].m3_gain == pytest.approx(now * (1.15 / 1.10 - 1))
    assert all(s.training_minutes > 0 and not s.needs_omega for s in found)
    assert found[0].isk_gain is None  # no plan yet: no price for the ore


def test_isk_goal_weeks_and_skill_book_payback(db):
    _priced_plan(db, 100.0)
    db.execute("INSERT INTO market_prices VALUES (?, 90000, NULL, 'x')", (ASTRO,))
    snapshots.record(db, 7, "wallet", 1_000_000.0)
    goals.add_goal(db, "isk", 7, isk=101_000_000)
    now = mining.estimate_current(db, 7).m3_per_minute
    astro = train_next.suggestions(db, 7)[0]
    assert astro.isk_gain == pytest.approx(now * 0.05 * 60 * 100)
    assert astro.book_price == 90000
    assert astro.payback_hours == pytest.approx(90000 / astro.isk_gain)
    hours = 6.0  # the default play time a week
    rate = now * 60 * 100
    expected = 100_000_000 / (rate * hours) - 100_000_000 / ((rate + astro.isk_gain) * hours)
    assert astro.weeks_sooner == pytest.approx(expected)
    assert train_next.goal_for(db, 7) == "Have 101M ISK"
    assert train_next.suggestions(db, 7)[1].book_price is None  # Mining Frigate is trained


def test_alpha_limits_and_the_skill_point_limit(db):
    db.execute("UPDATE sde_clone_grade_skills SET max_level = 0 WHERE skill_id = ?", (ASTRO,))
    found = train_next.suggestions(db, 7)
    assert "Astrogeology" not in {s.skill for s in found}  # above Alpha's limit
    skills_now = snapshots.latest(db, 7, "skills")
    snapshots.record(db, 7, "skills", {**skills_now, "total_sp": 6_000_000})
    blocked = train_next.suggestions(db, 7)
    assert blocked and all(s.needs_omega for s in blocked)
    assert blocked[0].training_minutes == pytest.approx(found[0].training_minutes / 2)  # Omega


def test_no_skills_or_no_mining_means_no_suggestions(db):
    snapshots.record(db, 7, "assets", [])
    assert train_next.suggestions(db, 7) == []
    snapshots.ensure_character(db, 8, "Nobody")
    assert train_next.suggestions(db, 8) == []
    assert train_next.goal_for(db, 8) is None


def test_an_alpha_at_every_limit_is_told_so(db):
    assert not train_next.at_alpha_limits(db, 7)  # Mining 4 of 5, Astrogeology 0 of 3, ...
    db.execute("UPDATE sde_clone_grade_skills SET max_level = 0 WHERE skill_id = ?", (ASTRO,))
    db.execute("UPDATE sde_clone_grade_skills SET max_level = 2 WHERE skill_id = ?", (FRIG_SKILL,))
    db.execute("UPDATE sde_clone_grade_skills SET max_level = 4 WHERE skill_id = ?", (MINING,))
    assert train_next.suggestions(db, 7) == []
    assert train_next.at_alpha_limits(db, 7)
    account = db.execute("INSERT INTO accounts (label, clone_state) VALUES ('main', 'omega')")
    db.execute("UPDATE characters SET account_id = ? WHERE character_id = 7", (account.lastrowid,))
    assert not train_next.at_alpha_limits(db, 7)
