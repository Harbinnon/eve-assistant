"""The Wealth page (design 4.10): net worth with orders, income and spending, assets."""

import json
import re
from datetime import date

import pytest
from fastapi.testclient import TestClient

from eve_assistant import snapshots, store, wealth, worth
from eve_assistant.web import create_app

ORE, MINERAL, HULL, MODULE, BLUEPRINT = 1230, 34, 32880, 3651, 1010
JITA, AMARR = 30000142, 30002187
STATION, STRUCTURE = 60003760, 1_035_466_617_946
MINER, HAULER = 7, 8
LOCAL = "http://127.0.0.1"


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "wealth.sqlite3"
    conn = store.connect(path)
    conn.executemany(
        "INSERT INTO sde_groups VALUES (?, ?, ?, 1)",
        [(462, 25, "Veldspar"), (18, 4, "Mineral"), (25, 6, "Frigate"), (53, 7, "Laser"),
         (105, 9, "Frigate Blueprint")],
    )  # fmt: skip
    conn.executemany(
        "INSERT INTO sde_types (type_id, name, group_id, published) VALUES (?, ?, ?, 1)",
        [(ORE, "Veldspar", 462), (MINERAL, "Tritanium", 18), (HULL, "Venture", 25),
         (MODULE, "Miner I", 53), (BLUEPRINT, "Venture Blueprint", 105)],
    )  # fmt: skip
    conn.executemany(
        "INSERT INTO market_prices VALUES (?, ?, NULL, 'x')",
        [(ORE, 20.0), (MINERAL, 4.0), (HULL, 1_000_000.0), (MODULE, 50_000.0),
         (BLUEPRINT, 2_000_000.0)],
    )  # fmt: skip
    conn.executemany(
        "INSERT INTO sde_solar_systems VALUES (?, ?, 1, 1, ?, 'B')",
        [(JITA, "Jita", 0.946), (AMARR, "Amarr", 1.0)],
    )
    conn.execute("INSERT INTO sde_npc_stations (station_id, system_id, owner_id) "
                 "VALUES (?, ?, 1000035)", (STATION, JITA))  # fmt: skip
    conn.execute("INSERT INTO sde_npc_corporations (corporation_id, name) "
                 "VALUES (1000035, 'Caldari Navy')")  # fmt: skip
    conn.execute(
        "INSERT INTO structures (structure_id, accessible, player_name, solar_system_id, "
        "type_id, owner_id, checked_at) VALUES (?, 1, 'Mining Hub <b>', ?, 35832, 1, 'x')",
        (STRUCTURE, AMARR),
    )
    conn.execute("INSERT INTO accounts (label, clone_state) VALUES ('main', 'omega')")
    snapshots.ensure_character(conn, MINER, "Miner")
    snapshots.ensure_character(conn, HAULER, "Hauler")
    conn.execute("UPDATE characters SET account_id = 1 WHERE character_id = ?", (MINER,))
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def db(db_path):
    conn = store.connect(db_path)
    yield conn
    conn.close()


def snap(db, character_id, kind, payload, at="2026-10-30T12:00:00+00:00"):
    """A snapshot taken at a given time (record() uses the clock)."""
    db.execute(
        "INSERT INTO snapshots (character_id, kind, taken_at, content_hash, payload) "
        "VALUES (?, ?, ?, 'h', json(?))",
        (character_id, kind, at, json.dumps(payload)),
    )
    db.commit()


SELL = {"order_id": 1, "type_id": ORE, "volume_remain": 1_000, "price": 25.0}
BUY = {"order_id": 2, "type_id": MINERAL, "is_buy_order": True, "escrow": 300_000.0,
       "volume_remain": 100_000, "price": 3.0}  # fmt: skip
CORP = {"order_id": 3, "type_id": ORE, "volume_remain": 5_000, "is_corporation": True}


def test_orders_count_in_net_worth_both_ways_but_not_a_corporations(db):
    snap(db, MINER, "wallet", 1_000_000.0, "2026-10-29T08:00:00+00:00")
    snap(db, MINER, "orders", [SELL, BUY, CORP], "2026-10-29T09:00:00+00:00")
    points = worth.history(db, [MINER], now="2026-10-30T00:00:00+00:00")
    assert points[-1].orders == 20_000 + 300_000  # 1,000 ore at 20, and the escrow
    assert points[-1].total == 1_320_000

    (miner,) = wealth.holdings(db, [MINER])
    assert (miner.listed, miner.escrow, miner.account) == (20_000, 300_000, "main")
    assert miner.total == 1_320_000


def test_net_worth_changes_over_7_and_30_days(db):
    snap(db, MINER, "wallet", 1_000_000.0, "2026-10-10T08:00:00+00:00")
    snap(db, MINER, "wallet", 4_000_000.0, "2026-10-25T08:00:00+00:00")
    snap(db, MINER, "wallet", 3_000_000.0, "2026-10-29T08:00:00+00:00")
    points = worth.history(db, [MINER], now="2026-10-30T00:00:00+00:00")
    week, month = wealth.changes(points)
    assert (week.days, week.isk) == (7, 2_000_000)  # the 22nd still had 1M; now 3M
    assert (month.days, month.isk) == (30, None)  # the line starts on the 10th
    assert wealth.changes([]) == [wealth.Change(7, None), wealth.Change(30, None)]


@pytest.mark.parametrize(
    ("entry", "group"),
    [
        ({"ref_type": "market_transaction", "amount": 5.0}, "market_sales"),
        ({"ref_type": "market_transaction", "amount": -5.0}, "market_purchases"),
        ({"ref_type": "market_escrow", "amount": -5.0}, "market_purchases"),
        ({"ref_type": "transaction_tax", "amount": -1.0}, "taxes_fees"),
        ({"ref_type": "brokers_fee", "amount": -1.0}, "taxes_fees"),
        ({"ref_type": "contract_sales_tax", "amount": -1.0}, "taxes_fees"),
        ({"ref_type": "contract_reward", "amount": 1.0}, "contracts"),
        ({"ref_type": "skill_purchase", "amount": -1.0}, "skills"),
        ({"ref_type": "agent_mission_reward", "amount": 1.0}, "rewards"),
        ({"ref_type": "daily_goal_payouts", "amount": 1.0}, "rewards"),
        ({"ref_type": "bounty_prizes", "amount": 1.0}, "bounties"),
        ({"ref_type": "insurance", "amount": 1.0}, "insurance"),
        ({"ref_type": "planetary_export_tax", "amount": -1.0}, "industry"),
        ({"ref_type": "player_donation", "first_party_id": MINER, "second_party_id": HAULER},
         "own"),
        ({"ref_type": "player_donation", "first_party_id": MINER}, "players"),  # a stranger
        ({"ref_type": "something_new", "amount": 1.0}, "other"),
    ],
)  # fmt: skip
def test_journal_entries_fall_into_groups(entry, group):
    assert wealth.category(entry, {MINER, HAULER}) == group


def _entry(id_, day, ref, amount, **parties):
    return {"id": id_, "date": f"{day}T10:00:00Z", "ref_type": ref, "amount": amount, **parties}


def test_income_and_spending_by_week_each_entry_once(db):
    sale = _entry(1, "2026-10-28", "market_transaction", 5_000_000.0)
    earlier = _entry(2, "2026-10-21", "skill_purchase", -1_000_000.0)
    gift = _entry(3, "2026-10-28", "player_donation", -2_000_000.0,
                  first_party_id=MINER, second_party_id=HAULER)  # fmt: skip
    snap(db, MINER, "wallet_journal", [earlier, sale], "2026-10-28T12:00:00+00:00")
    snap(db, MINER, "wallet_journal", [earlier, sale, gift], "2026-10-29T12:00:00+00:00")
    received = _entry(9, "2026-10-28", "player_donation", 2_000_000.0,
                      first_party_id=MINER, second_party_id=HAULER)  # fmt: skip
    snap(db, HAULER, "wallet_journal", [received])

    everyone = wealth.flows(db, [MINER, HAULER], "week", today=date(2026, 10, 30))
    assert [p.label for p in everyone.periods[:2]] == ["26 Oct", "19 Oct"]
    assert len(everyone.periods) == 2  # none from before the journal starts
    rows = {r.code: r.amounts[:2] for r in everyone.rows}
    assert rows == {"market_sales": (5_000_000, 0), "skills": (0, -1_000_000)}  # gift cancels
    assert everyone.left_out_own
    assert everyone.net[:2] == (5_000_000, -1_000_000)
    assert everyone.first == "2026-10-21T10:00:00Z"

    miner = wealth.flows(db, [MINER], "month", today=date(2026, 10, 30))
    assert [p.label for p in miner.periods] == ["Oct 2026"]
    assert {r.code: r.total for r in miner.rows}["own"] == -2_000_000
    assert not miner.left_out_own


HANGAR = {"location_type": "station", "location_id": STATION, "location_flag": "Hangar"}


def _item(item_id, type_id, quantity=1, **where):
    return {"item_id": item_id, "type_id": type_id, "quantity": quantity, **(where or HANGAR)}


def test_assets_by_place_through_ships_with_whats_worth_selling(db):
    snap(db, MINER, "assets", [
        _item(1, HULL),  # a ship in the hangar
        _item(2, MODULE, location_type="item", location_id=1, location_flag="HiSlot0"),
        _item(3, ORE, 50_000, location_type="item", location_id=1,
              location_flag="SpecializedOreHold"),  # in the ship: not "left in a hangar"
        _item(4, ORE, 10_000),  # loose: 200k, worth selling
        _item(5, MINERAL, 1_000),  # loose but only 4k
        _item(6, BLUEPRINT, is_blueprint_copy=True, **HANGAR),
        _item(7, HULL, location_type="solar_system", location_id=JITA, location_flag="Hangar"),
    ])  # fmt: skip
    snap(db, HAULER, "assets", [
        _item(20, ORE, 6_000, location_type="item", location_id=STRUCTURE,
              location_flag="Hangar"),
    ])  # fmt: skip

    result = wealth.assets(db, [MINER, HAULER])
    station, space, hub = result.places
    assert station.label == "Caldari Navy station in Jita"
    assert station.value == 1_000_000 + 50_000 + 60_000 * 20 + 4_000  # the copy counts nothing
    assert [s.name for s in station.stacks][:2] == ["Veldspar", "Venture"]
    assert {s.name: s.group for s in station.stacks}["Miner I"] == "Modules"
    assert (space.label, space.value) == ("In space in Jita", 1_000_000)
    assert (hub.label, hub.player_named, hub.system) == ("Mining Hub <b>", True, "Amarr")
    assert [(i.stack.name, i.stack.quantity, i.place.label) for i in result.idle] == [
        ("Veldspar", 10_000, "Caldari Navy station in Jita"),
        ("Veldspar", 6_000, "Mining Hub <b>"),
    ]  # not the ore in the ship, nor 4k of Tritanium
    assert result.value == pytest.approx(sum(p.value for p in result.places))


@pytest.fixture
def client(db_path):
    conn = store.connect(db_path)
    snap(conn, MINER, "wallet", 2_000_000.0)
    snap(conn, MINER, "orders", [SELL, BUY])
    snap(conn, MINER, "assets", [_item(4, ORE, 10_000)])
    snap(conn, MINER, "wallet_journal", [_entry(1, date.today().isoformat(),
                                                "market_transaction", 5_000_000.0)])  # fmt: skip
    conn.close()
    with TestClient(create_app(db_path), base_url=LOCAL) as test_client:
        yield test_client


def test_the_wealth_page_for_everyone_and_for_one(client):
    page = client.get("/wealth").text
    assert 'aria-current="page">Wealth' in page
    assert "Account main" in page
    assert "Market sales" in page
    assert "+5M" in page
    assert "Worth selling" in page
    assert "Caldari Navy station in Jita" in page

    one = client.get(f"/wealth?who={HAULER}&by=month").text
    assert '<option value="8" selected>Hauler</option>' in one
    assert '<option value="month" selected>Month</option>' in one
    assert "Market sales" not in one  # the hauler's journal is empty
    assert client.get("/wealth?who=999&by=nonsense").status_code == 200  # unknown: everyone


def test_the_wealth_page_has_no_inline_styles_scripts_or_repeated_ids(client):
    page = client.get("/wealth").text
    assert "<script" not in page
    assert "style=" not in page
    assert "<style" not in page
    ids = re.findall(r'\sid="([^"]+)"', page)
    assert len(ids) == len(set(ids))
