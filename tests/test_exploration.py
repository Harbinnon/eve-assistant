"""Exploration (roadmap 5.2): relic and data sites, a starting guess, and
learning from the change in net worth."""

import json
from datetime import datetime

import pytest

from eve_assistant import corrections, fleet, outcomes, planner, sessions, skills, snapshots, store
from eve_assistant.activities import exploration

FRIGATE, LAUNCHER, RELIC, DATA, ARCH, HACK = 605, 17938, 22177, 22175, 13278, 21718


@pytest.fixture
def db():
    conn = store.connect(":memory:")
    conn.executemany(
        "INSERT INTO sde_dogma_attributes VALUES (?, ?)",
        [(182, "requiredSkill1"), (277, "requiredSkill1Level"), (3083, "virusCoherence")],
    )
    conn.executemany(
        "INSERT INTO sde_groups VALUES (?, 1, ?, 1)",
        [(25, "Frigate"), (481, "Scan Probe Launcher"), (538, "Data Miners"), (16, "Skills")],
    )
    conn.executemany(
        "INSERT INTO sde_types (type_id, name, group_id, published) VALUES (?, ?, ?, 1)",
        [(FRIGATE, "Heron", 25), (LAUNCHER, "Core Probe Launcher I", 481),
         (RELIC, "Relic Analyzer I", 538), (DATA, "Data Analyzer I", 538),
         (ARCH, "Archaeology", 16), (HACK, "Hacking", 16)],
    )  # fmt: skip
    conn.executemany(
        "INSERT INTO sde_type_attributes VALUES (?, ?, ?)",
        [(RELIC, 3083, 40.0), (RELIC, 182, ARCH), (RELIC, 277, 1),
         (DATA, 3083, 40.0), (DATA, 182, HACK), (DATA, 277, 1)],
    )  # fmt: skip
    conn.executemany(
        "INSERT INTO sde_clone_grade_skills VALUES (1, 'Alpha', ?, 3)", [(ARCH,), (HACK,)]
    )
    snapshots.ensure_character(conn, 7, "Explorer")
    snapshots.record(conn, 7, "public", {"race_id": 1})
    snapshots.record(conn, 7, "attributes", {n: 20 for n in skills.ATTRIBUTE_NAMES.values()})
    snapshots.record(conn, 7, "skills", {"total_sp": 1, "skills": [
        {"skill_id": ARCH, "trained_skill_level": 1, "active_skill_level": 1,
         "skillpoints_in_skill": 0}]})  # fmt: skip
    snapshots.record(conn, 7, "location", {"solar_system_id": 30000142, "station_id": 1})
    snapshots.record(conn, 7, "ship", {"ship_type_id": FRIGATE, "ship_item_id": 9001})
    conn.commit()
    yield conn
    conn.close()


def _fit(db, *modules):
    snapshots.record(db, 7, "assets", [
        {"type_id": m, "location_id": 9001, "location_flag": f"HiSlot{n}", "quantity": 1}
        for n, m in enumerate(modules)
    ])  # fmt: skip


def test_the_fit_decides_which_sites(db):
    character = skills.load_character_skills(db, 7)
    assert exploration.site_kinds(db, character, [LAUNCHER, RELIC, DATA]) == ["relic"]  # no Hacking
    assert exploration.site_kinds(db, character, [RELIC]) == []  # can't probe without a launcher


def test_an_exploration_session_and_a_note_without_the_fit(db):
    _fit(db, LAUNCHER, RELIC)
    (item,) = exploration.exploration_candidates(db, 7, fleet.owned_ships(db, 7), 60, 1e9, [])
    assert item.kind == "exploration"
    assert item.title == "Scan for relic sites and hack them for 60 min"
    assert item.isk == pytest.approx(exploration.ISK_PER_HOUR)
    _fit(db, RELIC)
    notes = []
    assert exploration.exploration_candidates(db, 7, fleet.owned_ships(db, 7), 60, 1e9, notes) == []
    assert [n.code for n in notes] == ["exploration_needs_fit"]


def _snap(db, kind, payload, at):
    db.execute(
        "INSERT INTO snapshots (character_id, kind, taken_at, content_hash, payload) "
        "VALUES (7, ?, ?, ?, ?)",
        (kind, at, f"{kind}{at}", json.dumps(payload)),
    )


def test_income_is_learned_from_the_change_in_net_worth(db):
    db.execute("INSERT INTO market_prices VALUES (30000, 500000, NULL, 'x')")  # some loot
    _snap(db, "wallet", 1_000_000.0, "2026-10-17T10:01:00+00:00")
    _snap(db, "assets", [], "2026-10-17T10:01:00+00:00")
    _snap(db, "wallet", 1_000_000.0, "2026-10-17T11:00:00+00:00")
    _snap(db, "assets", [{"type_id": 30000, "quantity": 3}], "2026-10-17T11:00:00+00:00")
    start = datetime.fromisoformat("2026-10-17T10:00:00+00:00")
    end = datetime.fromisoformat("2026-10-17T11:00:00+00:00")
    assert outcomes._worth_change(db, 7, start, end) == {
        "start": 1_000_000.0, "end": 2_500_000.0, "change": 1_500_000.0}  # fmt: skip

    plan = planner.Plan(
        character_id=7, minutes_available=60, items=(), alternatives=(), notes=(), net_worth=0.0
    )
    plan_id = planner.record(db, plan, {}).plan_id
    details = {"inputs": {"exploration_minutes": 60.0, "isk_per_hour": 1_000_000.0}}
    item_id = db.execute(
        "INSERT INTO plan_items (plan_id, position, kind, key, title, isk, minutes, risk_rating, "
        "expected_loss, details) VALUES (?, 0, 'exploration', 'e', 'x', 1, 60, 'green', 0, ?)",
        (plan_id, json.dumps(details)),
    ).lastrowid
    session = sessions.start(db, 7, plan_id, now="2026-10-17T10:00:00+00:00")
    sessions.finish(db, session.session_id, now="2026-10-17T11:00:00+00:00")
    outcome = {"plan": {"items": [{"item_id": item_id, "kind": "exploration", "response": None}]},
               "worth": {"start": 1e6, "end": 2.5e6, "change": 1_500_000.0}}  # fmt: skip
    db.execute(
        "INSERT INTO session_outcomes (session_id, computed_at, settled, content_hash, details) "
        "VALUES (?, 'x', 1, 'h', ?)",
        (session.session_id, json.dumps(outcome)),
    )
    (seen,) = corrections.exploration_observations(db, 7)
    assert seen.ratio == pytest.approx(1.5)
