import json

import pytest

from eve_assistant import contracts, history, planner, projects, sessions, snapshots, store

OWNER, ALT, STRANGER, NPC = 7, 8, 95094576, 1000132


@pytest.fixture
def db():
    conn = store.connect(":memory:")
    snapshots.ensure_character(conn, OWNER, "Hauler")
    snapshots.ensure_character(conn, ALT, "Alt")
    conn.execute("INSERT INTO sde_npc_corporations VALUES (?, NULL)", (NPC,))
    conn.commit()
    yield conn
    conn.close()


def contract(contract_id, status, acceptor=OWNER, completed=None, **extra):
    return {
        "contract_id": contract_id,
        "type": "courier",
        "status": status,
        "title": "Player-written title",
        "issuer_id": STRANGER,
        "issuer_corporation_id": 98000001,
        "acceptor_id": acceptor,
        "assignee_id": 0,
        "date_issued": "2026-10-17T10:00:00Z",
        "date_accepted": "2026-10-17T11:00:00Z",
        "date_completed": completed,
        "days_to_complete": 3,
        "reward": 2_000_000.0,
        "collateral": 50_000_000.0,
        "volume": 12_000.0,
        "start_location_id": 60003760,
        "end_location_id": 60008494,
        **extra,
    }


def test_contracts_are_stored_without_other_players_or_titles(db):
    cleaned = snapshots.sanitise(
        db, "contracts", [contract(1, "in_progress"), contract(2, "outstanding", acceptor=ALT)]
    )
    mine, alts = cleaned
    assert mine["acceptor_id"] == OWNER
    assert alts["acceptor_id"] == ALT  # the owner's own characters are kept
    text = json.dumps(cleaned)
    for leaked in (str(STRANGER), "98000001", "Player-written", "title", "issuer"):
        assert leaked not in text
    assert mine["reward"] == 2_000_000.0
    assert snapshots.sanitise(db, "contracts", {"error": "x"}) == []


def test_contract_states():
    assert contracts.state(contract(1, "finished"), OWNER) == "delivered"
    assert contracts.state(contract(1, "failed"), OWNER) == "gave_up"
    assert contracts.state(contract(1, "in_progress"), OWNER) == "underway"
    assert contracts.state(contract(1, "in_progress", acceptor=ALT), OWNER) == "other"
    assert contracts.state(contract(1, "outstanding"), OWNER) == "other"


def _courier_project(db, contract_id):
    return projects.start(db, OWNER, "courier", str(contract_id), "Courier", 120, 2e6, {})


def test_courier_projects_close_themselves(db):
    delivered = _courier_project(db, 11)
    failed = _courier_project(db, 12)
    snapshots.record(
        db,
        OWNER,
        "contracts",
        snapshots.sanitise(db, "contracts", [contract(11, "finished"), contract(12, "failed")]),
    )
    closed = dict(contracts.sync_projects(db, OWNER))
    assert closed == {delivered: "done", failed: "dropped"}
    assert projects.open_projects(db, OWNER) == []


def test_projects_without_a_settled_contract_stay_open(db):
    underway = _courier_project(db, 21)
    unknown = _courier_project(db, 22)  # never accepted: not in the list
    snapshots.record(db, OWNER, "contracts", [contract(21, "in_progress")])
    assert contracts.sync_projects(db, OWNER) == []
    assert {p.project_id for p in projects.open_projects(db, OWNER)} == {underway, unknown}


def _played_with_courier(db, contract_id):
    plan = planner.Plan(
        character_id=OWNER, minutes_available=60, items=(), alternatives=(), notes=(),
        net_worth=0.0,
    )  # fmt: skip
    plan_id = planner.record(db, plan, {}).plan_id
    db.execute(
        "INSERT INTO plan_items (plan_id, position, kind, key, title, isk, minutes, "
        "risk_rating, expected_loss, details) VALUES (?, 0, 'courier', ?, 'Courier', 2000000, "
        "50, 'green', 0, ?)",
        (plan_id, str(contract_id), json.dumps({"inputs": {"contract_id": contract_id}})),
    )
    session = sessions.start(db, OWNER, plan_id, now="2026-10-17T10:30:00+00:00")
    session = sessions.finish(db, session.session_id, now="2026-10-17T12:00:00+00:00")
    db.execute(
        "INSERT INTO session_outcomes (session_id, computed_at, settled, content_hash, details) "
        "VALUES (?, 'x', 1, 'h', ?)",
        (session.session_id, json.dumps({
            "isk": {"net": 0, "contracts": 0.0}, "window": {
                "start": "2026-10-17T10:30:00+00:00", "end": "2026-10-17T12:02:00+00:00"},
            "plan": None, "data": {}, "notes": [], "mined": None, "mined_value": None,
        })),
    )  # fmt: skip
    db.commit()


def test_history_reads_courier_steps_from_the_contract(db):
    _played_with_courier(db, 31)
    snapshots.record(
        db, OWNER, "contracts", [contract(31, "finished", completed="2026-10-17T11:45:00Z")]
    )
    (step,) = history.sessions_history(db)[0].steps
    assert (step.verdict, step.actual_isk, step.source) == ("seen", 2_000_000.0, "contract")

    snapshots.record(db, OWNER, "contracts", [contract(31, "in_progress")])
    (step,) = history.sessions_history(db)[0].steps
    assert step.verdict == "underway"
    assert step.verdict_text == "accepted, not delivered yet"

    snapshots.record(db, OWNER, "contracts", [])
    (step,) = history.sessions_history(db)[0].steps
    assert step.verdict == "unknown"
