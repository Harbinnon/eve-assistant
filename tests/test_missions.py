"""Distribution missions and LP store values (roadmap 5.3)."""

import json
from types import SimpleNamespace

import pytest

from eve_assistant import (
    corrections,
    explain,
    loyalty,
    outcomes,
    planner,
    routes,
    sessions,
    snapshots,
    store,
)
from eve_assistant.activities import missions

HOME, NEXT, FAR = 30000001, 30000002, 30000003
ST_HOME, ST_NEXT, ST_FAR = 60000001, 60000002, 60000003
CORP, FACTION, ITEM, TAG = 1000035, 500001, 4001, 4002


class FakeEsi:
    def __init__(self, offers):
        self.offers = offers
        self.calls = 0

    def get(self, path, **_kwargs):
        self.calls += 1
        return SimpleNamespace(data=self.offers)


@pytest.fixture
def db():
    conn = store.connect(":memory:")
    conn.executemany(
        "INSERT INTO sde_solar_systems VALUES (?, ?, 1, 1, 0.9, 'B')",
        [(HOME, "Home"), (NEXT, "Next"), (FAR, "Far")],
    )
    for a, b in ((HOME, NEXT), (NEXT, FAR)):
        conn.executemany("INSERT INTO sde_jumps VALUES (?, ?)", [(a, b), (b, a)])
    conn.executemany(
        "INSERT INTO sde_npc_stations VALUES (?, ?, ?, 1, 0.5, 0.05)",
        [(ST_HOME, HOME, CORP), (ST_NEXT, NEXT, CORP), (ST_FAR, FAR, CORP)],
    )
    conn.execute("INSERT INTO sde_npc_corporations VALUES (?, ?, 'Caldari Navy')", (CORP, FACTION))
    conn.executemany(
        "INSERT INTO sde_agents VALUES (?, ?, ?, ?, 22, ?, 2, 0)",
        [(1, "Agent One", CORP, ST_HOME, 1), (2, "Agent Two", CORP, ST_NEXT, 2),
         (3, "Agent Three", CORP, ST_FAR, 3)],
    )  # fmt: skip
    conn.executemany(
        "INSERT INTO market_prices VALUES (?, ?, NULL, 'x')", [(ITEM, 1_000_000.0), (TAG, 10_000.0)]
    )
    snapshots.ensure_character(conn, 7, "Hauler")
    snapshots.record(conn, 7, "location", {"solar_system_id": HOME, "station_id": ST_HOME})
    snapshots.record(conn, 7, "standings", [{"from_id": FACTION, "from_type": "faction",
                                             "standing": 1.5}])  # fmt: skip
    snapshots.record(conn, 7, "loyalty", [{"corporation_id": CORP, "loyalty_points": 4200}])
    conn.commit()
    yield conn
    conn.close()


def test_systems_within_a_few_jumps(db):
    assert routes.within(db, HOME, 1) == {HOME: 0, NEXT: 1}
    assert routes.within(db, HOME, 5) == {HOME: 0, NEXT: 1, FAR: 2}


def test_standing_opens_levels():
    assert missions.best_level({}, 1, CORP, FACTION) == 1
    assert missions.best_level({FACTION: 1.5}, 1, CORP, FACTION) == 2
    assert missions.best_level({CORP: 3.2, FACTION: 1.5}, 1, CORP, FACTION) == 3
    assert missions.best_level({1: 5.0}, 1, CORP, FACTION) == 4


def test_the_starting_guess_per_level():
    assert missions.per_hour(4) == (1_200_000.0, 2_250.0)
    assert missions.per_hour(2) == pytest.approx((1_200_000 / 9, 250.0))


def test_lp_is_worth_the_median_of_the_best_offers(db):
    offers = [
        {"type_id": ITEM, "quantity": 1, "lp_cost": 1000, "isk_cost": 500_000},  # 500 an LP
        {"type_id": ITEM, "quantity": 2, "lp_cost": 1000, "isk_cost": 0,
         "required_items": [{"type_id": TAG, "quantity": 10}]},  # 1,900 an LP
        {"type_id": TAG, "quantity": 1, "lp_cost": 100, "isk_cost": 0},  # 100 an LP
        {"type_id": 999, "quantity": 1, "lp_cost": 100, "isk_cost": 0},  # no price: left out
    ]  # fmt: skip
    esi = FakeEsi(offers)
    assert loyalty.store_value(db, esi, CORP, now="2026-10-17T10:00:00+00:00") == 500.0
    assert loyalty.store_value(db, esi, CORP, now="2026-10-17T20:00:00+00:00") == 500.0
    assert esi.calls == 1  # once a day
    assert loyalty.balance(db, 7, CORP) == 4200


def test_missions_at_the_nearest_agents_your_standing_opens(db):
    esi = FakeEsi([{"type_id": ITEM, "quantity": 1, "lp_cost": 1000, "isk_cost": 0}])
    items = missions.mission_candidates(db, esi, 7, 60, 1e9, True, [])
    assert [i.inputs["level"] for i in items] == [2, 1]  # level 3 needs standing 3
    two = items[0]
    assert two.title.startswith("Distribution missions for Agent Two (level 2, 1 jump away)")
    working = 60 - 1 - 2  # a jump, then docking
    isk_hour, lp_hour = missions.per_hour(2)
    lp = lp_hour * working / 60
    assert two.isk == pytest.approx(isk_hour * working / 60 + lp * 1000)
    texts = [explain.describe(r.code, r.detail) for r in two.reasons]
    assert any("worth about 1,000 ISK each at the Caldari Navy LP store" in t for t in texts)
    assert any("you have 4,200 LP with them" in t for t in texts)


def test_mission_rewards_are_counted_and_learned(db):
    assert outcomes._journal_group({"ref_type": "agent_mission_reward", "amount": 5}) == "missions"
    plan = planner.Plan(
        character_id=7, minutes_available=60, items=(), alternatives=(), notes=(), net_worth=0.0
    )
    plan_id = planner.record(db, plan, {}).plan_id
    details = {"inputs": {"mission_minutes": 60.0, "isk_per_hour": 400_000.0}}
    item_id = db.execute(
        "INSERT INTO plan_items (plan_id, position, kind, key, title, isk, minutes, risk_rating, "
        "expected_loss, details) VALUES (?, 0, 'mission', 'm', 'Missions', 1, 60, 'green', 0, ?)",
        (plan_id, json.dumps(details)),
    ).lastrowid
    session = sessions.start(db, 7, plan_id, now="2026-10-17T10:00:00+00:00")
    sessions.finish(db, session.session_id, now="2026-10-17T11:00:00+00:00")
    outcome = {"plan": {"items": [{"item_id": item_id, "kind": "mission", "response": None}]},
               "isk": {"missions": 600_000.0}}  # fmt: skip
    db.execute(
        "INSERT INTO session_outcomes (session_id, computed_at, settled, content_hash, details) "
        "VALUES (?, 'x', 1, 'h', ?)",
        (session.session_id, json.dumps(outcome)),
    )
    (seen,) = corrections.mission_observations(db, 7)
    assert seen.ratio == pytest.approx(1.5)
