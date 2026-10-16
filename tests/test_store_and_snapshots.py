import sqlite3

import pytest

from eve_assistant import accounts, snapshots, store


@pytest.fixture
def db():
    conn = store.connect(":memory:")
    yield conn
    conn.close()


def test_migrations_apply_once(db):
    assert store.migrate(db) == []
    names = [r[0] for r in db.execute("SELECT name FROM schema_migrations ORDER BY number")]
    assert names[0] == "0001_initial.sql"
    assert [int(n.split("_")[0]) for n in names] == list(range(1, len(names) + 1))


def test_snapshots_are_written_only_on_change(db):
    snapshots.ensure_character(db, 1, "Pilot")
    assert snapshots.record(db, 1, "wallet", 100.0) is True
    assert snapshots.record(db, 1, "wallet", 100.0) is False
    assert snapshots.record(db, 1, "wallet", 250.5) is True
    rows = db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    checks = [r[0] for r in db.execute("SELECT outcome FROM snapshot_checks ORDER BY check_id")]
    assert rows == 2
    assert checks == ["changed", "unchanged", "changed"]
    assert snapshots.latest(db, 1, "wallet") == 250.5


def test_dict_key_order_does_not_count_as_a_change(db):
    snapshots.ensure_character(db, 1, "Pilot")
    snapshots.record(db, 1, "location", {"solar_system_id": 1, "station_id": 2})
    assert snapshots.record(db, 1, "location", {"station_id": 2, "solar_system_id": 1}) is False


def test_snapshots_cannot_be_edited_or_deleted(db):
    snapshots.ensure_character(db, 1, "Pilot")
    snapshots.record(db, 1, "wallet", 1.0)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("UPDATE snapshots SET payload = '2'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("DELETE FROM snapshots")


def test_accounts_group_characters(db):
    snapshots.ensure_character(db, 1, "Main")
    snapshots.ensure_character(db, 2, "Alt")
    snapshots.ensure_character(db, 3, "Other")
    accounts.assign(db, 1, "first")
    accounts.assign(db, 2, "first")
    accounts.assign(db, 3, "second")
    accounts.set_clone_state(db, "second", "alpha")
    views = {v.label: v for v in accounts.overview(db)}
    assert views["first"].characters == ("Alt", "Main")
    assert views["second"].clone_state == "alpha"

    accounts.assign(db, 3, "first")  # moving the last character removes the empty account
    assert [v.label for v in accounts.overview(db)] == ["first"]


def test_account_errors_are_explained(db):
    with pytest.raises(accounts.AccountError, match="not known yet"):
        accounts.assign(db, 99, "main")
    snapshots.ensure_character(db, 1, "Main")
    accounts.assign(db, 1, "main")
    with pytest.raises(accounts.AccountError, match="must be one of"):
        accounts.set_clone_state(db, "main", "gold")
    with pytest.raises(accounts.AccountError, match="No account"):
        accounts.set_clone_state(db, "nope", "alpha")
