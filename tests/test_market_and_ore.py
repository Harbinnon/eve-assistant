"""Fees, order books, routes and the ore comparison, on hand-made data."""

import httpx
import pytest
import respx

from eve_assistant import fees, market, ore, routes, sde, snapshots, store
from eve_assistant.esi.client import EsiClient

# A tiny map: Home (station 1) - Mid - Hub (Jita's real IDs), plus a lowsec
# shortcut Home - Low - Hub.
HOME, MID, LOW = 31000001, 31000002, 31000003
JITA = market.HUBS[0]
HOME_STATION = 61000001
ORE, MINERAL, REPROCESSING_SKILL, ORE_SKILL, ACCOUNTING = 1230, 34, 3385, 60377, 16622
BASE = "https://esi.test"


def test_sales_tax_and_broker_fee_rules():
    assert fees.sales_tax(0) == pytest.approx(0.075)
    assert fees.sales_tax(5) == pytest.approx(0.075 * 0.45)
    assert fees.broker_fee(0, 0, 0) == pytest.approx(0.03)
    assert fees.broker_fee(5, 2.0, 1.0) == pytest.approx(0.03 - 0.015 - 0.0006 - 0.0002)
    assert fees.broker_fee(5, 10, 10) == fees.BROKER_FEE_MINIMUM
    assert fees.broker_fee(0, -5, -5) == pytest.approx(0.03)  # bad standing never raises it


def test_reprocess_tax_falls_with_standing():
    assert fees.reprocess_tax(0.05, 0) == pytest.approx(0.05)
    assert fees.reprocess_tax(0.05, 3.335) == pytest.approx(0.025)
    assert fees.reprocess_tax(0.05, 8) == 0


def test_refine_yield_formula():
    assert ore.refine_yield(0.5, 0, 0, 0) == 0.5
    assert ore.refine_yield(0.5, 5, 5, 5) == pytest.approx(0.5 * 1.15 * 1.10 * 1.10)


def _order(price, volume, buy, location=JITA.station_id, system=JITA.system_id, rng="station"):
    return market.Order(price, volume, buy, location, system, rng)


def test_instant_sale_walks_down_the_book_and_respects_range():
    book = [
        _order(10.0, 100, True),
        _order(9.0, 100, True),
        _order(12.0, 1000, True, location=999, system=1),  # other system, station range
        _order(11.0, 50, True, location=999, system=1, rng="region"),
        _order(8.0, 10, False),  # a sell order: ignored
    ]
    fill = market.sell_into_buy_orders(book, 300, JITA.station_id, JITA.system_id)
    assert fill.quantity == 250
    assert fill.gross == pytest.approx(50 * 11 + 100 * 10 + 100 * 9)
    assert fill.lowest_price == 9.0
    assert market.lowest_sell(book, JITA.station_id) == 8.0


@pytest.fixture
def db():
    conn = store.connect(":memory:")
    conn.executemany(
        "INSERT INTO sde_solar_systems VALUES (?, ?, ?, 1, ?, 'B')",
        [(HOME, "Home", 10000032, 0.9), (MID, "Mid", 10000032, 0.7),
         (LOW, "Low", 10000032, 0.3), (JITA.system_id, "Jita", JITA.region_id, 0.95)],
    )  # fmt: skip
    conn.executemany(
        "INSERT INTO sde_regions VALUES (?, ?)",
        [(10000032, "Home Region"), (JITA.region_id, "The Forge")],
    )
    links = [(HOME, MID), (MID, JITA.system_id), (HOME, LOW), (LOW, JITA.system_id)]
    conn.executemany("INSERT INTO sde_jumps VALUES (?, ?)", links + [(b, a) for a, b in links])
    conn.executemany(
        "INSERT INTO sde_npc_stations VALUES (?, ?, ?, 1, 0.5, 0.05)",
        [(HOME_STATION, HOME, 1000001), (JITA.station_id, JITA.system_id, 1000035)],
    )
    conn.executemany("INSERT INTO sde_npc_corporations VALUES (?, ?)", [(1000001, 500001)])
    conn.executemany(
        "INSERT INTO sde_types (type_id, name, group_id, published, volume, portion_size) "
        "VALUES (?, ?, 1, 1, ?, ?)",
        [
            (ORE, "Veldspar", 0.1, 100),
            (MINERAL, "Tritanium", 0.01, 1),
            (REPROCESSING_SKILL, "Reprocessing", 0.01, 1),
            (ORE_SKILL, "Simple Ore Processing", 0.01, 1),
            (ACCOUNTING, "Accounting", 0.01, 1),
        ],
    )
    conn.execute("INSERT INTO sde_type_materials VALUES (?, ?, 400)", (ORE, MINERAL))
    conn.execute("INSERT INTO sde_type_attributes VALUES (?, 790, ?)", (ORE, ORE_SKILL))
    conn.execute("INSERT INTO sde_meta VALUES (1, 'x', 'y', ?)", (sde.LOADER_VERSION,))
    snapshots.ensure_character(conn, 7, "Pilot")
    snapshots.record(conn, 7, "public", {"race_id": 1})
    names = ("charisma", "intelligence", "memory", "perception", "willpower")
    snapshots.record(conn, 7, "attributes", {name: 20 for name in names})
    snapshots.record(
        conn,
        7,
        "skills",
        {"total_sp": 1000, "skills": [
            {"skill_id": REPROCESSING_SKILL, "trained_skill_level": 5, "active_skill_level": 5,
             "skillpoints_in_skill": 0},
            {"skill_id": ACCOUNTING, "trained_skill_level": 4, "active_skill_level": 2,
             "skillpoints_in_skill": 0}]},
    )  # fmt: skip
    snapshots.record(conn, 7, "standings", [{"from_id": 1000001, "from_type": "npc_corp",
                                            "standing": 3.335}])  # fmt: skip
    conn.commit()
    yield conn
    conn.close()


def test_routes_prefer_highsec_when_asked(db):
    assert routes.jumps_between(db, HOME, JITA.system_id, highsec_only=True) == [
        HOME,
        MID,
        JITA.system_id,
    ]
    assert routes.jump_count(db, HOME, JITA.system_id) == 2
    assert routes.jumps_between(db, HOME, LOW, highsec_only=True) is None
    assert routes.lowest_security(db, [HOME, LOW]) == pytest.approx(0.3)


def _book(*orders):
    return [
        {"price": p, "volume_remain": v, "is_buy_order": b, "location_id": loc,
         "system_id": sys, "range": "station"}
        for p, v, b, loc, sys in orders
    ]  # fmt: skip


@respx.mock
def test_ore_analysis_compares_selling_refining_and_hauling(db):
    home_region, jita_region = 10000032, JITA.region_id

    def books(request):
        region = int(request.url.path.split("/")[2])
        type_id = int(request.url.params["type_id"])
        if region == home_region and type_id == ORE:
            data = _book((9.0, 10_000, True, HOME_STATION, HOME))
        elif region == home_region and type_id == MINERAL:
            data = _book((4.0, 1_000_000, True, HOME_STATION, HOME))
        elif region == jita_region and type_id == ORE:
            data = _book((10.0, 3_000, True, JITA.station_id, JITA.system_id),
                         (11.0, 50_000, False, JITA.station_id, JITA.system_id))  # fmt: skip
        else:
            data = []
        return httpx.Response(200, json=data)

    respx.get(url__regex=rf"{BASE}/markets/\d+/orders").mock(side_effect=books)
    with httpx.Client() as http:
        result = ore.analyse(db, EsiClient(http, db, base_url=BASE), 7, ORE, 5_050, HOME_STATION)

    options = {o.key: o for o in result.options}
    tax = fees.sales_tax(2)  # Accounting is Alpha-limited to 2
    assert options["sell_raw_here"].isk == pytest.approx(5_050 * 9.0 * (1 - tax))
    assert options["sell_raw_here"].active_minutes == ore.MINUTES_TO_SELL

    expected_yield = 0.5 * 1.15  # Reprocessing 5, nothing else
    assert result.refine_yield == pytest.approx(expected_yield)
    assert result.refine_tax == pytest.approx(0.025)  # standing 3.335 halves the 5% take
    minerals = int(400 * 50 * expected_yield * (1 - 0.025))
    leftover_value = 50 * 9.0 * (1 - tax)
    assert options["refine_sell_here"].isk == pytest.approx(
        minerals * 4.0 * (1 - tax) + leftover_value
    )

    jita = options["sell_raw_jita"]
    assert jita.jumps == 2
    assert jita.active_minutes == ore.MINUTES_TO_SELL + 2 * ore.MINUTES_PER_JUMP
    assert jita.isk == pytest.approx(3_000 * 10.0 * (1 - tax))
    assert jita.reasons[0].code == "not_enough_buyers"
    assert options["list_raw_jita"].instant is False
    assert {r.code for r in result.reasons} >= {"leftover_not_refined", "skills_used"}
    # The unit prices each option expects, before tax, are kept for plans to record.
    assert options["sell_raw_here"].unit_prices == {ORE: 9.0}
    assert options["refine_sell_here"].unit_prices == {MINERAL: 4.0, ORE: 9.0}
    assert jita.unit_prices == {ORE: 10.0}
    assert options["list_raw_jita"].unit_prices == {ORE: pytest.approx(10.99)}
    assert next(r for r in result.reasons if r.code == "assumed_minutes_per_jump").detail == {
        "minutes": 1.0,
        "base": 1.0,
        "sessions": 0,
    }


def test_ore_analysis_refuses_non_ore(db):
    with httpx.Client() as http:
        esi = EsiClient(http, db, base_url=BASE)
        with pytest.raises(ore.OreError, match="cannot be refined"):
            ore.analyse(db, esi, 7, MINERAL, 10, HOME_STATION)


STRUCTURE = 1_050_000_000_001


def test_merge_counts_each_order_once():
    regional = [market.Order(9.0, 100, True, 1, 1, "10", order_id=1, type_id=ORE)]
    own = [
        market.Order(9.0, 100, True, 1, 1, "10", order_id=1, type_id=ORE),
        market.Order(9.5, 50, True, 1, 1, "station", order_id=2, type_id=ORE),
    ]
    assert [o.order_id for o in market.merge(regional, own)] == [1, 2]


def _structure(db, accessible=1):
    db.execute(
        "INSERT INTO structures (structure_id, accessible, player_name, solar_system_id, "
        "checked_at) VALUES (?, ?, 'Player-named citadel', ?, 'x')",
        (STRUCTURE, accessible, HOME),
    )
    db.commit()


def _structure_markets(own_orders, status=200):
    home_region = 10000032

    def books(request):
        region = int(request.url.path.split("/")[2])
        type_id = int(request.url.params["type_id"])
        data = []
        if region == home_region and type_id == ORE:
            data = [{"order_id": 1, "price": 9.0, "volume_remain": 1_000, "is_buy_order": True,
                     "location_id": STRUCTURE, "system_id": HOME, "range": "10"}]  # fmt: skip
        return httpx.Response(200, json=data)

    respx.get(url__regex=rf"{BASE}/markets/\d+/orders").mock(side_effect=books)
    respx.get(f"{BASE}/markets/structures/{STRUCTURE}").mock(
        return_value=httpx.Response(status, json=own_orders)
    )


@respx.mock
def test_selling_at_a_structure_counts_its_own_market(db):
    _structure(db)
    _structure_markets(
        [
            {"order_id": 1, "type_id": ORE, "price": 9.0, "volume_remain": 1_000,
             "is_buy_order": True, "location_id": STRUCTURE, "range": "10"},
            {"order_id": 2, "type_id": ORE, "price": 9.5, "volume_remain": 2_000,
             "is_buy_order": True, "location_id": STRUCTURE, "range": "station"},
            {"order_id": 3, "type_id": MINERAL, "price": 4.0, "volume_remain": 9,
             "is_buy_order": True, "location_id": STRUCTURE, "range": "station"},
        ]
    )  # fmt: skip
    esi = EsiClient(httpx.Client(), db, base_url=BASE, token_for=lambda _cid: "token")
    result = ore.analyse(db, esi, 7, ORE, 3_000, STRUCTURE)
    here = next(o for o in result.options if o.key == "sell_raw_here")
    tax = fees.sales_tax(2)
    assert here.isk == pytest.approx((2_000 * 9.5 + 1_000 * 9.0) * (1 - tax))  # order 1 once
    assert not [o for o in result.options if o.key.startswith("refine_sell")]
    codes = {r.code for r in result.reasons}
    assert {"selling_at_structure", "refining_at_structure_not_modelled"} <= codes
    assert "Player-named" not in " ".join(o.title for o in result.options)


@respx.mock
def test_a_structure_market_that_cannot_be_read_falls_back(db):
    _structure(db)
    _structure_markets({"error": "forbidden"}, status=403)
    esi = EsiClient(httpx.Client(), db, base_url=BASE, token_for=lambda _cid: "token")
    result = ore.analyse(db, esi, 7, ORE, 500, STRUCTURE)
    here = next(o for o in result.options if o.key == "sell_raw_here")
    assert here.isk == pytest.approx(500 * 9.0 * (1 - fees.sales_tax(2)))
    assert "structure_market_unavailable" in {r.code for r in result.reasons}


def test_structures_without_access_are_refused(db):
    _structure(db, accessible=0)
    with httpx.Client() as http:
        esi = EsiClient(http, db, base_url=BASE)
        with pytest.raises(ore.OreError, match="can access"):
            ore.analyse(db, esi, 7, ORE, 500, STRUCTURE)
