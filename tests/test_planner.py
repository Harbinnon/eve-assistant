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
from eve_assistant.activities import hauling
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
def test_live_signals_set_the_band_and_insurance_lowers_the_expected_loss(db):
    from datetime import UTC, datetime

    from eve_assistant import signals

    _mock_market_and_contracts()
    with httpx.Client() as http:
        stale = planner.build_plan(db, EsiClient(http, db, base_url=BASE), 7, 60)
    assert "no_live_signals" in {n.code for n in stale.notes}
    stale_mining = stale.items[-1]

    signals._mark(db, signals.SETTING, datetime.now(UTC))  # read just now, no kills anywhere
    db.execute("INSERT INTO insurance_prices VALUES (?, 'Platinum', 1, 50000)", (FRIGATE,))
    with httpx.Client() as http:
        live = planner.build_plan(db, EsiClient(http, db, base_url=BASE), 7, 60)
    assert "no_live_signals" not in {n.code for n in live.notes}
    mining = live.items[-1]
    assert mining.risk.band == "very_low"  # quiet highsec
    assert "no_recent_kills" in {r.code for r in mining.reasons}
    assert mining.risk.payout == 20_000.0  # 40% of the Platinum payout, uninsured
    assert stale_mining.risk.band == "low"


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
    with httpx.Client() as http:
        esi = EsiClient(http, db, base_url=BASE)
        with pytest.raises(planner.PlanError):
            planner.build_plan(db, esi, 7, 0)
    with pytest.raises(planner.PlanError):
        planner.respond(db, 999, "do")
    with pytest.raises(preferences.PreferenceError):
        preferences.mute(db, "kind:piracy")


def test_ship_capacity_decides_what_can_be_hauled():
    from eve_assistant import ore

    def option(m3, is_ore=True, jumps=3):
        return ore.Option(
            key="k", title="t", isk=1.0, active_minutes=1.0, instant=True, location="x",
            jumps=jumps, hauled_m3=m3, hauled_is_ore=is_ore,
        )  # fmt: skip

    holds = (50.0, 5000.0)  # cargo, ore hold
    assert hauling.fits_in_ship(option(4000), holds)  # ore goes in the ore hold
    assert not hauling.fits_in_ship(option(6000), holds)
    assert not hauling.fits_in_ship(option(100, is_ore=False), holds)  # minerals: cargo only
    assert hauling.fits_in_ship(option(10**9, jumps=0), holds)  # selling where it is


@respx.mock
def test_ore_stored_in_another_station_is_offered_with_travel(db):
    other_system, other_station = 31000003, 61000003
    db.execute(
        "INSERT INTO sde_solar_systems VALUES (?, 'Next Door', ?, 1, 0.8, 'B')",
        (other_system, REGION),
    )
    db.executemany(
        "INSERT INTO sde_jumps VALUES (?, ?)", [(HOME, other_system), (other_system, HOME)]
    )
    db.execute(
        "INSERT INTO sde_npc_stations VALUES (?, ?, 1000001, 1, 0.5, 0.05)",
        (other_station, other_system),
    )
    snapshots.record(
        db,
        7,
        "assets",
        [
            {"type_id": LASER, "location_id": 9001, "location_flag": "HiSlot0", "quantity": 1},
            {"type_id": ORE, "location_id": other_station, "location_flag": "Hangar",
             "quantity": 2000},
        ],
    )  # fmt: skip

    def books(request):
        price = {ORE: 10.0, MINERAL: 2.0}.get(int(request.url.params["type_id"]))
        data = [] if price is None else [
            {"price": price, "volume_remain": 10_000_000, "is_buy_order": True,
             "location_id": other_station, "system_id": other_system, "range": "station"}
        ]  # fmt: skip
        return httpx.Response(200, json=data)

    respx.get(url__regex=rf"{BASE}/markets/\d+/orders").mock(side_effect=books)
    respx.get(f"{BASE}/contracts/public/{REGION}").mock(return_value=httpx.Response(200, json=[]))
    with httpx.Client() as http:
        plan = planner.build_plan(db, EsiClient(http, db, base_url=BASE), 7, 60)

    sells = [i for i in [*plan.items, *(a.item for a in plan.alternatives)]
             if i.kind == "sell_hangar"]  # fmt: skip
    assert sells, "ore in the other station should be considered"
    best = max(sells, key=lambda i: i.isk)
    assert best.title.startswith("Go to Next Door (1 jumps), then: Veldspar x2,000")
    assert best.inputs["travel_jumps"] == 1
    assert best.minutes > 2  # travel time is included
    assert "ore_stored_elsewhere" in {r.code for r in best.reasons}


@respx.mock
def test_mining_uses_the_learned_speed_but_records_the_raw_estimate(db, monkeypatch):
    from eve_assistant import corrections

    _mock_market_and_contracts()
    with httpx.Client() as http:
        plain = planner.build_plan(db, EsiClient(http, db, base_url=BASE), 7, 60).items[-1]
    codes = [r.code for r in plain.reasons]
    assert "not_learned_yet" in codes

    def slower_mining(_db, _character_id, what):
        if what == "mining_rate":
            return corrections.Correction(what, 0.5, 4, 0.4)
        return corrections.Correction(what, 1.0, 0, None)

    monkeypatch.setattr(corrections, "correction", slower_mining)
    with httpx.Client() as http:
        slower = planner.build_plan(db, EsiClient(http, db, base_url=BASE), 7, 60).items[-1]
    raw = [r.detail["m3_per_minute"] for r in (plain.reasons[0], slower.reasons[0])]
    assert raw[0] == raw[1]  # what gets recorded is the uncorrected estimate
    assert slower.inputs["quantity"] < plain.inputs["quantity"] * 0.6
    learned = next(r for r in slower.reasons if r.code == "learned_correction")
    assert learned.detail["factor"] == 0.5
    assert learned.detail["sessions"] == 4


@respx.mock
def test_learned_sale_prices_scale_plan_isk_but_record_the_market_prices(db, monkeypatch):
    from eve_assistant import corrections

    _mock_market_and_contracts()
    with httpx.Client() as http:
        plain = planner.build_plan(db, EsiClient(http, db, base_url=BASE), 7, 60).items

    def lower_sales(_db, _character_id, what):
        if what == "sale_price":
            return corrections.Correction(what, 0.8, 3, 0.7)
        return corrections.Correction(what, 1.0, 0, None)

    monkeypatch.setattr(corrections, "correction", lower_sales)
    with httpx.Client() as http:
        lower = planner.build_plan(db, EsiClient(http, db, base_url=BASE), 7, 60).items
    for before, after in zip(plain[1:], lower[1:], strict=True):  # selling, then mining
        assert after.kind == before.kind
        assert after.isk == pytest.approx(before.isk * 0.8)
        assert after.inputs["unit_prices"] == before.inputs["unit_prices"]
        assert after.inputs["unit_prices"]  # the market's price per unit, before tax
        learned = [r for r in after.reasons if r.code == "learned_correction"]
        assert [r.detail["what"] for r in learned] == ["sale_price"]


STRUCTURE = 1_050_000_000_002


@respx.mock
def test_ore_in_an_accessible_structure_is_offered_and_sold_there(db):
    db.execute(
        "INSERT INTO structures (structure_id, accessible, player_name, solar_system_id, "
        "checked_at) VALUES (?, 1, 'Player-named', ?, 'x')",
        (STRUCTURE, HOME),
    )
    snapshots.record(db, 7, "location", {"solar_system_id": HOME, "structure_id": STRUCTURE})
    assets = [
        {"type_id": LASER, "location_id": 9001, "location_flag": "HiSlot0", "quantity": 1},
        {"type_id": ORE, "location_id": STRUCTURE, "location_flag": "Hangar", "quantity": 1000},
    ]
    snapshots.record(db, 7, "assets", assets)
    _mock_market_and_contracts()
    respx.get(f"{BASE}/markets/structures/{STRUCTURE}").mock(
        return_value=httpx.Response(200, json=[
            {"order_id": 77, "type_id": ORE, "price": 12.0, "volume_remain": 10_000,
             "is_buy_order": True, "location_id": STRUCTURE, "range": "station"}])
    )  # fmt: skip
    with httpx.Client() as http:
        esi = EsiClient(http, db, base_url=BASE, token_for=lambda _cid: "token")
        plan = planner.build_plan(db, esi, 7, 60)
    sell = next(i for i in plan.items if i.kind == "sell_hangar")
    assert sell.inputs["station_id"] == STRUCTURE
    assert sell.isk > 1000 * 10 * 0.9  # the structure's 12.00 order beats the station's 10.00
    codes = {n.code for n in plan.notes}
    assert "selling_at_structure_here" in codes
    assert "ore_in_structure_skipped" not in codes


def _saved_fits(db, fits):
    from eve_assistant import snapshots as snaps

    snaps.record(db, 7, "fittings", snaps.sanitise(db, "fittings", fits))


def _fit(name, lasers, fitting_id=1):
    return {
        "fitting_id": fitting_id,
        "name": name,
        "description": "My own notes",
        "ship_type_id": FRIGATE,
        "items": [{"type_id": LASER, "flag": f"HiSlot{n}", "quantity": 1} for n in range(lasers)]
        + [{"type_id": ORE, "flag": "Cargo", "quantity": 500}],
    }


@respx.mock
def test_a_better_saved_fit_is_mentioned_without_changing_the_estimate(db):
    _mock_market_and_contracts()
    with httpx.Client() as http:
        before = planner.build_plan(db, EsiClient(http, db, base_url=BASE), 7, 60).items[-1]
    _saved_fits(db, [_fit("Same as now", 1, 1), _fit("Two <lasers>", 2, 2)])
    with httpx.Client() as http:
        mining = planner.build_plan(db, EsiClient(http, db, base_url=BASE), 7, 60).items[-1]
    note = next(r for r in mining.reasons if r.code == "better_saved_fit")
    assert note.detail["fit_name"] == "Two <lasers>"
    assert note.detail["with_fit"] == pytest.approx(2 * note.detail["now"])
    assert note.detail["missing"] == ["Test Miner"]  # the second laser isn't in the hangar
    assert mining.inputs["quantity"] == before.inputs["quantity"]  # still the real fit

    from eve_assistant import explain

    text = explain.describe(note.code, note.detail)
    assert "your own name for it" in text
    assert "Not where the ship is: Test Miner." in text


@respx.mock
def test_no_note_when_saved_fits_are_no_better_or_the_modules_are_there(db):
    _mock_market_and_contracts()
    _saved_fits(db, [_fit("Same as now", 1)])
    with httpx.Client() as http:
        mining = planner.build_plan(db, EsiClient(http, db, base_url=BASE), 7, 60).items[-1]
    assert "better_saved_fit" not in [r.code for r in mining.reasons]

    from eve_assistant import fittings
    from eve_assistant import snapshots as snaps

    assets = snaps.latest(db, 7, "assets") + [
        {"type_id": LASER, "location_id": STATION, "location_flag": "Hangar", "quantity": 1}
    ]
    snaps.record(db, 7, "assets", assets)
    _saved_fits(db, [_fit("Two lasers", 2)])
    (fit,) = fittings.for_hull(db, 7, FRIGATE)
    assert fit.modules == (LASER, LASER)  # cargo isn't fitted
    assert fittings.missing_modules(db, 7, fit, 9001, STATION) == []


def test_saved_fit_descriptions_are_not_kept(db):
    _saved_fits(db, [_fit("Name", 1)])
    from eve_assistant import snapshots as snaps

    (stored,) = snaps.latest(db, 7, "fittings")
    assert "description" not in stored
    assert snaps.sanitise(db, "fittings", {"error": "x"}) == []
