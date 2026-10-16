import httpx
import pytest
import respx

from eve_assistant import snapshots, store
from eve_assistant.esi import COMPATIBILITY_DATE
from eve_assistant.esi.client import EsiClient, EsiError, EsiRateLimited

BASE = "https://esi.test"


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def db():
    conn = store.connect(":memory:")
    yield conn
    conn.close()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def esi(db, clock):
    with httpx.Client() as http:
        yield EsiClient(http, db, token_for=lambda cid: f"token-{cid}", base_url=BASE, clock=clock)


@respx.mock
def test_sends_compatibility_date_and_token(esi):
    route = respx.get(f"{BASE}/characters/7/wallet").mock(
        return_value=httpx.Response(200, json=12.5, headers={"cache-control": "max-age=120"})
    )
    assert esi.get("/characters/7/wallet", character_id=7).data == 12.5
    sent = route.calls.last.request.headers
    assert sent["x-compatibility-date"] == COMPATIBILITY_DATE
    assert sent["authorization"] == "Bearer token-7"


@respx.mock
def test_public_routes_send_no_token(esi):
    route = respx.get(f"{BASE}/characters/7").mock(return_value=httpx.Response(200, json={}))
    esi.get("/characters/7")
    assert "authorization" not in route.calls.last.request.headers


@respx.mock
def test_fresh_cache_skips_the_request(esi, clock):
    route = respx.get(f"{BASE}/x").mock(
        return_value=httpx.Response(200, json=[1], headers={"cache-control": "max-age=300"})
    )
    esi.get("/x")
    clock.now += 100
    result = esi.get("/x")
    assert result.from_cache and result.data == [1]
    assert route.call_count == 1


@respx.mock
def test_stale_cache_uses_etag_and_accepts_304(esi, clock):
    route = respx.get(f"{BASE}/x").mock(
        side_effect=[
            httpx.Response(200, json=[1], headers={"etag": '"v1"', "cache-control": "max-age=60"}),
            httpx.Response(304, headers={"cache-control": "max-age=60"}),
        ]
    )
    esi.get("/x")
    clock.now += 61
    result = esi.get("/x")
    assert result.from_cache and result.data == [1]
    assert route.calls.last.request.headers["if-none-match"] == '"v1"'


@respx.mock
def test_pages_are_joined(esi):
    route = respx.get(f"{BASE}/assets")
    route.side_effect = lambda request: httpx.Response(
        200,
        json=[int(request.url.params.get("page", "1"))],
        headers={"x-pages": "3"},
    )
    assert esi.get("/assets").data == [1, 2, 3]


@respx.mock
def test_rate_limit_stops_with_retry_after(esi):
    respx.get(f"{BASE}/x").mock(return_value=httpx.Response(429, headers={"retry-after": "42"}))
    with pytest.raises(EsiRateLimited) as err:
        esi.get("/x")
    assert err.value.retry_after == 42


@respx.mock
def test_low_error_budget_pauses_before_ip_block(esi):
    respx.get(f"{BASE}/x").mock(
        return_value=httpx.Response(
            200,
            json=[],
            headers={"x-esi-error-limit-remain": "5", "x-esi-error-limit-reset": "30"},
        )
    )
    with pytest.raises(EsiRateLimited, match="error budget"):
        esi.get("/x")


@respx.mock
def test_forbidden_explains_missing_permission(esi):
    respx.get(f"{BASE}/x").mock(return_value=httpx.Response(403, json={"error": "nope"}))
    with pytest.raises(EsiError, match="missing a permission"):
        esi.get("/x", character_id=1)


@respx.mock
def test_collect_keeps_going_after_one_kind_fails(db, esi):
    snapshots.ensure_character(db, 7, "Pilot")
    # respx uses the first matching route, so the specific one goes first.
    respx.get(f"{BASE}/characters/7/assets").mock(return_value=httpx.Response(403))
    respx.get(url__startswith=f"{BASE}/characters/7").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    results = {r.kind: r.outcome for r in snapshots.collect(db, esi, 7)}
    assert results["assets"] == "failed"
    assert results["wallet"] == "changed"
    assert len(results) == len(snapshots.KINDS)
