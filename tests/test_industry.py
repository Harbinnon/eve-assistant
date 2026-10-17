"""Manufacturing from your blueprints (roadmap 5.4)."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from eve_assistant import explain, industry, skills, snapshots, store
from eve_assistant.activities import manufacturing

SYSTEM, STATION = 30000142, 60003760
BP, SHIP, TRIT, PYE, INDUSTRY, MASS = 32881, 32880, 34, 35, 3380, 3387


@pytest.fixture
def db():
    conn = store.connect(":memory:")
    conn.executemany("INSERT INTO sde_dogma_attributes VALUES (?, ?)",
                     [(440, "manufacturingTimeBonus"), (275, "skillTimeConstant")])  # fmt: skip
    conn.executemany(
        "INSERT INTO sde_types (type_id, name, group_id, published) VALUES (?, ?, 1, 1)",
        [(BP, "Venture Blueprint"), (SHIP, "Venture"), (TRIT, "Tritanium"), (PYE, "Pyerite"),
         (INDUSTRY, "Industry"), (MASS, "Mass Production")],
    )  # fmt: skip
    conn.execute("INSERT INTO sde_type_attributes VALUES (?, 440, -4.0)", (INDUSTRY,))
    conn.execute("INSERT INTO sde_blueprints VALUES (?, ?, 1, 6000, 30)", (BP, SHIP))
    conn.executemany("INSERT INTO sde_blueprint_materials VALUES (?, ?, ?)",
                     [(BP, TRIT, 1000), (BP, PYE, 333)])  # fmt: skip
    conn.execute("INSERT INTO sde_blueprint_skills VALUES (?, ?, 1)", (BP, INDUSTRY))
    conn.executemany(
        "INSERT INTO market_prices VALUES (?, ?, ?, 'x')",
        [(SHIP, 300_000.0, 250_000.0), (TRIT, 4.0, 3.0), (PYE, 20.0, 15.0)],
    )
    conn.execute("INSERT INTO industry_cost_indices VALUES (?, 0.05, 'x')", (SYSTEM,))
    snapshots.ensure_character(conn, 7, "Builder")
    snapshots.record(conn, 7, "public", {"race_id": 1})
    snapshots.record(conn, 7, "attributes", {n: 20 for n in skills.ATTRIBUTE_NAMES.values()})
    snapshots.record(conn, 7, "skills", {"total_sp": 1, "skills": [
        {"skill_id": INDUSTRY, "trained_skill_level": 5, "active_skill_level": 5,
         "skillpoints_in_skill": 0}]})  # fmt: skip
    conn.execute("INSERT INTO sde_clone_grade_skills VALUES (1, 'Alpha', ?, 5)", (INDUSTRY,))
    snapshots.record(conn, 7, "location", {"solar_system_id": SYSTEM, "station_id": STATION})
    snapshots.record(
        conn,
        7,
        "assets",
        [{"type_id": TRIT, "location_id": STATION, "location_flag": "Hangar", "quantity": 5000}],
    )
    snapshots.record(conn, 7, "blueprints", [
        {"item_id": 1, "type_id": BP, "location_id": STATION, "location_flag": "Hangar",
         "material_efficiency": 10, "time_efficiency": 20, "quantity": -2, "runs": 4}])  # fmt: skip
    conn.commit()
    yield conn
    conn.close()


def test_a_build_counts_me_te_skills_and_the_job_fee(db):
    character = skills.load_character_skills(db, 7)
    (blueprint,) = snapshots.latest(db, 7, "blueprints")
    build = industry.plan_build(db, character, blueprint, SYSTEM, {TRIT: 5000}, tax_rate=0.075)
    assert build.runs == 4  # the copy's runs, all within a day
    assert build.seconds == pytest.approx(4 * 6000 * 0.8 * 0.8)  # TE 20, Industry 5
    trit, pye = build.materials
    assert (trit.quantity, trit.in_hangar) == (3600, 5000)  # 1000 x 4 x 0.9
    assert pye.quantity == 1199  # ceil(333 x 4 x 0.9)
    estimated_value = (1000 * 3.0 + 333 * 15.0) * 4  # base materials at adjusted prices
    assert build.job_fee == pytest.approx(estimated_value * (0.05 + 0.0025 + 0.04 + 0.0025))
    assert build.materials_cost == pytest.approx(3600 * 4.0 + 1199 * 20.0)
    assert build.value == pytest.approx(4 * 300_000 * (1 - 0.075))
    assert [m.name for m in build.to_buy] == ["Pyerite"]
    assert build.is_copy


def test_no_build_without_the_skill_or_the_cost_index(db):
    character = skills.load_character_skills(db, 7)
    (blueprint,) = snapshots.latest(db, 7, "blueprints")
    assert industry.plan_build(db, character, blueprint, 30000001, {}, 0.075) is None
    db.execute("UPDATE sde_blueprint_skills SET level = 6")
    assert industry.plan_build(db, character, blueprint, SYSTEM, {}, 0.075) is None


def test_cost_indices_come_from_esi(db):
    esi = SimpleNamespace(get=lambda path: SimpleNamespace(data=[
        {"solar_system_id": SYSTEM, "cost_indices": [
            {"activity": "manufacturing", "cost_index": 0.0712},
            {"activity": "copying", "cost_index": 0.01}]}]))  # fmt: skip
    now = datetime(2026, 10, 17, 18, tzinfo=UTC)
    assert industry.due(db, now)
    industry.refresh(db, esi, now)
    assert industry.cost_index(db, SYSTEM) == pytest.approx(0.0712)
    assert not industry.due(db, now)


def test_the_planner_gets_a_quick_build_job(db):
    notes = []
    (item,) = manufacturing.manufacturing_candidates(db, 7, 1e9, notes)
    assert item.kind == "manufacturing"
    assert item.minutes == manufacturing.MINUTES_TO_START + manufacturing.MINUTES_TO_BUY
    assert item.title.startswith("Build 4 Venture from your copy (ME 10)")
    assert item.inputs["runs"] == 4
    assert item.risk.exposure == 0.0
    costs = item.reasons[0]
    text = explain.describe(costs.code, costs.detail)
    assert text.startswith("Materials 38.4k ISK and job fee")
    assert (
        "on what it costs. Prices are CCP averages, not hub quotes; to buy first: Pyerite." in text
    )
    snapshots.record(db, 7, "industry_jobs", [{"status": "active"}])  # 1 slot, now busy
    assert manufacturing.manufacturing_candidates(db, 7, 1e9, notes) == []
    assert notes[-1].code == "job_slots_full"
