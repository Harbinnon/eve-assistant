"""Corp-aware planning (roadmap 6.1): corp tax and the corporation's buyback."""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from eve_assistant import corporation, explain, market, ore, snapshots, store
from eve_assistant.activities import missions

NOW = datetime.fromisoformat("2026-10-19T12:00:00+00:00")
NPC, PLAYER, OTHER = 1000044, 98000001, 98000002
HOME, JITA = 30000001, 30000142


class FakeEsi:
    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def get(self, path, **_kwargs):
        self.calls.append(path)
        return SimpleNamespace(data=self.answers[path])


@pytest.fixture
def db():
    conn = store.connect(":memory:")
    conn.execute(
        "INSERT INTO sde_npc_corporations VALUES (?, 500001, 'School of Applied Knowledge')", (NPC,)
    )
    conn.executemany(
        "INSERT INTO sde_solar_systems VALUES (?, ?, 1, 1, 0.9, 'B')",
        [(HOME, "Home"), (JITA, "Jita")],
    )
    conn.executemany("INSERT INTO sde_jumps VALUES (?, ?)", [(HOME, JITA), (JITA, HOME)])
    snapshots.ensure_character(conn, 7, "Pilot")
    yield conn
    conn.close()


def _member_of(db, corporation_id, alliance_id=None):
    public = {"corporation_id": corporation_id}
    if alliance_id:
        public["alliance_id"] = alliance_id
    snapshots.record(db, 7, "public", public)


def _reward(amount, tax, receiver):
    return {"id": amount, "date": "2026-10-17T07:25:57Z", "ref_type": "agent_mission_reward",
            "amount": amount, "tax": tax, "tax_receiver_id": receiver}  # fmt: skip


def test_an_npc_corporation_takes_eleven_percent(db):
    _member_of(db, NPC)
    tax = corporation.tax(db, 7)
    assert (tax.rate, tax.source, tax.corporation) == (0.11, "npc", "School of Applied Knowledge")
    assert "takes 11.0% of mission rewards and bounties (what NPC corporations take)" in (
        explain.describe(tax.reason().code, tax.reason().detail)
    )


def test_the_journal_says_what_was_taken(db):
    _member_of(db, NPC)
    snapshots.record(db, 7, "wallet_journal", [
        _reward(311_500.0, 38_500.0, NPC),
        _reward(90_000.0, 10_000.0, OTHER),  # paid to an earlier corporation: not counted
        {"id": 1, "ref_type": "bounty_prizes", "amount": 12_000.0},  # no tax shown
    ])  # fmt: skip
    tax = corporation.tax(db, 7)
    assert tax.source == "journal"
    assert tax.rate == pytest.approx(0.11)


def test_a_player_corporation_uses_its_public_rate(db):
    _member_of(db, PLAYER, alliance_id=99000001)
    assert corporation.tax(db, 7).source == "unknown"  # not read yet
    public = {"name": "Haulers Inc", "ticker": "HAUL", "alliance_id": 99000001, "tax_rate": 0.05}
    esi = FakeEsi({f"/corporations/{PLAYER}": public})
    corporation.refresh(db, esi, 7, NOW)
    corporation.refresh(db, esi, 7, NOW + timedelta(hours=2))
    assert len(esi.calls) == 1  # once a day
    corporation.refresh(db, esi, 7, NOW + timedelta(days=1))
    assert len(esi.calls) == 2
    corp = corporation.of(db, 7)
    assert (corp.name, corp.is_npc, corp.alliance_id) == ("Haulers Inc", False, 99000001)
    assert corporation.tax(db, 7) == corporation.Tax(0.05, "corporation", "Haulers Inc")


def test_no_corporation_known_means_no_tax(db):
    assert corporation.tax(db, 7) == corporation.Tax(0.0, "unknown")
    assert corporation.of(db, 7) is None


def test_missions_pay_after_corp_tax(db):
    conn = db
    conn.execute("INSERT INTO sde_npc_stations VALUES (60000001, ?, ?, 1, 0.5, 0.05)", (HOME, NPC))
    conn.execute("INSERT INTO sde_agents VALUES (1, 'Agent One', ?, 60000001, 22, 1, 2, 0)", (NPC,))
    snapshots.record(conn, 7, "location", {"solar_system_id": HOME, "station_id": 60000001})
    _member_of(conn, NPC)
    esi = FakeEsi({f"/loyalty/stores/{NPC}/offers": []})
    item = missions.mission_candidates(conn, esi, 7, 60, 1e9, True)[0]
    isk_hour, _lp = missions.per_hour(1)
    assert item.isk == pytest.approx(isk_hour * 0.89)
    assert item.inputs["isk_per_hour"] == pytest.approx(isk_hour * 0.89)
    assert item.inputs["corp_tax"] == 0.11
    assert "corp_tax" in [r.code for r in item.reasons]


def test_the_buyback_is_saved_checked_and_removed(db):
    assert corporation.buyback(db) is None
    with pytest.raises(corporation.BuybackError, match="between 1% and 100%"):
        corporation.set_buyback(db, 120, "Jita")
    with pytest.raises(corporation.BuybackError, match="No solar system is called 'Nowhere'"):
        corporation.set_buyback(db, 90, "Nowhere")
    corporation.set_buyback(db, 90, "jita")
    assert corporation.buyback(db) == corporation.Buyback(0.9, JITA, "Jita")
    corporation.clear_buyback(db)
    assert corporation.buyback(db) is None


def test_selling_ore_to_the_buyback_is_an_option(db):
    jita = market.HUBS[0]
    orders = [
        market.Order(100.0, 10_000, True, jita.station_id, jita.system_id, "station"),
        market.Order(120.0, 10, True, 60000999, jita.system_id, "station"),  # another station
        market.Order(150.0, 500, False, jita.station_id, jita.system_id, "region"),
    ]
    here = {"system_id": HOME}
    assert ore._buyback_option(db, here, 1230, 1000, 100.0, lambda *_: orders, (True, 1.5)) is None
    corporation.set_buyback(db, 90, "Jita")
    option = ore._buyback_option(db, here, 1230, 1000, 100.0, lambda *_: orders, (True, 1.5))
    assert option.isk == pytest.approx(1000 * 100.0 * 0.9)
    assert (option.jumps, option.location, option.hauled_m3) == (1, "Jita", 100.0)
    assert option.title == "Sell the ore to your corporation's buyback in Jita (1 jumps)"
    assert option.active_minutes == pytest.approx(ore.MINUTES_TO_CONTRACT + 1.5)
    assert option.unit_prices == {}  # contracts don't teach market sale prices
    text = explain.describe(option.reasons[0].code, option.reasons[0].detail)
    assert "pays 90% of Jita's best buy (100 ISK a unit)" in text
