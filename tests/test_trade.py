"""Trade runs between the hubs (roadmap 5.5)."""

import pytest

from eve_assistant import market, snapshots, store, trade
from eve_assistant.market import Order

JITA, AMARR = market.HUBS[0], market.HUBS[1]
GOODS, SHIP, HAULER = 4001, 32880, 648


def sell(price, volume):
    return Order(price, volume, False, 1, 1, "station")


def buy(price, volume):
    return Order(price, volume, True, 1, 1, "station")


def test_both_books_are_walked_while_it_still_pays():
    units, cost, gross, b1, b2, s1, s2 = trade.walk(
        [sell(1.0, 10), sell(2.0, 10)], [buy(3.0, 8), buy(2.1, 20)], 0.9, 1e9, 1000
    )
    assert (units, cost, gross) == (10, 10.0, 8 * 3.0 + 2 * 2.1)  # 2.0 against 2.1 x 0.9: stop
    assert (b1, b2, s1, s2) == (1.0, 1.0, 3.0, 2.1)
    assert trade.walk([sell(1.0, 10)], [buy(3.0, 10)], 0.9, 4.5, 1000)[0] == 4  # ISK runs out
    assert trade.walk([sell(1.0, 10)], [buy(3.0, 10)], 0.9, 1e9, 3)[0] == 3  # cargo runs out
    assert trade.walk([], [buy(3.0, 10)], 0.9, 1e9, 3)[0] == 0


@pytest.fixture
def db():
    conn = store.connect(":memory:")
    store.ensure_market(conn)
    conn.executemany(
        "INSERT INTO sde_solar_systems VALUES (?, ?, ?, 1, 0.9, 'B')",
        [(JITA.system_id, "Jita", JITA.region_id), (AMARR.system_id, "Amarr", AMARR.region_id)],
    )
    link = [(JITA.system_id, AMARR.system_id), (AMARR.system_id, JITA.system_id)]
    conn.executemany("INSERT INTO sde_jumps VALUES (?, ?)", link)
    conn.executemany(
        "INSERT INTO sde_npc_stations VALUES (?, ?, 1000035, 1, 0.5, 0.05)",
        [(JITA.station_id, JITA.system_id), (AMARR.station_id, AMARR.system_id)],
    )
    conn.executemany(
        "INSERT INTO sde_types (type_id, name, group_id, published, volume, packaged_volume) "
        "VALUES (?, ?, 1, 1, ?, ?)",
        [(GOODS, "Goods", 10.0, None), (SHIP, "Venture", 29500.0, 2500.0),
         (HAULER, "Badger", 250000.0, 20000.0)],
    )  # fmt: skip
    orders = [
        (1, JITA.region_id, JITA.station_id, GOODS, 0, 100.0, 50),  # sell 50 at 100 in Jita
        (2, AMARR.region_id, AMARR.station_id, GOODS, 1, 150.0, 30),  # buy 30 at 150 in Amarr
        (3, AMARR.region_id, AMARR.station_id, GOODS, 0, 200.0, 10),  # sell 10 at 200 in Amarr
        (4, JITA.region_id, JITA.station_id, SHIP, 0, 400_000.0, 5),  # ships, packed
        (5, AMARR.region_id, AMARR.station_id, SHIP, 1, 500_000.0, 5),
    ]
    conn.executemany("INSERT INTO market.orders VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'x')", orders)
    conn.commit()
    yield conn
    conn.close()


FEES = trade.Fees(0.08, {JITA.station_id: 0.03, AMARR.station_id: 0.03})


def test_instant_runs_buy_here_and_sell_to_buy_orders_there(db):
    found = trade.runs(db, trade.Limits(isk=1e9, cargo_m3=1000.0), FEES)
    goods = next(r for r in found if r.type_id == GOODS)
    assert (goods.source, goods.dest, goods.units) == (JITA, AMARR, 30)
    assert goods.buy_cost == 3000.0
    assert goods.sell_value == pytest.approx(30 * 150 * 0.92)
    assert goods.profit == pytest.approx(30 * 150 * 0.92 - 3000)
    assert SHIP not in {r.type_id for r in found}  # no room for a Venture, even packed
    roomy = trade.runs(db, trade.Limits(isk=1e9, cargo_m3=5000.0), FEES)
    (ship,) = [r for r in roomy if r.type_id == SHIP]
    assert (ship.units, ship.m3) == (2, 5000.0)  # ships travel packed
    assert trade.runs(db, trade.Limits(isk=1e9, cargo_m3=1000.0, min_profit=2000), FEES) == []


def test_listed_runs_wait_for_buyers_and_are_capped_by_volume(db):
    volumes = {(AMARR.region_id, GOODS): 200.0}  # a tenth of it: 20 units
    found = trade.runs(db, trade.Limits(isk=1e9, cargo_m3=1000.0), FEES, listed=True,
                       volumes=volumes)  # fmt: skip
    (goods,) = [r for r in found if r.type_id == GOODS]
    assert goods.listed
    assert goods.units == 20
    assert goods.first_sell == pytest.approx(199.99)
    assert goods.sell_value == pytest.approx(20 * 199.99 * (1 - 0.08 - 0.03))
    assert trade.days_to_sell(goods) == 1.0


def test_a_trip_counts_the_way_there_and_the_trade(db):
    run = trade.runs(db, trade.Limits(isk=1e9, cargo_m3=1000.0), FEES)[0]
    journey = trade.trip(db, AMARR.system_id, run, minutes_a_jump=2.0, highsec_only=True)
    assert journey.path_to_source == [AMARR.system_id, JITA.system_id]
    assert journey.jumps == 1
    assert journey.minutes == pytest.approx(2 * 2.0 + 2 * trade.MINUTES_TO_DOCK
                                            + trade.MINUTES_TO_TRADE)  # fmt: skip


def test_the_board_uses_the_wallet_and_the_active_ships_cargo(db):
    snapshots.ensure_character(db, 7, "Trader")
    snapshots.record(db, 7, "public", {"race_id": 1})
    names = ("charisma", "intelligence", "memory", "perception", "willpower")
    snapshots.record(db, 7, "attributes", {n: 20 for n in names})
    snapshots.record(db, 7, "skills", {"total_sp": 1, "skills": []})
    snapshots.record(db, 7, "wallet", 5000.0)
    snapshots.record(db, 7, "location", {"solar_system_id": JITA.system_id,
                                         "station_id": JITA.station_id})  # fmt: skip
    db.execute("UPDATE sde_types SET capacity = 3900 WHERE type_id = ?", (HAULER,))
    snapshots.record(db, 7, "ship", {"ship_type_id": HAULER, "ship_item_id": 9001})
    board = trade.board(db, 7, trade.Wanted(source="Jita", dest="Amarr"))
    assert board.limits.isk == 5000.0
    assert board.limits.cargo_m3 == 3900.0
    assert board.ship.name == "Badger"
    (offer,) = [o for o in board.offers if o.run.type_id == GOODS]
    assert offer.run.units == 30
    assert offer.isk_per_hour > 0
    assert offer.rating.rating in ("green", "amber", "red")
    assert trade.board(db, 7, trade.Wanted(source="Amarr", dest="Jita")).offers == []
