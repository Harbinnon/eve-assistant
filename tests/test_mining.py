"""Mining yield estimates on hand-made static data.

Fake universe: a frigate with a x2 role multiplier and +5% per Mining
Frigate level; a laser doing 10 m3 per 15 s with 1% crit chance for +200%;
an upgrade giving +9%; Mining and Astrogeology give +5% per level.
"""

import pytest

from eve_assistant import mining, skills, snapshots, store

FRIGATE, LASER, UPGRADE, LOCKED_LASER, IMPLANT = 100, 200, 300, 400, 500
MINING, ASTRO, FRIG_SKILL, ADV = 1, 2, 3, 4
ATTRS = {
    73: "duration",
    77: "miningAmount",
    207: "miningAmountMultiplier",
    434: "miningAmountBonus",
    1842: "miningFrigatesBonusOreMiningYield",
    1556: "generalMiningHoldCapacity",
    5967: "miningCritChance",
    5969: "miningCritBonusYield",
    182: "requiredSkill1",
    277: "requiredSkill1Level",
}


@pytest.fixture
def db():
    conn = store.connect(":memory:")
    conn.executemany("INSERT INTO sde_dogma_attributes VALUES (?, ?)", ATTRS.items())
    conn.executemany(
        "INSERT INTO sde_groups VALUES (?, 1, ?, 1)", [(54, "Mining Laser"), (1, "Other")]
    )
    conn.executemany(
        "INSERT INTO sde_types (type_id, name, group_id, published, capacity) "
        "VALUES (?, ?, ?, 1, 0)",
        [
            (FRIGATE, "Test Frigate", 1),
            (LASER, "Test Miner", 54),
            (LOCKED_LASER, "Fancy Miner", 54),
            (UPGRADE, "Test Upgrade", 1),
            (IMPLANT, "Test Implant", 1),
            (MINING, "Mining", 1),
            (ASTRO, "Astrogeology", 1),
            (FRIG_SKILL, "Mining Frigate", 1),
            (ADV, "Advanced Mining", 1),
        ],
    )
    attrs = [
        (FRIGATE, 207, 2.0), (FRIGATE, 1842, 5.0), (FRIGATE, 1556, 5000.0),
        (LASER, 73, 15000.0), (LASER, 77, 10.0), (LASER, 5967, 0.01), (LASER, 5969, 2.0),
        (LOCKED_LASER, 73, 15000.0), (LOCKED_LASER, 77, 50.0),
        (LOCKED_LASER, 182, ADV), (LOCKED_LASER, 277, 1),
        (UPGRADE, 434, 9.0), (IMPLANT, 434, 5.0),
        (MINING, 434, 5.0), (ASTRO, 434, 5.0),
    ]  # fmt: skip
    for skill in (MINING, ASTRO, FRIG_SKILL, ADV):
        attrs += [(skill, 275, 1.0), (skill, 180, 167), (skill, 181, 168)]
    conn.executemany("INSERT INTO sde_type_attributes VALUES (?, ?, ?)", attrs)
    snapshots.ensure_character(conn, 7, "Miner")
    snapshots.record(conn, 7, "public", {"race_id": 1})
    names = ("charisma", "intelligence", "memory", "perception", "willpower")
    snapshots.record(conn, 7, "attributes", {name: 20 for name in names})
    snapshots.record(
        conn,
        7,
        "skills",
        {"total_sp": 1, "skills": [
            {"skill_id": MINING, "trained_skill_level": 4, "active_skill_level": 4,
             "skillpoints_in_skill": 0},
            {"skill_id": FRIG_SKILL, "trained_skill_level": 2, "active_skill_level": 2,
             "skillpoints_in_skill": 0}]},
    )  # fmt: skip
    conn.commit()
    yield conn
    conn.close()


def test_yield_combines_ship_skills_upgrades_implants_and_crits(db):
    character = skills.load_character_skills(db, 7)
    result = mining.estimate(db, character, FRIGATE, [LASER, LASER, UPGRADE], [IMPLANT])
    factor = 2.0 * 1.20 * 1.0 * 1.10 * 1.09 * 1.05  # ship, Mining 4, Astro 0, frigate 2, ...
    per_cycle = 10.0 * factor * (1 + 0.01 * 2.0)
    assert [m.m3_per_cycle for m in result.modules] == [pytest.approx(per_cycle)] * 2
    assert result.modules[0].cycle_seconds == 15.0
    assert result.m3_per_minute == pytest.approx(2 * per_cycle * 4)
    assert result.hold_m3 == 5000.0
    assert result.minutes_to_fill_hold == pytest.approx(5000.0 / result.m3_per_minute)
    assert result.multipliers["Mining Frigate 2"] == pytest.approx(1.10)


def test_modules_the_character_cannot_use_are_left_out(db):
    character = skills.load_character_skills(db, 7)
    result = mining.estimate(db, character, FRIGATE, [LASER, LOCKED_LASER])
    assert [m.name for m in result.modules] == ["Test Miner"]
    reason = next(r for r in result.reasons if r.code == "modules_not_usable")
    assert reason.detail["modules"] == ["Fancy Miner"]


def test_no_mining_modules_is_reported(db):
    result = mining.estimate(db, skills.load_character_skills(db, 7), FRIGATE, [UPGRADE])
    assert result.m3_per_minute == 0
    assert "no_mining_modules" in {r.code for r in result.reasons}


def test_current_fit_reads_slots_from_assets(db):
    snapshots.record(db, 7, "ship", {"ship_type_id": FRIGATE, "ship_item_id": 9001})
    snapshots.record(
        db,
        7,
        "assets",
        [
            {"type_id": LASER, "location_id": 9001, "location_flag": "HiSlot0", "quantity": 1},
            {"type_id": LASER, "location_id": 9001, "location_flag": "HiSlot1", "quantity": 1},
            {"type_id": LASER, "location_id": 9001, "location_flag": "Cargo", "quantity": 5},
            {"type_id": UPGRADE, "location_id": 9001, "location_flag": "LoSlot0", "quantity": 1},
            {"type_id": LASER, "location_id": 1234, "location_flag": "Hangar", "quantity": 1},
        ],
    )
    snapshots.record(db, 7, "implants", [IMPLANT])
    ship, fitted, implants = mining.current_fit(db, 7)
    assert ship == FRIGATE
    assert sorted(fitted) == [LASER, LASER, UPGRADE]
    assert implants == [IMPLANT]
    assert len(mining.estimate_current(db, 7).modules) == 2
