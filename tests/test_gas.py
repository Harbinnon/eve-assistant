"""Gas harvesting (roadmap 5.1), on test_mining's fake universe plus a gas scoop.

The frigate gets +100% gas yield (role) and -5% cycle time a level of Mining
Frigate (trained to 2); a scoop takes 10 m3 every 30 s. So each active scoop
gives 10 x 2 x 60 / (30 x 0.9) = 44.4 m3 a minute.
"""

import json

import pytest

from eve_assistant import corrections, gas, planner, sessions, skills, snapshots
from eve_assistant.activities import gas as gas_activity
from tests.test_mining import FRIGATE, make_db

SCOOP, GAS_SKILL, AMBER, GOLDEN, COMPRESSED = 600, 5, 700, 701, 702
PER_SCOOP = 10 * 2 * 60 / (30 * 0.9)


@pytest.fixture
def db():
    conn = make_db()
    conn.executemany(
        "INSERT INTO sde_dogma_attributes VALUES (?, ?)",
        [(3239, "shipRoleBonusGasHarvestingYield"),
         (3237, "miningFrigateBonusGasCloudHarvestingDuration"), (102, "turretSlotsLeft")],
    )  # fmt: skip
    conn.executemany(
        "INSERT INTO sde_groups VALUES (?, 1, ?, 1)",
        [(737, "Gas Cloud Scoops"), (711, "Harvestable Cloud")],
    )
    conn.executemany(
        "INSERT INTO sde_types (type_id, name, group_id, published, volume) VALUES (?, ?, ?, 1, ?)",
        [(SCOOP, "Test Gas Scoop", 737, 5), (GAS_SKILL, "Gas Cloud Harvesting", 1, 0.01),
         (AMBER, "Amber Mykoserocin", 711, 10), (GOLDEN, "Golden Mykoserocin", 711, 10),
         (COMPRESSED, "Compressed Amber Mykoserocin", 711, 1)],
    )  # fmt: skip
    conn.executemany(
        "INSERT INTO sde_type_attributes VALUES (?, ?, ?)",
        [(FRIGATE, 3239, 100.0), (FRIGATE, 3237, -5.0), (FRIGATE, 102, 2.0),
         (SCOOP, 73, 30000.0), (SCOOP, 77, 10.0), (SCOOP, 182, GAS_SKILL), (SCOOP, 277, 1),
         (GAS_SKILL, 275, 1.0), (GAS_SKILL, 180, 167), (GAS_SKILL, 181, 168)],
    )  # fmt: skip
    conn.executemany(
        "INSERT INTO market_prices VALUES (?, ?, NULL, 'x')",
        [(AMBER, 10_000.0), (GOLDEN, 30_000.0), (COMPRESSED, 999_999.0)],
    )
    _gas_skill(conn, 1)
    conn.commit()
    yield conn
    conn.close()


def _gas_skill(db, level):
    current = snapshots.latest(db, 7, "skills")
    others = [s for s in current["skills"] if s["skill_id"] != GAS_SKILL]
    snapshots.record(db, 7, "skills", {**current, "skills": [*others, {
        "skill_id": GAS_SKILL, "trained_skill_level": level, "active_skill_level": level,
        "skillpoints_in_skill": 0}]})  # fmt: skip


def test_each_level_of_the_skill_allows_one_more_scoop(db):
    character = skills.load_character_skills(db, 7)
    one = gas.estimate(db, character, FRIGATE, [SCOOP, SCOOP])
    assert one.harvesters == 1
    assert one.m3_per_minute == pytest.approx(PER_SCOOP)
    assert "gas_skill_limit" in {r.code for r in one.reasons}
    _gas_skill(db, 3)
    three = gas.estimate(db, skills.load_character_skills(db, 7), FRIGATE, [SCOOP, SCOOP, SCOOP])
    assert three.harvesters == 2  # the frigate has two turret slots
    assert three.m3_per_minute == pytest.approx(2 * PER_SCOOP)
    assert three.hold_m3 == 5000.0
    assert gas.estimate(db, character, FRIGATE, []).reasons[-1].code == "no_gas_modules"


def test_gas_is_valued_at_the_average_highsec_booster_gas(db):
    assert gas.isk_per_m3(db) == pytest.approx((1_000 + 3_000) / 2)  # compressed is left out


def _fit(db, *modules):
    snapshots.record(db, 7, "ship", {"ship_type_id": FRIGATE, "ship_item_id": 9001})
    snapshots.record(db, 7, "location", {"solar_system_id": 30000142})
    snapshots.record(db, 7, "assets", [
        {"type_id": m, "location_id": 9001, "location_flag": f"HiSlot{n}", "quantity": 1}
        for n, m in enumerate(modules)
    ])  # fmt: skip


def test_a_gas_session_in_a_ship_with_scoops(db):
    from eve_assistant import fleet

    _fit(db, SCOOP)
    notes = []
    (item,) = gas_activity.gas_candidates(db, 7, fleet.owned_ships(db, 7), 60, 1e9, notes)
    assert item.kind == "gas"
    assert item.inputs["gas_minutes"] == pytest.approx(60 - 20 - 3)  # search, then one unload
    assert item.inputs["m3_per_minute"] == pytest.approx(PER_SCOOP)
    tax = item.reasons[3].detail["tax"]
    assert item.isk == pytest.approx(PER_SCOOP * 37 * 2_000 * (1 - tax))
    assert {r.code for r in item.reasons} >= {"gas_estimate", "gas_site_search", "gas_valued_as"}


def test_no_scoops_says_how_to_get_gas_into_plans(db):
    from eve_assistant import fleet

    _fit(db)
    notes = []
    assert gas_activity.gas_candidates(db, 7, fleet.owned_ships(db, 7), 60, 1e9, notes) == []
    assert [n.code for n in notes] == ["gas_needs_scoops"]


def test_gas_speed_is_learned_from_the_ledger(db):
    plan = planner.Plan(
        character_id=7, minutes_available=60, items=(), alternatives=(), notes=(), net_worth=0.0
    )
    plan_id = planner.record(db, plan, {}).plan_id
    details = {"inputs": {"gas_minutes": 30.0, "m3_per_minute": 40.0}}
    item_id = db.execute(
        "INSERT INTO plan_items (plan_id, position, kind, key, title, isk, minutes, risk_rating, "
        "expected_loss, details) VALUES (?, 0, 'gas', 'g', 'Gas', 1, 60, 'green', 0, ?)",
        (plan_id, json.dumps(details)),
    ).lastrowid
    session = sessions.start(db, 7, plan_id, now="2026-10-17T10:00:00+00:00")
    sessions.finish(db, session.session_id, now="2026-10-17T11:00:00+00:00")
    outcome = {
        "plan": {"items": [{"item_id": item_id, "kind": "gas", "response": None}]},
        "mined": [{"type_id": AMBER, "units": 90, "m3": 900.0}],  # 30 m3 a minute
    }
    db.execute(
        "INSERT INTO session_outcomes (session_id, computed_at, settled, content_hash, details) "
        "VALUES (?, 'x', 1, 'h', ?)",
        (session.session_id, json.dumps(outcome)),
    )
    (seen,) = corrections.gas_observations(db, 7)
    assert seen.ratio == pytest.approx(30 / 40)
    assert corrections.correction(db, 7, "gas_rate").factor == pytest.approx(1 - 0.25 / 4)
