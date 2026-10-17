"""The fleet: owned ships, what each can do, and the cost of getting into it."""

import pytest

from eve_assistant import fleet, ore, snapshots, store
from eve_assistant.activities import hauling

HOME, AWAY = 31000001, 31000002
STATION, AWAY_STATION = 61000001, 61000002
VENTURE, HAULER, ORE_HAULER, BARGE, LASER = 100, 101, 102, 103, 200
SKILL = 3000


@pytest.fixture
def db():
    conn = store.connect(":memory:")
    conn.executemany(
        "INSERT INTO sde_solar_systems VALUES (?, ?, 1, 1, 0.9, 'B')",
        [(HOME, "Home"), (AWAY, "Away")],
    )
    conn.executemany("INSERT INTO sde_jumps VALUES (?, ?)", [(HOME, AWAY), (AWAY, HOME)])
    conn.executemany(
        "INSERT INTO sde_npc_stations VALUES (?, ?, 1, 1, 0.5, 0.05)",
        [(STATION, HOME), (AWAY_STATION, AWAY)],
    )
    conn.executemany(
        "INSERT INTO sde_groups VALUES (?, ?, ?, 1)", [(1, 6, "Ships"), (54, 7, "Mining Laser")]
    )
    conn.executemany(
        "INSERT INTO sde_types (type_id, name, group_id, published, capacity) "
        "VALUES (?, ?, ?, 1, ?)",
        [
            (VENTURE, "Venture", 1, 50.0),
            (HAULER, "Hauler", 1, 6000.0),
            (ORE_HAULER, "Ore Hauler", 1, 500.0),
            (BARGE, "Barge", 1, 300.0),
            (LASER, "Miner", 54, 0.0),
            (SKILL, "Barge Skill", 1, 0.0),
        ],
    )
    conn.executemany(
        "INSERT INTO sde_dogma_attributes VALUES (?, ?)",
        [
            (1556, "generalMiningHoldCapacity"),
            (182, "requiredSkill1"),
            (277, "requiredSkill1Level"),
        ],
    )
    conn.executemany(
        "INSERT INTO sde_type_attributes VALUES (?, ?, ?)",
        [
            (ORE_HAULER, 1556, 40000.0),
            (VENTURE, 1556, 5000.0),
            (BARGE, 182, SKILL),
            (BARGE, 277, 1),
            (SKILL, 275, 1.0),
        ],
    )
    conn.executemany(
        "INSERT INTO market_prices VALUES (?, ?, ?, 'now')",
        [(VENTURE, 1e6, 1e6), (HAULER, 5e6, 5e6), (ORE_HAULER, 2e6, 2e6), (LASER, 1e5, 1e5)],
    )
    snapshots.ensure_character(conn, 7, "Pilot")
    snapshots.record(conn, 7, "public", {"race_id": 1})
    names = ("charisma", "intelligence", "memory", "perception", "willpower")
    snapshots.record(conn, 7, "attributes", {name: 20 for name in names})
    snapshots.record(conn, 7, "skills", {"total_sp": 1, "skills": []})
    snapshots.record(conn, 7, "location", {"solar_system_id": HOME, "station_id": STATION})
    snapshots.record(conn, 7, "ship", {"ship_type_id": VENTURE, "ship_item_id": 1})
    snapshots.record(
        conn,
        7,
        "assets",
        [
            {"item_id": 1, "type_id": VENTURE, "location_id": STATION, "location_flag": "Hangar",
             "is_singleton": True, "quantity": 1},
            {"item_id": 11, "type_id": LASER, "location_id": 1, "location_flag": "HiSlot0",
             "is_singleton": True, "quantity": 1},
            {"item_id": 2, "type_id": HAULER, "location_id": STATION, "location_flag": "Hangar",
             "is_singleton": True, "quantity": 1},
            {"item_id": 3, "type_id": ORE_HAULER, "location_id": AWAY_STATION,
             "location_flag": "Hangar", "is_singleton": True, "quantity": 1},
            {"item_id": 4, "type_id": BARGE, "location_id": STATION, "location_flag": "Hangar",
             "is_singleton": True, "quantity": 1},
        ],
    )  # fmt: skip
    conn.commit()
    yield conn
    conn.close()


def test_owned_ships_read_holds_fits_values_and_skills(db):
    ships = {s.name: s for s in fleet.owned_ships(db, 7)}
    assert set(ships) == {"Venture", "Hauler", "Ore Hauler", "Barge"}
    assert ships["Venture"].is_active
    assert ships["Venture"].fitted == (LASER,)
    assert ships["Venture"].value == pytest.approx(1.1e6)  # hull plus fitted laser
    assert ships["Ore Hauler"].capacity_for(is_ore=True) == 40500.0
    assert ships["Ore Hauler"].capacity_for(is_ore=False) == 500.0
    assert ships["Ore Hauler"].system_id == AWAY
    assert not ships["Barge"].usable  # needs a skill the pilot lacks


def test_boarding_costs(db):
    ships = {s.name: s for s in fleet.owned_ships(db, 7)}
    assert fleet.boarding(db, 7, ships["Venture"]).minutes == 0
    assert fleet.boarding(db, 7, ships["Hauler"]).minutes == fleet.MINUTES_TO_SWITCH_SHIP
    away = fleet.boarding(db, 7, ships["Ore Hauler"])
    assert away.jumps == 1
    assert away.minutes > fleet.MINUTES_TO_SWITCH_SHIP
    assert fleet.boarding(db, 7, ships["Barge"]) is None


def test_hauler_choice_prefers_the_cheapest_ship_that_fits(db):
    ships = fleet.owned_ships(db, 7)

    def option(m3, is_ore=True):
        return ore.Option(
            key="k", title="t", isk=1.0, active_minutes=1.0, instant=True, location="x",
            jumps=5, hauled_m3=m3, hauled_is_ore=is_ore,
        )  # fmt: skip

    small = hauling.pick_hauler(ships, STATION, STATION, option(4000))
    assert small[0].name == "Venture"  # ore hold fits, already boarded
    assert small[1] == 0
    minerals = hauling.pick_hauler(ships, STATION, STATION, option(3000, is_ore=False))
    assert minerals[0].name == "Hauler"
    assert minerals[1] == fleet.MINUTES_TO_SWITCH_SHIP
    big_ore = hauling.pick_hauler(ships, AWAY_STATION, STATION, option(30000))
    assert big_ore[0].name == "Ore Hauler"  # waiting at the ore's station
    assert hauling.pick_hauler(ships, STATION, STATION, option(99999)) is None


def test_ships_in_known_structures_get_a_system(db):
    from eve_assistant import structures

    db.execute(
        "INSERT INTO structures VALUES (1000000000001, 1, 'Somebody''s Citadel', ?, 35832, 1, "
        "'2026-10-16T00:00:00+00:00', 7)",
        (AWAY,),
    )
    db.execute(
        "INSERT INTO structures VALUES (1000000000002, 0, NULL, NULL, NULL, NULL, "
        "'2026-10-16T00:00:00+00:00', 7)"
    )
    assert structures.system_of(db, 1000000000001) == AWAY
    assert structures.system_of(db, 1000000000002) is None  # no docking access
    assert structures.system_of(db, AWAY_STATION) == AWAY
    assets = snapshots.latest(db, 7, "assets")
    assets.append(
        {"item_id": 9, "type_id": HAULER, "location_id": 1000000000001, "location_type": "item",
         "location_flag": "Hangar", "is_singleton": True, "quantity": 1}
    )  # fmt: skip
    snapshots.record(db, 7, "assets", assets)
    in_citadel = [s for s in fleet.owned_ships(db, 7) if s.item_id == 9][0]
    assert in_citadel.system_id == AWAY
    assert fleet.boarding(db, 7, in_citadel).jumps == 1


def test_loads_too_big_for_one_trip_pick_the_fewest_trips(db):
    ships = fleet.owned_ships(db, 7)
    load = ore.Option(
        key="k", title="t", isk=1.0, active_minutes=1.0, instant=True, location="x",
        jumps=5, hauled_m3=12_000, hauled_is_ore=True,
    )  # fmt: skip
    ship, switch, trips = hauling.multi_trip_haul(ships, STATION, STATION, load)
    # the Hauler's 6,000 m3 cargo takes ore too: 2 trips beats the Venture's 3
    assert ship.name == "Hauler"
    assert trips == 2
    assert switch == fleet.MINUTES_TO_SWITCH_SHIP
    ship, _switch, trips = hauling.multi_trip_haul(ships, AWAY_STATION, STATION, load)
    assert ship.name == "Ore Hauler"
    assert trips == 1
