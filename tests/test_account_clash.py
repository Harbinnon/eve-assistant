"""Characters put on one account that were online at the same time: an account
logs in one character at a time, so one of them must be on another account."""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from eve_assistant import accounts, snapshots
from eve_assistant.web import create_app
from tests.test_together import LOCAL, _world

NOW = datetime.now(UTC).replace(microsecond=0)


def _stamp(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat().replace("+00:00", "Z")


def _played(db, character_id, logged_in, logged_out):
    """An online check after a session: hours ago it started and ended."""
    snapshots.record(db, character_id, "online", {
        "online": False, "last_login": _stamp(logged_in), "last_logout": _stamp(logged_out),
        "logins": 1})  # fmt: skip
    db.commit()


def test_two_characters_online_together_cant_share_an_account():
    db = _world(second_account="main")  # both on main
    _played(db, 7, logged_in=3, logged_out=1)
    _played(db, 8, logged_in=2, logged_out=0.5)  # overlaps for an hour
    found = accounts.clashes(db, NOW)
    assert [(c.account, c.characters) for c in found] == [("main", ("Hauler", "Miner"))]
    assert found[0].at == (NOW - timedelta(hours=2)).isoformat()


def test_switching_characters_is_not_a_clash():
    db = _world(second_account="main")
    _played(db, 7, logged_in=3, logged_out=2)
    _played(db, 8, logged_in=2 - 1 / 60, logged_out=1)  # a minute's clock slack
    assert accounts.clashes(db, NOW) == []


def test_characters_on_different_accounts_may_play_together():
    db = _world()  # main and second
    _played(db, 7, logged_in=3, logged_out=1)
    _played(db, 8, logged_in=2, logged_out=0.5)
    assert accounts.clashes(db, NOW) == []


def test_only_the_last_two_weeks_count():
    db = _world(second_account="main")
    _played(db, 7, logged_in=24 * 20, logged_out=24 * 20 - 2)
    _played(db, 8, logged_in=24 * 20 - 1, logged_out=24 * 20 - 3)
    assert accounts.clashes(db, NOW) == []


def test_a_character_still_online_counts_until_its_last_check():
    db = _world(second_account="main")
    snapshots.record(db, 7, "online", {"online": True, "last_login": _stamp(2),
                                       "last_logout": _stamp(30), "logins": 2})  # fmt: skip
    db.commit()  # just checked: online since two hours ago
    _played(db, 8, logged_in=1, logged_out=0.5)
    assert [c.characters for c in accounts.clashes(db)] == [("Hauler", "Miner")]


def test_the_characters_page_says_so(tmp_path):
    path = tmp_path / "c.sqlite3"
    db = _world(path, second_account="main")
    _played(db, 7, logged_in=3, logged_out=1)
    _played(db, 8, logged_in=2, logged_out=0.5)
    db.close()
    with TestClient(create_app(path), base_url=LOCAL) as client:
        page = " ".join(client.get("/characters").text.split())
    assert "<strong>Hauler and Miner were both online at" in page
    assert "so they can't be on the same account (main)" in page
    assert "(so far: main, second)" in page  # the account names in use
