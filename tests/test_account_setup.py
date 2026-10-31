"""Accounts set on the Characters page: the only way on a hosted copy, where
the `account` command can't be run. Playing together needs them."""

import pytest
from fastapi.testclient import TestClient

from eve_assistant import accounts, store, together
from eve_assistant.web import create_app
from tests.test_together import LOCAL, _token, _world


def test_setting_up_puts_the_character_on_the_account_and_marks_it():
    db = _world(second_account=None)
    accounts.set_up(db, 8, "  second ", "omega")
    assert together.members(db, [8])[0].account == "second"
    assert together.members(db, [8])[0].clone_state == "omega"
    assert together.check(together.members(db, [7, 8])) == []
    accounts.set_up(db, 8, "main", "omega")  # moved: the empty account goes
    assert accounts.labels(db) == ["main"]


@pytest.mark.parametrize(
    "label, state, message",
    [
        ("main", "gold", "Clone state must be one of"),
        ("", "omega", "Give the account a name"),
        ("x" * 41, "omega", "under 40 characters"),
    ],
)
def test_bad_account_details_are_refused(label, state, message):
    db = _world(second_account=None)
    with pytest.raises(accounts.AccountError, match=message):
        accounts.set_up(db, 8, label, state)


def test_the_characters_page_sets_an_account(tmp_path):
    path = tmp_path / "a.sqlite3"
    _world(path, second_account=None).close()
    with TestClient(create_app(path), base_url=LOCAL) as client:
        page = client.get("/characters").text
        assert "No account set" in page
        assert "Set account" in page
        assert '<option value="main">' in page  # existing names are suggested
        saved = client.post(
            "/characters/8/account",
            data={"token": _token(page), "label": "second", "state": "omega"},
            follow_redirects=False,
        )
        assert saved.status_code == 303
        page = client.get(saved.headers["location"]).text
        assert "Account saved." in page
        assert "Account: second" in page
        refused = client.post(
            "/characters/8/account",
            data={"token": _token(page), "label": "second", "state": "gold"},
        )
        assert refused.status_code == 400
    db = store.connect(path)
    assert together.check(together.members(db, [7, 8])) == []


def test_the_not_on_an_account_message_points_to_the_characters_page():
    from eve_assistant import explain

    text = explain.describe("together_no_account", {"character": "Alex"})
    assert "Characters page" in text
    assert "eve-assistant" not in text
