"""Nullsec basics (roadmap 6.2): sovereignty, jump bridges, and risk in null."""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from eve_assistant import explain, risk, routes, signals, snapshots, sovereignty, store

NOW = datetime.fromisoformat("2026-10-19T12:00:00+00:00")
A, B, C, D, HIGH = 30001001, 30001002, 30001003, 30001004, 30000142
OURS, THEIRS = 99000001, 99000002


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
    conn.executemany(
        "INSERT INTO sde_solar_systems VALUES (?, ?, 1, 1, ?, 'B')",
        [(A, "1DQ1-A", -0.4), (B, "8QT-H4", -0.3), (C, "M-OEE8", -0.2), (D, "T5ZI-S", -0.1),
         (HIGH, "Jita", 0.95)],
    )  # fmt: skip
    for x, y in ((HIGH, A), (A, B), (B, C), (C, D)):
        conn.executemany("INSERT INTO sde_jumps VALUES (?, ?)", [(x, y), (y, x)])
    snapshots.ensure_character(conn, 7, "Pilot")
    snapshots.record(conn, 7, "public", {"corporation_id": 98000001, "alliance_id": OURS})
    yield conn
    conn.close()


def _sov(db):
    esi = FakeEsi({"/sovereignty/systems": [
        {"system_id": A, "alliance_id": OURS, "corporation_id": 98000001},
        {"system_id": B, "alliance_id": OURS, "corporation_id": 98000001},
        {"system_id": C, "alliance_id": THEIRS, "corporation_id": 98000002},
        {"system_id": D, "faction_id": 500010},
        {"system_id": HIGH},
    ]})  # fmt: skip
    sovereignty.refresh(db, esi, NOW)
    return esi


def _kills(db, kills):
    esi = FakeEsi({
        "/universe/system_kills": [{"system_id": s, "ship_kills": n, "pod_kills": 0}
                                   for s, n in kills],
        "/incursions": [],
        "/insurance/prices": [],
    })  # fmt: skip
    signals.refresh(db, esi, NOW)


def test_sovereignty_is_read_twice_a_day(db):
    assert sovereignty.due(db, NOW)
    _sov(db)
    assert not sovereignty.due(db, NOW + timedelta(hours=11))
    assert sovereignty.due(db, NOW + timedelta(hours=12))
    assert sovereignty.friendly_systems(db) == {A, B}
    assert [sovereignty.holder(db, s) for s in (A, C, D, HIGH)] == ["yours", "other", "npc", "none"]


def test_sovereignty_is_read_in_esis_current_shape(db):
    esi = FakeEsi({"/sovereignty/systems": {"solar_systems": [
        {"solar_system_id": A, "claim": {"alliance": {
            "alliance_id": OURS, "corporation_id": 98000001, "claimed_since": "2020-10-08",
            "sovereignty_hub": {"id": 1}, "development": {"military_level": 5}}}},
        {"solar_system_id": D, "claim": {"faction": {"faction_id": 500010}}},
        {"solar_system_id": C, "claim": {"unclaimed": True}},
    ]}})  # fmt: skip
    sovereignty.refresh(db, esi, NOW)
    assert sovereignty.friendly_systems(db) == {A}
    assert [sovereignty.holder(db, s) for s in (A, C, D)] == ["yours", "none", "npc"]


def test_bridges_are_read_from_the_usual_formats(db):
    text = "1DQ1-A » T5ZI-S\n\n8qt-h4 --> M-OEE8\n1DQ1-A @ 1DQ1-A V - Moon 2 <-> T5ZI-S @ x\n"
    assert sovereignty.parse_bridges(db, text) == [(A, D), (B, C), (A, D)]
    assert sovereignty.parse_bridges(db, "1DQ1-A->T5ZI-S\nM-OEE8<->8QT-H4") == [(A, D), (C, B)]
    assert sovereignty.save_bridges(db, text) == 2  # the same bridge twice counts once
    assert sovereignty.bridges(db) == [(A, D), (B, C)]
    assert sovereignty.bridges_text(db) == "1DQ1-A » T5ZI-S\n8QT-H4 » M-OEE8"
    with pytest.raises(sovereignty.BridgeError, match="lines 1, 3"):
        sovereignty.parse_bridges(db, "1DQ1-A\n8QT-H4 M-OEE8\nnothing here")
    assert sovereignty.save_bridges(db, "") == 0
    assert sovereignty.bridges(db) == []


def test_routes_take_bridges_both_ways_but_not_in_highsec_only(db):
    assert routes.jumps_between(db, HIGH, D) == [HIGH, A, B, C, D]
    sovereignty.save_bridges(db, "1DQ1-A T5ZI-S")
    assert routes.jumps_between(db, HIGH, D) == [HIGH, A, D]
    assert routes.jumps_between(db, D, A) == [D, A]
    assert routes.jumps_between(db, A, D, highsec_only=True) is None
    assert routes.within(db, A, 1) == {A: 0, HIGH: 1, B: 1, D: 1}


def test_quiet_space_held_by_your_alliance_is_medium(db):
    _sov(db)
    _kills(db, [])
    friendly = risk.assess(db, [A, B], now=NOW.isoformat())
    assert friendly.band == "medium"
    assert [r.code for r in friendly.reasons] == ["no_recent_kills", "friendly_space"]
    hostile = risk.assess(db, [B, C], now=NOW.isoformat())
    assert hostile.band == "high"
    assert "hostile_null" in [r.code for r in hostile.reasons]
    assert "anyone can shoot you there" in explain.describe("hostile_null", {})


def test_kills_in_your_space_make_it_high(db):
    _sov(db)
    _kills(db, [(B, 1)])
    assert risk.assess(db, [A, B], now=NOW.isoformat()).band == "high"


def test_a_nullsec_camp_shows_sooner_than_a_lowsec_one(db):
    _sov(db)
    _kills(db, [(B, signals.NULLSEC_CAMP - 1), (C, signals.NULLSEC_CAMP)])
    assert signals.hotspots(db, NOW.isoformat()) == {C: (signals.NULLSEC_CAMP, False)}
    assessed = risk.assess(db, [B, C], now=NOW.isoformat())
    assert "camped" in [r.code for r in assessed.reasons]
