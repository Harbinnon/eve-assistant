import functools
import json
import sqlite3

import pytest

from eve_assistant import cli, outcomes, planner, sessions, snapshots, store
from eve_assistant.skills import Reason

CHAR, VELDSPAR, PLAG = 7, 1230, 18
DAY = "2026-10-17"


def at(minutes: float) -> str:
    """A time `minutes` after 18:00 UTC on DAY (negative allowed)."""
    total = 18 * 60 + int(minutes)
    day, rest = divmod(total, 24 * 60)
    return f"2026-10-{17 + day:02d}T{rest // 60:02d}:{rest % 60:02d}:00+00:00"


def eve(minutes: float) -> str:
    """The same time as ESI writes it."""
    return at(minutes).replace("+00:00", "Z")


def snap(db, kind, payload, minutes, outcome="changed"):
    when = at(minutes)
    if outcome == "changed":
        db.execute(
            "INSERT INTO snapshots (character_id, kind, taken_at, content_hash, payload) "
            "VALUES (?, ?, ?, 'h', ?)",
            (CHAR, kind, when, json.dumps(payload)),
        )
    db.execute(
        "INSERT INTO snapshot_checks (character_id, kind, checked_at, outcome) VALUES (?, ?, ?, ?)",
        (CHAR, kind, when, outcome),
    )
    db.commit()


def ledger(quantity_by_type: dict[int, int]) -> list[dict]:
    return [
        {"date": DAY, "type_id": t, "quantity": q, "solar_system_id": 30005308}
        for t, q in quantity_by_type.items()
    ]


@pytest.fixture
def db():
    conn = store.connect(":memory:")
    snapshots.ensure_character(conn, CHAR, "Miner")
    conn.executemany(
        "INSERT INTO sde_types (type_id, name, group_id, published, volume) VALUES (?, ?, 1, 1, ?)",
        [(VELDSPAR, "Veldspar", 0.1), (PLAG, "Plagioclase", 0.35)],
    )
    conn.executemany(
        "INSERT INTO market_prices VALUES (?, ?, NULL, 'x')", [(VELDSPAR, 15.0), (PLAG, 60.0)]
    )
    conn.commit()
    yield conn
    conn.close()


def _plan(db) -> int:
    plan = planner.Plan(
        character_id=CHAR,
        minutes_available=60,
        items=(),
        alternatives=(),
        notes=(Reason("loss_chances_are_guesses"),),
        net_worth=1e9,
    )
    plan_id = planner.record(db, plan, {}).plan_id
    db.execute(
        "INSERT INTO plan_items (plan_id, position, kind, key, title, isk, minutes, risk_rating, "
        "expected_loss, details) VALUES (?, 0, 'mining', 'k', 'Mine Veldspar', 4000000, 50, "
        "'green', 0, '{}')",
        (plan_id,),
    )
    db.commit()
    planner.respond(db, db.execute("SELECT MAX(item_id) FROM plan_items").fetchone()[0], "do")
    return plan_id


def _played(db, start=0, end=60, plan_id=None) -> sessions.PlaySession:
    session = sessions.start(db, CHAR, plan_id, now=at(start))
    return sessions.finish(db, session.session_id, now=at(end))


JOURNAL = [
    {"date": eve(-30), "ref_type": "bounty_prizes", "amount": 1_000.0, "id": 1},
    {
        "date": eve(20),
        "ref_type": "market_transaction",
        "amount": 2_000_000.0,
        "id": 2,
        "first_party_id": 95094576,
        "second_party_id": CHAR,
        "description": "Someone bought your ore",
    },
    {"date": eve(20), "ref_type": "transaction_tax", "amount": -50_000.0, "id": 3},
    {"date": eve(40), "ref_type": "market_escrow", "amount": -300_000.0, "id": 4},
    {
        "date": eve(45),
        "ref_type": "player_donation",
        "amount": 5_000.0,
        "id": 5,
        "reason": "player-written text",
    },
    {"date": eve(61), "ref_type": "contract_reward", "amount": 700_000.0, "id": 6},  # grace
    {"date": eve(90), "ref_type": "bounty_prizes", "amount": 9_000.0, "id": 7},
]
TRANSACTIONS = [
    {"date": eve(20), "type_id": VELDSPAR, "quantity": 100_000, "unit_price": 20.0,
     "is_buy": False, "client_id": 95094576, "transaction_id": 1},
    {"date": eve(40), "type_id": PLAG, "quantity": 10, "unit_price": 30_000.0,
     "is_buy": True, "client_id": 95094577, "transaction_id": 2},
    {"date": eve(-20), "type_id": VELDSPAR, "quantity": 5, "unit_price": 20.0,
     "is_buy": False, "client_id": 95094576, "transaction_id": 3},
]  # fmt: skip


def _usual_data(db):
    snap(db, "mining_ledger", ledger({VELDSPAR: 100}), 0)
    snap(db, "mining_ledger", ledger({VELDSPAR: 400, PLAG: 1000}), 60)
    snap(db, "wallet_journal", JOURNAL, 60)
    snap(db, "wallet_transactions", TRANSACTIONS, 60)


def test_wallet_and_trades_inside_the_session(db):
    _usual_data(db)
    settled, details = outcomes.compute(db, _played(db), now=at(65))
    isk = details["isk"]
    assert isk["market_sales"] == 2_000_000
    assert isk["taxes_fees"] == -50_000
    assert isk["market_purchases"] == -300_000
    assert isk["contracts"] == 700_000  # just after "I'm done", within the grace
    assert isk["other"] == 5_000
    assert isk["net"] == 2_355_000
    assert details["sold"] == [
        {"type_id": VELDSPAR, "name": "Veldspar", "units": 100_000, "isk": 2_000_000.0}
    ]
    assert details["bought"][0]["units"] == 10
    assert not settled


def test_counterparties_and_player_text_are_not_copied(db):
    _usual_data(db)
    _settled, details = outcomes.compute(db, _played(db), now=at(65))
    text = json.dumps(details)
    for leaked in ("95094576", "95094577", "Someone bought", "player-written", "client_id"):
        assert leaked not in text


def test_ore_mined_is_the_ledger_difference(db):
    _usual_data(db)
    _settled, details = outcomes.compute(db, _played(db), now=at(65))
    mined = {m["name"]: m for m in details["mined"]}
    assert mined["Veldspar"]["units"] == 300
    assert mined["Veldspar"]["m3"] == pytest.approx(30)
    assert mined["Plagioclase"]["value"] == 60_000
    assert details["mined_m3"] == pytest.approx(380)
    assert details["mined_value"] == pytest.approx(64_500)


def test_provisional_until_fresh_data_then_settled(db):
    _usual_data(db)
    session = _played(db)
    assert outcomes.update(db, now=at(65)) == 1
    first = outcomes.latest(db, session.session_id)
    assert not first.settled
    assert "Provisional" in first.notes()[0]
    assert outcomes.update(db, now=at(66)) == 0  # nothing changed: no new row

    snap(db, "mining_ledger", None, 75, outcome="unchanged")  # ledger cache has passed
    snap(db, "wallet_journal", None, 125, outcome="unchanged")  # journal cache has passed
    assert outcomes.update(db, now=at(130)) == 1
    final = outcomes.latest(db, session.session_id)
    assert final.settled
    assert final.notes() == []
    assert outcomes.update(db, now=at(500)) == 0  # settled outcomes are left alone


def test_no_ledger_at_the_start_means_mining_unknown(db):
    snap(db, "mining_ledger", ledger({VELDSPAR: 100}), -60)  # an hour before: too old
    snap(db, "mining_ledger", ledger({VELDSPAR: 400}), 60)
    _settled, details = outcomes.compute(db, _played(db), now=at(65))
    assert details["mined"] is None
    assert details["notes"][-1]["code"] == "ledger_start_unknown"


def test_mining_stops_counting_at_the_next_session(db):
    snap(db, "mining_ledger", ledger({VELDSPAR: 100}), 0)
    snap(db, "mining_ledger", ledger({VELDSPAR: 400}), 70)
    first = _played(db, 0, 60)
    snap(db, "mining_ledger", ledger({VELDSPAR: 900}), 100)  # mined in the next session
    _played(db, 90, 120)
    settled, details = outcomes.compute(db, first, now=at(200))
    assert details["mined"][0]["units"] == 300
    assert {"code": "after_next_session"} in details["notes"]
    assert details["data"]["ledger_settled"]


def test_ledger_checked_too_late_settles_with_a_note(db):
    snap(db, "mining_ledger", ledger({VELDSPAR: 100}), 0)
    snap(db, "mining_ledger", ledger({VELDSPAR: 250}), 60)
    session = _played(db)
    _settled, details = outcomes.compute(db, session, now=at(60 + 200))
    assert details["data"]["ledger_settled"]
    assert {"code": "ledger_end_late"} in details["notes"]
    assert details["mined"][0]["units"] == 150


def test_plan_comparison_and_skipped_sessions(db):
    plan_id = _plan(db)
    _usual_data(db)
    session = _played(db, plan_id=plan_id)
    cancelled = sessions.start(db, CHAR, now=at(200))
    sessions.finish(db, cancelled.session_id, how="cancelled", now=at(210))
    running = sessions.start(db, CHAR, now=at(300))

    outcomes.update(db, now=at(305))
    outcome = outcomes.latest(db, session.session_id)
    assert outcome.planned_isk == 4_000_000
    assert outcome.planned_minutes == 50
    assert outcome.details["plan"]["items"][0]["response"] == "do"
    assert outcomes.latest(db, cancelled.session_id) is None
    with pytest.raises(sessions.SessionError, match="not a finished session"):
        outcomes.compute(db, running)
    assert [s.session_id for s, _ in outcomes.recent(db)] == [session.session_id]


def test_outcomes_are_append_only(db):
    _usual_data(db)
    _played(db)
    outcomes.update(db, now=at(65))
    for statement in ("UPDATE session_outcomes SET settled = 1", "DELETE FROM session_outcomes"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute(statement)


def test_cli_lists_outcomes(tmp_path, monkeypatch, capsys):
    path = tmp_path / "cli.sqlite3"
    conn = store.connect(path)
    snapshots.ensure_character(conn, CHAR, "Miner")
    snap(conn, "wallet_journal", JOURNAL, 60)
    _played(conn)
    conn.close()
    monkeypatch.setattr(store, "connect", functools.partial(store.connect, path))
    monkeypatch.setattr(outcomes, "utc_now", lambda: at(65))
    assert cli.main(["outcomes"]) == 0
    out = capsys.readouterr().out
    assert "Wallet change: +2,355,000 ISK." in out
    assert "Ore mined is unknown" in out
