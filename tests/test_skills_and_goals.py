"""Skill plans and goals against a small hand-made static data set.

Fake universe: a Barge ship needs Barge skill 1. Barge skill needs Mining 4
and Geology 3. Alpha may train Mining to 4 but Geology and Barge not at all.
All skills are rank 1, perception primary and willpower secondary.
"""

import httpx
import pytest
import respx

from eve_assistant import goals, skills, snapshots, store
from eve_assistant.esi.client import EsiClient

MINING, GEOLOGY, BARGE_SKILL, BARGE_SHIP, FRIGATE = 1, 2, 3, 100, 101
RACE = 1


@pytest.fixture
def db():
    conn = store.connect(":memory:")
    types = [
        (MINING, "Mining"), (GEOLOGY, "Geology"), (BARGE_SKILL, "Barge"),
        (BARGE_SHIP, "Big Barge"), (FRIGATE, "Little Frigate"),
    ]  # fmt: skip
    conn.executemany(
        "INSERT INTO sde_types (type_id, name, group_id, published) VALUES (?, ?, 1, 1)", types
    )
    attrs = []
    for skill in (MINING, GEOLOGY, BARGE_SKILL):
        attrs += [(skill, 275, 1.0), (skill, 180, 167), (skill, 181, 168)]
    attrs += [
        (BARGE_SKILL, 182, MINING), (BARGE_SKILL, 277, 4),
        (BARGE_SKILL, 183, GEOLOGY), (BARGE_SKILL, 278, 3),
        (BARGE_SHIP, 182, BARGE_SKILL), (BARGE_SHIP, 277, 1),
        (FRIGATE, 182, MINING), (FRIGATE, 277, 1),
    ]  # fmt: skip
    conn.executemany("INSERT INTO sde_type_attributes VALUES (?, ?, ?)", attrs)
    conn.execute(
        "INSERT INTO sde_clone_grade_skills VALUES (?, 'Alpha Test', ?, 4)", (RACE, MINING)
    )
    conn.execute(
        "INSERT INTO sde_meta (build_number, release_date, loaded_at) VALUES (1, 'x', 'y')"
    )
    conn.commit()
    yield conn
    conn.close()


def _character(db, trained, total_sp=100_000, wallet=1_000_000.0, character_id=7):
    snapshots.ensure_character(db, character_id, "Pilot")
    snapshots.record(db, character_id, "public", {"race_id": RACE, "name": "Pilot"})
    snapshots.record(
        db,
        character_id,
        "attributes",
        {"charisma": 20, "intelligence": 20, "memory": 20, "perception": 20, "willpower": 20},
    )
    snapshots.record(
        db,
        character_id,
        "skills",
        {
            "total_sp": total_sp,
            "skills": [
                {
                    "skill_id": s,
                    "trained_skill_level": t,
                    "active_skill_level": a,
                    "skillpoints_in_skill": sp,
                }
                for s, (t, a, sp) in trained.items()
            ],  # fmt: skip
        },
    )
    snapshots.record(db, character_id, "wallet", wallet)
    return character_id


def test_requirements_list_prerequisites_first(db):
    order = [skill for skill, _ in skills.full_requirements(db, BARGE_SHIP)]
    assert order.index(MINING) < order.index(BARGE_SKILL)
    assert order.index(GEOLOGY) < order.index(BARGE_SKILL)
    assert dict(skills.full_requirements(db, BARGE_SHIP)) == {MINING: 4, GEOLOGY: 3, BARGE_SKILL: 1}


def test_training_time_uses_attributes_and_partial_progress(db):
    cid = _character(db, {MINING: (3, 3, 8000 + 1000)})  # 1,000 SP into level 4
    character = skills.load_character_skills(db, cid)
    req = skills.plan_skill(db, character, MINING, 4)
    (step,) = req.steps
    assert step.skillpoints == 45255 - 9000
    assert step.minutes_omega == pytest.approx((45255 - 9000) / 30)  # 20 + 20/2 per minute
    assert req.minutes_as_alpha == pytest.approx(req.minutes_as_omega * 2)


def test_alpha_capped_goal_needs_omega(db):
    cid = _character(db, {MINING: (4, 4, 45255)})
    req = skills.plan(db, skills.load_character_skills(db, cid), BARGE_SHIP)
    assert req.needs_omega and req.minutes_as_alpha is None
    assert {r.detail["skill"] for r in req.reasons if r.code == "alpha_cap"} == {"Geology", "Barge"}
    assert [s.skill_id for s in req.steps if not s.allowed_for_alpha] == [GEOLOGY] * 3 + [
        BARGE_SKILL
    ]


def test_trained_but_alpha_locked(db):
    cid = _character(
        db,
        {MINING: (4, 4, 45255), GEOLOGY: (3, 0, 8000), BARGE_SKILL: (1, 0, 250)},
    )
    req = skills.plan(db, skills.load_character_skills(db, cid), BARGE_SHIP)
    assert not req.usable_now and req.usable_with_omega_now
    assert req.steps == ()
    assert "trained_but_alpha_locked" in {r.code for r in req.reasons}

    db.execute("INSERT INTO accounts (label, clone_state) VALUES ('main', 'omega')")
    db.execute("UPDATE characters SET account_id = 1")
    assert skills.plan(db, skills.load_character_skills(db, cid), BARGE_SHIP).usable_now


def test_alpha_over_five_million_sp_cannot_train(db):
    cid = _character(db, {}, total_sp=6_000_000)
    req = skills.plan(db, skills.load_character_skills(db, cid), FRIGATE)
    assert req.minutes_as_alpha is None and req.needs_omega
    assert "alpha_sp_limit" in {r.code for r in req.reasons}


def test_find_type_suggests_close_names(db):
    assert skills.find_type(db, "big barge") == BARGE_SHIP
    with pytest.raises(skills.SkillsError, match="Big Barge"):
        skills.find_type(db, "Barg")


def test_ship_goal_combines_skills_and_isk(db):
    cid = _character(db, {MINING: (4, 4, 45255)}, wallet=10_000_000.0)
    db.execute("INSERT INTO market_prices VALUES (?, 25000000, 20000000, 'now')", (BARGE_SHIP,))
    goal_id = goals.add_goal(db, "fly_ship", cid, type_id=BARGE_SHIP)
    (goal,) = goals.active_goals(db)
    status = goals.evaluate(db, goal)
    assert goal.goal_id == goal_id and goal.label == "Fly a Big Barge"
    assert status.isk_needed == 15_000_000
    assert status.requirement.needs_omega
    assert "clone_state_unknown" in {r.code for r in status.reasons}


def test_isk_goal_without_character_sums_wallets(db):
    _character(db, {}, wallet=1_000.0, character_id=7)
    _character(db, {}, wallet=2_000.0, character_id=8)
    goals.add_goal(db, "isk", None, isk=10_000)
    status = goals.evaluate(db, goals.active_goals(db)[0])
    assert status.wallet == 3_000 and status.isk_needed == 7_000


def test_goal_rules(db):
    with pytest.raises(goals.GoalError, match="particular character"):
        goals.add_goal(db, "fly_ship", None, type_id=BARGE_SHIP)
    with pytest.raises(goals.GoalError, match="positive"):
        goals.add_goal(db, "isk", None, isk=0)
    goal_id = goals.add_goal(db, "isk", None, isk=5)
    goals.set_status(db, goal_id, "dropped")
    assert goals.active_goals(db) == []


@respx.mock
def test_prices_refresh_replaces_old_rows(db):
    respx.get("https://esi.test/markets/prices").mock(
        return_value=httpx.Response(
            200, json=[{"type_id": BARGE_SHIP, "average_price": 5.0, "adjusted_price": 4.0}]
        )
    )
    db.execute("INSERT INTO market_prices VALUES (999, 1, 1, 'old')")
    with httpx.Client() as http:
        assert goals.update_prices(db, EsiClient(http, db, base_url="https://esi.test")) == 1
    assert goals.price_of(db, BARGE_SHIP) == 5.0
    assert goals.price_of(db, 999) is None
