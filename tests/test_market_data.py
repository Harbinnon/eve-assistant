"""The trade tool's market data (roadmap 5.5): hub order books and volumes."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
import respx

from eve_assistant import market, market_data, store
from eve_assistant.esi.client import EsiClient, EsiRateLimited

BASE = "https://esi.test"
NOW = datetime(2026, 10, 18, 18, tzinfo=UTC)
JITA = market.HUBS[0]
PERIMETER_TOWER, ELSEWHERE = 1_035_466_617_946, 60000001


def order(order_id, location, type_id, is_buy, price, volume=100):
    return {"order_id": order_id, "location_id": location, "type_id": type_id,
            "is_buy_order": is_buy, "price": price, "volume_remain": volume,
            "min_volume": 1, "issued": "2026-10-18T10:00:00Z"}  # fmt: skip


class FakeEsi:
    def __init__(self, pages=None, history=None, fail=None):
        self._pages = pages or []
        self._history = history or []
        self.fail = fail
        self.history_calls = 0

    def pages(self, path, params=None, workers=1):
        if self.fail:
            raise self.fail
        yield from self._pages

    def get(self, path, params=None, remember=True):
        self.history_calls += 1
        return SimpleNamespace(data=self._history)


@pytest.fixture
def db():
    conn = store.connect(":memory:")
    conn.execute(
        "INSERT INTO sde_npc_stations VALUES (?, ?, 1000035, 1, 0.5, 0.05)",
        (JITA.station_id, JITA.system_id),
    )
    conn.execute(
        "INSERT INTO structures (structure_id, accessible, player_name, solar_system_id, "
        "checked_at) VALUES (?, 1, 'A tower', ?, 'x')",
        (PERIMETER_TOWER, JITA.system_id),
    )
    yield conn
    conn.close()


def test_a_scan_keeps_the_hub_and_usable_structures_and_replaces_the_last(db):
    pages = [
        [order(1, JITA.station_id, 34, False, 5.0), order(2, JITA.station_id, 34, True, 4.0)],
        [order(3, PERIMETER_TOWER, 34, False, 4.9), order(4, ELSEWHERE, 34, False, 1.0)],
    ]
    assert market_data.scan_hub(db, FakeEsi(pages), JITA, NOW) == 3  # elsewhere isn't kept
    assert market_data.scanned_at(db, JITA) == NOW.isoformat(timespec="seconds")
    sells = market_data.book(db, [JITA.station_id, PERIMETER_TOWER], 34, is_buy=False)
    assert [(o.order_id, o.price) for o in sells] == [(3, 4.9), (1, 5.0)]  # cheapest first
    assert sells[1].system_id == JITA.system_id
    market_data.scan_hub(db, FakeEsi([[order(9, JITA.station_id, 35, True, 7.0)]]), JITA, NOW)
    assert db.execute("SELECT order_id FROM market.orders").fetchall()[0][0] == 9


def test_only_stale_hubs_are_scanned_and_a_rate_limit_stops(db):
    assert market_data.stale(db, NOW, [JITA]) == [JITA]
    market_data.scan(db, FakeEsi([[]]), NOW, [JITA])
    assert market_data.stale(db, NOW + timedelta(minutes=29), [JITA]) == []
    assert market_data.stale(db, NOW + timedelta(minutes=30), [JITA]) == [JITA]
    limited = FakeEsi(fail=EsiRateLimited("slow down", 30))
    problems = market_data.scan(db, limited, NOW + timedelta(hours=1), market.HUBS)
    assert problems == ["Market scan paused: slow down"]  # the other hubs wait


def test_daily_volumes_come_from_history_once_a_day(db):
    history = [{"date": f"2026-10-{d:02d}", "volume": 3000, "average": 5.0} for d in range(1, 18)]
    esi = FakeEsi(history=history)
    assert market_data.volumes(db, esi, JITA.region_id, [34], NOW) == {34: 3000 * 17 / 30}
    market_data.volumes(db, esi, JITA.region_id, [34], NOW + timedelta(hours=5))
    assert esi.history_calls == 1


@respx.mock
def test_pages_come_back_in_order_a_few_at_a_time():
    def answer(request):
        page = int(request.url.params.get("page", 1))
        return httpx.Response(200, json=[page], headers={"x-pages": "4"})

    respx.get(f"{BASE}/markets/1/orders").mock(side_effect=answer)
    with httpx.Client() as http:
        esi = EsiClient(http, store.connect(":memory:"), base_url=BASE)
        assert list(esi.pages("/markets/1/orders", workers=3)) == [[1], [2], [3], [4]]
        assert list(esi.pages("/markets/1/orders")) == [[1], [2], [3], [4]]


def test_a_new_market_layout_is_recreated_empty(db, monkeypatch):
    store.ensure_market(db)
    db.execute("INSERT INTO market.scans VALUES (1, 'x', 1, 1)")
    monkeypatch.setattr(store, "MARKET_SCHEMA_VERSION", "2")
    store.ensure_market(db)
    assert db.execute("SELECT COUNT(*) FROM market.scans").fetchone()[0] == 0
