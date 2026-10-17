"""Live risk signals (roadmap 4.1): kills in the last hour, incursions and
insurance payouts, and how they set a route's chance band."""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from eve_assistant import explain, risk, signals, store

NOW = datetime.fromisoformat("2026-10-17T18:00:00+00:00")
HIGH_A, HIGH_B, LOW, NULL, EXO = 30000001, 30000002, 30000003, 30000004, 30000005
VENTURE = 32880


class FakeEsi:
    def __init__(self, kills=(), incursions=(), insurance=()):
        self.answers = {
            "/universe/system_kills": list(kills),
            "/incursions": list(incursions),
            "/insurance/prices": list(insurance),
        }
        self.calls = []

    def get(self, path, **_kwargs):
        self.calls.append(path)
        return SimpleNamespace(data=self.answers[path])


@pytest.fixture
def db():
    conn = store.connect(":memory:")
    conn.executemany(
        "INSERT INTO sde_solar_systems VALUES (?, ?, 1, 1, ?, 'B')",
        [(HIGH_A, "Highsec A", 0.9), (HIGH_B, "Highsec B", 0.5), (LOW, "Lowsec", 0.3),
         (NULL, "Nullsec", -0.2), (EXO, "Exordium One", 0.8)],
    )  # fmt: skip
    yield conn
    conn.close()


def _read(db, kills=(), incursions=(), now=NOW):
    esi = FakeEsi(
        kills=[{"system_id": s, "ship_kills": n, "pod_kills": 0, "npc_kills": 9} for s, n in kills],
        incursions=[{"infested_solar_systems": list(incursions), "state": "established"}],
        insurance=[{"type_id": VENTURE, "levels": [
            {"name": "Basic", "cost": 12_035.05, "payout": 120_350.5},
            {"name": "Platinum", "cost": 72_210.3, "payout": 240_701.0}]}],
    )  # fmt: skip
    signals.refresh(db, esi, now)
    return esi


def test_a_reading_replaces_the_last_and_insurance_comes_daily(db):
    first = _read(db, kills=[(HIGH_A, 2)])
    assert "/insurance/prices" in first.calls
    assert signals.kills_in(db, [HIGH_A, HIGH_B]) == {HIGH_A: 2}
    second = _read(db, kills=[(HIGH_B, 1)], now=NOW + timedelta(hours=1))
    assert "/insurance/prices" not in second.calls  # once a day is enough
    assert signals.kills_in(db, [HIGH_A, HIGH_B]) == {HIGH_B: 1}
    assert not signals.due(db, NOW + timedelta(minutes=90))
    assert signals.due(db, NOW + timedelta(hours=2))


def test_stale_signals_are_not_used(db):
    assert not signals.fresh(db, NOW.isoformat())  # never read
    _read(db, kills=[(HIGH_A, 9)])
    assert signals.fresh(db, (NOW + timedelta(hours=2)).isoformat())
    later = (NOW + timedelta(hours=4)).isoformat()
    assert not signals.fresh(db, later)
    assert risk.assess(db, [HIGH_A], now=later) == risk.Assessment("low")  # security alone


def test_an_uninsured_hull_still_pays_forty_percent(db):
    _read(db)
    assert signals.hull_payout(db, VENTURE) == pytest.approx(0.4 * 240_701.0)
    assert signals.hull_payout(db, 12345) == 0.0
    assert signals.hull_payout(db, None) == 0.0


@pytest.mark.parametrize(
    ("systems", "kills", "band", "codes"),
    [
        ([HIGH_A, HIGH_B], [], "very_low", ["no_recent_kills"]),
        ([HIGH_A, HIGH_B], [(HIGH_B, 1)], "low", ["recent_kills"]),
        ([HIGH_A, LOW], [], "low", ["no_recent_kills"]),
        ([HIGH_A, LOW], [(LOW, 2)], "medium", ["recent_kills"]),
        ([HIGH_A, HIGH_B], [(HIGH_A, 19)], "low", ["recent_kills"]),  # busy, not a hotspot
        ([HIGH_A, HIGH_B], [(HIGH_A, 20)], "medium", ["recent_kills", "hotspot"]),
        ([HIGH_A, LOW], [(LOW, 10)], "high", ["recent_kills", "camped"]),
        ([NULL], [], "high", ["no_recent_kills"]),
    ],
)
def test_the_band_follows_kills_along_the_way(db, systems, kills, band, codes):
    _read(db, kills=kills)
    assessed = risk.assess(db, systems, now=NOW.isoformat())
    assert assessed.band == band
    assert [r.code for r in assessed.reasons] == codes


def test_incursions_and_exordium(db):
    _read(db, incursions=[HIGH_B])
    assessed = risk.assess(db, [HIGH_A, HIGH_B], now=NOW.isoformat())
    assert assessed.band == "medium"
    assert assessed.reasons[-1].detail == {"systems": ["Highsec B"]}
    assert risk.assess(db, [EXO], "Exordium", now=NOW.isoformat()).band == "none"
    assert risk.assess(db, [], now=NOW.isoformat()).band == "high"  # nowhere known


def test_the_hull_payout_comes_off_the_expected_loss():
    rating = risk.rate("low", 1_000_000.0, 100_000_000.0, payout=400_000.0)
    assert rating.expected_loss == pytest.approx(0.005 * 600_000)
    assert rating.share_of_net_worth == pytest.approx(0.01)  # the whole stake still counts
    assert risk.rate("low", 100.0, 1e6, payout=400.0).expected_loss == 0.0
    text = explain.risk_summary(rating.as_dict())
    assert "after the 400,000 ISK EVE pays for the hull (uninsured)" in text


def test_reasons_read_plainly(db):
    _read(db, kills=[(HIGH_A, 25), (HIGH_B, 2), (LOW, 12)], incursions=[HIGH_B])
    texts = [
        explain.describe(r.code, r.detail)
        for r in risk.assess(db, [HIGH_A, HIGH_B, LOW], now=NOW.isoformat()).reasons
    ]
    assert texts == [
        "39 ships or pods destroyed in the last hour along the way, most in Highsec A (25), "
        "Lowsec (12) and Highsec B (2).",
        "Gank hotspot or busy hub: 25 ships and pods destroyed in Highsec A in the last hour.",
        "Possibly camped: 12 ships and pods destroyed in Lowsec in the last hour.",
        "Incursion in Highsec B: Sansha's ships there attack anyone, miners included.",
    ]
    _read(db, kills=[(HIGH_B, 1)])
    (only,) = risk.assess(db, [HIGH_A, HIGH_B], now=NOW.isoformat()).reasons
    assert explain.describe(only.code, only.detail) == (
        "1 ship or pod destroyed in the last hour along the way, in Highsec B."
    )
    assert "security alone" in explain.describe("no_live_signals", {})
