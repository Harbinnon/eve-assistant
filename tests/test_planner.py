"""The planner, risk matrix and preferences on a small hand-made world.

World: one highsec system with an NPC station, a lowsec neighbour, a mining
frigate with one laser, Veldspar refining into Tritanium, and a character
docked at the station with Veldspar in the hangar.
"""

import sqlite3

import httpx
import pytest
import respx

from eve_assistant import planner, preferences, risk, sde, snapshots, store
from eve_assistant.esi.client import EsiClient

HOME, LOW = 31000001, 31000002
STATION = 61000001
REGION = 10000032
FRIGATE, LASER, ORE, MINERAL = 100, 200, 1230, 34
BASE = "https://esi.test"


def test_band_for_security():
    assert risk.band_for_security(0.9) == "low"
    assert risk.band_for_security(0.45) == "low"  # rounds to 0.5
    assert risk.band_for_security(0.3) == "medium"
    assert risk.band_for_security(-0.2) == "high"
    assert risk.band_for_security(None) == "high"
    assert risk.band_for_security(0.3, "Exordium") == "none"
    assert risk.rate("none", 10_000, 1).rating == "green"
    assert risk.rate("none", 10_000, 1).expected_loss == 0


@pytest.mark.parametrize(
    "band, share, rating",
    [
        ("very_low", 0.01, "green"),
        ("very_low", 0.15, "amber"),
        ("low", 0.05, "amber"),
        ("medium", 0.01, "amber"),
        ("medium", 0.20, "red"),
        ("high", 0.05, "red"),
        ("low", 0.5, "red"),
    ],
)
def test_matrix_cells(band, share, rating):
    result = risk.rate(band, share * 1_000_000, 1_000_000)
    assert result.rating == rating
    assert result.expected_loss == pytest.approx(risk.LOSS_CHANCE[band] * share * 1_000_000)


def test_zero_net_worth_counts_any_exposure_as_everything():
    assert risk.rate("low", 10, 0).share_of_net_worth == 1.0
    assert risk.rate("low", 0, 0).rating == "green"


@pytest.fixture
def db():
    conn = store.connect(":memory:")
    conn.execute("INSERT INTO sde_regions VALUES (?, 'Home Region')", (REGION,))
    conn.executemany(
        "INSERT INTO sde_solar_systems VALUES (?, ?, ?, 1, ?, 'B')",
        [(HOME, "Home", REGION, 0.9), (LOW, "Low", REGION, 0.3)],
    )
    conn.executemany("INSERT INTO sde_jumps VALUES (?, ?)", [(HOME, LOW), (LOW, HOME)])
    conn.execute(
        "INSERT INTO sde_npc_stations VALUES (?, ?, 1000001, 1, 0.5, 0.05)", (STATION, HOME)
    )
    conn.executemany(
        "INSERT INTO sde_groups VALUES (?, ?, ?, 1)",
        [(1, 6, "Frigate"), (54, 7, "Mining Laser"), (462, 25, "Veldspar"), (18, 4, "Mineral")],
    )
    conn.executemany(
        "INSERT INTO sde_types "
        "(type_id, name, group_id, published, volume, portion_size, capacity) "
        "VALUES (?, ?, ?, 1, ?, ?, ?)",
        [
            (FRIGATE, "Test Frigate", 1, 1000, 1, 50),
            (LASER, "Test Miner", 54, 5, 1, 0),
            (ORE, "Veldspar", 462, 0.1, 100, 0),
            (MINERAL, "Tritanium", 18, 0.01, 1, 0),
        ],
    )
    conn.executemany(
        "INSERT INTO sde_dogma_attributes VALUES (?, ?)",
        [(73, "duration"), (77, "miningAmount"), (1556, "generalMiningHoldCapacity")],
    )
    conn.executemany(
        "INSERT INTO sde_type_attributes VALUES (?, ?, ?)",
        [(LASER, 73, 60000.0), (LASER, 77, 100.0), (FRIGATE, 1556, 1000.0)],
    )
    conn.execute("INSERT INTO sde_type_materials VALUES (?, ?, 400)", (ORE, MINERAL))
    conn.execute("INSERT INTO sde_meta VALUES (1, 'x', 'y', ?)", (sde.LOADER_VERSION,))
    conn.executemany(
        "INSERT INTO market_prices VALUES (?, ?, ?, 'now')",
        [(FRIGATE, 1_000_000, 1_000_000), (LASER, 100_000, 100_000), (ORE, 10, 10)],
    )
    snapshots.ensure_character(conn, 7, "Miner")
    snapshots.record(conn, 7, "public", {"race_id": 1})
    names = ("charisma", "intelligence", "memory", "perception", "willpower")
    snapshots.record(conn, 7, "attributes", {name: 20 for name in names})
    snapshots.record(conn, 7, "skills", {"total_sp": 1, "skills": []})
    snapshots.record(conn, 7, "skillqueue", [])
    snapshots.record(conn, 7, "location", {"solar_system_id": HOME, "station_id": STATION})
    snapshots.record(conn, 7, "ship", {"ship_type_id": FRIGATE, "ship_item_id": 9001})
    snapshots.record(
        conn,
        7,
        "assets",
        [
            {"type_id": LASER, "location_id": 9001, "location_flag": "HiSlot0", "quantity": 1},
            {"type_id": ORE, "location_id": STATION, "location_flag": "Hangar", "quantity": 1000},
        ],
    )
    snapshots.record(conn, 7, "wallet", 50_000_000.0)
    snapshots.record(conn, 7, "standings", [])
    snapshots.record(
        conn, 7, "mining_ledger", [{"type_id": ORE, "solar_system_id": HOME, "quantity": 5}]
    )
    conn.commit()
    yield conn
    conn.close()


def _mock_market_and_contracts(contracts=()):
    def books(request):
        type_id = int(request.url.params["type_id"])
        price = {ORE: 10.0, MINERAL: 2.0}.get(type_id)
        data = [] if price is None else [
            {"price": price, "volume_remain": 10_000_000, "is_buy_order": True,
             "location_id": STATION, "system_id": HOME, "range": "station"}
        ]  # fmt: skip
        return httpx.Response(200, json=data)

    respx.get(url__regex=rf"{BASE}/markets/\d+/orders").mock(side_effect=books)
    respx.get(f"{BASE}/contracts/public/{REGION}").mock(
        return_value=httpx.Response(200, json=list(contracts))
    )


@respx.mock
def test_plan_sells_hangar_ore_then_mines(db):
    _mock_market_and_contracts()
    with httpx.Client() as http:
        plan = planner.build_plan(db, EsiClient(http, db, base_url=BASE), 7, 60)

    kinds = [item.kind for item in plan.items]
    assert kinds == ["skill", "sell_hangar", "mining"]
    assert plan.total_minutes == pytest.approx(60)
    mining_item = plan.items[-1]
    assert mining_item.risk.band == "low"
    assert mining_item.new_to_you is False  # the mining ledger shows mining
    assert mining_item.inputs["mining_minutes"] > 0
    assert {n.code for n in plan.notes} >= {"ores_from_mining_ledger", "loss_chances_are_guesses"}


@respx.mock
def test_muted_categories_become_alternatives(db):
    preferences.mute(db, "kind:mining")
    _mock_market_and_contracts()
    with httpx.Client() as http:
        plan = planner.build_plan(db, EsiClient(http, db, base_url=BASE), 7, 60)
    assert "mining" not in [i.kind for i in plan.items]
    assert {a.why_not for a in plan.alternatives if a.item.kind == "mining"} == {"muted"}
    assert preferences.unmute(db, "kind:mining") is True
    assert preferences.active_mutes(db) == set()


@respx.mock
def test_red_risk_is_left_out_unless_allowed(db):
    snapshots.record(db, 7, "wallet", 0.0)  # tiny net worth: the ship is most of it
    snapshots.record(db, 7, "location", {"solar_system_id": LOW, "station_id": None})
    db.execute("INSERT INTO sde_npc_stations VALUES (61000002, ?, 1000001, 1, 0.5, 0.05)", (LOW,))
    _mock_market_and_contracts()
    with httpx.Client() as http:
        esi = EsiClient(http, db, base_url=BASE)
        cautious = planner.build_plan(db, esi, 7, 30)
        bold = planner.build_plan(db, esi, 7, 30, allow_risky=True)
    assert "mining" not in [i.kind for i in cautious.items]
    assert any(a.why_not == "red_risk" for a in cautious.alternatives)
    assert "mining" in [i.kind for i in bold.items]


@respx.mock
def test_plans_are_recorded_and_cannot_be_changed(db):
    _mock_market_and_contracts()
    with httpx.Client() as http:
        plan = planner.build_plan(db, EsiClient(http, db, base_url=BASE), 7, 45)
    plan = planner.record(db, plan, {"note": "test"})
    rows = db.execute(
        "SELECT position, kind FROM plan_items WHERE plan_id = ? ORDER BY item_id", (plan.plan_id,)
    ).fetchall()
    assert [r["kind"] for r in rows if r["position"] is not None] == [i.kind for i in plan.items]

    item_id = db.execute("SELECT item_id FROM plan_items WHERE kind = 'mining' LIMIT 1").fetchone()[
        0
    ]
    planner.respond(db, item_id, "do")
    planner.respond(db, item_id, "mute")
    assert "kind:mining" in preferences.active_mutes(db)
    for table in ("plans", "plan_items", "plan_responses"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute(f"DELETE FROM {table}")  # noqa: S608


def test_bad_inputs(db):
    with httpx.Client() as http, pytest.raises(planner.PlanError):
        planner.build_plan(db, EsiClient(http, db, base_url=BASE), 7, 0)
    with pytest.raises(planner.PlanError):
        planner.respond(db, 999, "do")
    with pytest.raises(preferences.PreferenceError):
        preferences.mute(db, "kind:piracy")
