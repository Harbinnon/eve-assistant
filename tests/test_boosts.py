"""Mining boosts (roadmap 8.5): a Mining Foreman Burst's strength from the
static data, the boosted miner, and "one mines, one boosts" when playing
together. The world is test_together's, with the hauler (8) made a booster:
a Burst II with charges loaded, an Industrial Core II and an ore compressor,
Mining Director 4 and Industrial Command Ships 2."""

import json

import httpx
import pytest
import respx

from eve_assistant import boosts, explain, planner, snapshots, together
from eve_assistant.esi.client import EsiClient
from tests.test_together import BASE, HAULER, ORE, _mock_esi, _world

BURST, CHARGE, CORE, COMPRESSOR, DIRECTOR, COMMAND, COMPRESSED, MINDLINK, SHIELD = range(500, 509)
ATTRIBUTES = {
    "warfareBuff1ID": 2468,
    "warfareBuff1Multiplier": 2596,
    "warfareBuff1Value": 2469,
    "commandStrengthBonus": 1302,
    "shipBonusICS2": 2001,
    "mindlinkBonus": 2002,
    "industrialCoreBonusMiningBurstStrength": 2003,
}


def _booster_world(wallet=50_000_000.0, charges=10, mindlink=False):
    db = _world()
    db.executemany(
        "INSERT INTO sde_groups VALUES (?, ?, ?, 1)",
        [(70, 7, "Command Burst"), (71, 8, "Mining Foreman Burst Charges"),
         (72, 7, "Industrial Core"), (73, 7, "Ore Compressor"), (74, 16, "Leadership"),
         (75, 25, "Compressed Veldspar"), (76, 20, "Cyber Leadership")],
    )  # fmt: skip
    db.executemany(
        "INSERT INTO sde_types (type_id, name, group_id, published, volume) VALUES (?, ?, ?, 1, ?)",
        [(BURST, "Mining Foreman Burst II", 70, 5), (CHARGE, "Mining Laser Optimization Charge",
         71, 1), (CORE, "Medium Industrial Core II", 72, 5),
         (COMPRESSOR, "Medium Asteroid Ore Compressor I", 73, 5), (DIRECTOR, "Mining Director",
         74, 0.01), (COMMAND, "Industrial Command Ships", 74, 0.01),
         (COMPRESSED, "Compressed Veldspar", 75, 0.001), (MINDLINK, "Mining Foreman Mindlink",
         76, 1), (SHIELD, "Shield Command Burst II", 70, 5)],
    )  # fmt: skip
    db.executemany(
        "INSERT INTO sde_dogma_attributes VALUES (?, ?)", [(i, n) for n, i in ATTRIBUTES.items()]
    )
    db.executemany(
        "INSERT INTO sde_type_attributes VALUES (?, ?, ?)",
        [(BURST, ATTRIBUTES["warfareBuff1Value"], 1.25), (BURST, 73, 60_000.0),
         (SHIELD, ATTRIBUTES["warfareBuff1Value"], 9.0),  # stronger, but not for mining
         (CHARGE, ATTRIBUTES["warfareBuff1ID"], 24), (CHARGE, ATTRIBUTES["warfareBuff1Multiplier"],
         -15.0), (DIRECTOR, ATTRIBUTES["commandStrengthBonus"], 10.0),
         (HAULER, ATTRIBUTES["shipBonusICS2"], 2.0),
         (CORE, ATTRIBUTES["industrialCoreBonusMiningBurstStrength"], 7.5),
         (MINDLINK, ATTRIBUTES["mindlinkBonus"], 25.0)],
    )  # fmt: skip
    db.execute("INSERT INTO sde_compressible_types VALUES (?, ?)", (ORE, COMPRESSED))
    db.execute("INSERT INTO market_prices VALUES (?, 1000, 1000, 'now')", (CHARGE,))
    fitted = [{"type_id": BURST, "location_id": 9002, "location_flag": "HiSlot0", "quantity": 1},
              {"type_id": CORE, "location_id": 9002, "location_flag": "HiSlot1", "quantity": 1},
              {"type_id": SHIELD, "location_id": 9002, "location_flag": "HiSlot3", "quantity": 1},
              {"type_id": COMPRESSOR, "location_id": 9002, "location_flag": "HiSlot2",
               "quantity": 1}]  # fmt: skip
    if charges:
        fitted.append({"type_id": CHARGE, "location_id": 9002, "location_flag": "HiSlot0",
                       "quantity": charges})  # fmt: skip
    snapshots.record(db, 8, "assets", fitted)
    snapshots.record(db, 8, "skills", {"total_sp": 1, "skills": [
        {"skill_id": DIRECTOR, "trained_skill_level": 4, "active_skill_level": 4,
         "skillpoints_in_skill": 1},
        {"skill_id": COMMAND, "trained_skill_level": 2, "active_skill_level": 2,
         "skillpoints_in_skill": 1}]})  # fmt: skip
    snapshots.record(db, 8, "wallet", wallet)
    snapshots.record(db, 8, "implants", [MINDLINK] if mindlink else [])
    db.commit()
    return db


STRENGTH = 1.25 * 1.4 * 1.04 * 1.075  # Burst II, Director 4, ICS 2 at 2%, Core II


def test_the_boost_strength_comes_from_the_static_data():
    db = _booster_world()
    boost = boosts.best_boost(db, 8)
    assert boost.burst == "Mining Foreman Burst II"
    assert boost.charge == "Mining Laser Optimization Charge"
    assert boost.strength == pytest.approx(STRENGTH)
    assert boost.cycle_change == pytest.approx(-15 * STRENGTH)  # -29.3%
    assert [what for what, _value in boost.factors] == [
        "Mining Foreman Burst II",
        "Mining Director 4",
        "Test Hauler, Industrial Command Ships 2",
        "Medium Industrial Core II (running)",
    ]
    assert boost.charges_loaded == 10
    assert boost.charges_needed(60) == 60  # one a one-minute cycle


def test_a_mindlink_strengthens_it():
    db = _booster_world(mindlink=True)
    assert boosts.best_boost(db, 8).strength == pytest.approx(STRENGTH * 1.25)


def test_no_burst_no_boost():
    db = _world()
    assert boosts.best_boost(db, 8) is None


def _team(db, **options):
    with httpx.Client() as http:
        return together.build(db, EsiClient(http, db, base_url=BASE), [7, 8], 60, **options)


@respx.mock
def test_one_mining_while_the_other_boosts_wins_when_it_makes_most():
    _mock_esi()
    db = _booster_world()
    team = _team(db)
    assert team.chosen.kind == "mine_and_boost"
    assert team.chosen.roles == ("miner", "booster")
    mined, boosted = team.chosen.plans
    mining = next(i for i in mined.items if i.kind == "mining")
    assert "boosted by Hauler (cycles 29% shorter), passing each full hold to Hauler" in (
        mining.title
    )
    assert mining.inputs["cycle_factor"] == pytest.approx(1 - 0.15 * STRENGTH)
    step = boosted.items[-1]
    assert step.kind == "team_boost"
    assert step.isk == -60 * 1000.0  # the charges it uses
    assert step.title.startswith("Load 60 Mining Laser Optimization Charges first (10 loaded).")
    assert ", compress it and sell it" in step.title
    detail = step.reasons[0].detail
    assert detail["compressed"]
    assert detail["trips"] == 1  # compressed, it all fits
    assert {shape.kind for shape in team.others} == {"mine_and_haul", "side_by_side"}
    boosted_value = team.chosen.value
    hauled_value = next(s for s in team.others if s.kind == "mine_and_haul").value
    assert boosted_value > hauled_value


@respx.mock
def test_risky_boosting_is_left_out_unless_allowed():
    _mock_esi()
    db = _booster_world(wallet=0.0)
    careful = _team(db)
    assert careful.chosen.kind != "mine_and_boost"
    assert "team_boost_risky" in {n.code for n in careful.notes}
    assert _team(db, allow_risky=True).chosen.kind == "mine_and_boost"


@respx.mock
def test_without_a_burst_the_note_says_why():
    _mock_esi()
    db = _world()
    codes = {n.code for n in _team(db).notes}
    assert "no_team_booster" in codes


def test_the_reason_spells_out_the_strength():
    text = explain.describe(
        "team_boost",
        {"miner": "Harbinnon", "ship": "Porpoise", "charge": "Mining Laser Optimization Charge",
         "base": -15.0, "strength": STRENGTH, "cycle_change": -15 * STRENGTH,
         "factors": [["Mining Foreman Burst II", 1.25], ["Mining Director 4", 1.4]],
         "charges": 240, "loaded": 0, "charge_cost": 240_000.0, "priced": True,
         "compressed": True, "m3": 120.0, "trips": 1, "minutes_needed": 6.0},
    )  # fmt: skip
    assert "-15% × strength 1.96" in text
    assert "Mining Foreman Burst II 1.25 × Mining Director 4 1.4" in text
    assert "cycles 29.3% shorter for Harbinnon" in text
    assert "Uses 240 charges a session, about 240k ISK; 0 loaded now" in text
    assert "about 120 m³ compressed" in text


def test_plans_saved_before_boosts_still_load():
    db = _world()
    plan_id = planner.record(db, planner.Plan(7, 60, (), (), (), 0.0), {}).plan_id
    old = {"value": 0, "other": {"shape": "side_by_side", "value": 1.0, "members": []}}
    db.execute(
        "INSERT INTO team_plans (created_at, minutes_available, shape, details) "
        "VALUES ('2026-10-31T00:00:00+00:00', 60, 'mine_and_haul', ?)",
        (json.dumps(old),),
    )
    db.execute("INSERT INTO team_plan_members VALUES (1, ?, 'miner')", (plan_id,))
    assert [o["shape"] for o in together.load(db, 1).others] == ["side_by_side"]


def test_a_miner_whose_mining_is_risky_is_told_so():
    plan = planner.Plan(7, 60, (), (planner.Alternative(_risky_mining(), "red_risk"),), (), 0.0)
    assert together._why_no_mining(plan, "no_boost_mining") == "team_mining_risky"
    assert together._why_no_mining(planner.Plan(7, 60, (), (), (), 0.0), "x") == "x"
    text = explain.describe("team_mining_risky", {"miner": "Harbinnon", "booster": "Alex"})
    assert text.startswith("Harbinnon mining while Alex boosts wasn't offered: Harbinnon's")


def _risky_mining():
    from eve_assistant import risk
    from eve_assistant.activities.base import PlanItem

    return PlanItem("mining", "1:2", "Mine", 1.0, 60, risk.rate("high", 1e9, 1))
