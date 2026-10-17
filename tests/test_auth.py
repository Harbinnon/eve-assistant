from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx

from eve_assistant import auth
from eve_assistant.esi import sso
from eve_assistant.secrets_store import MemoryStore


@pytest.fixture
def mocked_sso(jwks, make_token):
    with respx.mock as mock:
        mock.get(sso.JWKS_URL).mock(return_value=httpx.Response(200, json=jwks))
        yield mock


def test_login_stores_refresh_token_for_the_character(config, mocked_sso, make_token):
    token_route = mocked_sso.post(sso.TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={"access_token": make_token(), "refresh_token": "refresh-1", "expires_in": 1199},
        )
    )
    opened = []

    def fake_wait(callback_url, state):
        # The browser was sent to EVE with the same state the callback checks.
        assert parse_qs(urlsplit(opened[0]).query)["state"] == [state]
        return "one-time-code"

    store = MemoryStore()
    with httpx.Client() as client:
        character = auth.login_character(config, store, client, opened.append, fake_wait)

    assert character.character_id == 2112000001
    assert store.data["characters"]["2112000001"]["refresh_token"] == "refresh-1"
    sent = parse_qs(token_route.calls.last.request.content.decode())
    challenge = parse_qs(urlsplit(opened[0]).query)["code_challenge"][0]
    assert sso.challenge_for(sent["code_verifier"][0]) == challenge


def test_missing_scopes_are_reported(config, mocked_sso, make_token):
    mocked_sso.post(sso.TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": make_token(), "refresh_token": "r"})
    )
    with httpx.Client() as client:
        character = auth.login_character(
            config, MemoryStore(), client, lambda url: None, lambda url, state: "c"
        )
    assert "esi-assets.read_assets.v1" in character.missing_scopes
    assert "esi-skills.read_skills.v1" not in character.missing_scopes


def test_access_token_saves_the_rotated_refresh_token(config, mocked_sso, make_token):
    mocked_sso.post(sso.TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": make_token(), "refresh_token": "refresh-2"}
        )
    )
    store = MemoryStore(
        {
            "characters": {
                "2112000001": {
                    "name": "Test Pilot",
                    "refresh_token": "refresh-1",
                    "scopes": [],
                    "added_at": 0,
                }
            }
        }
    )
    with httpx.Client() as client:
        token = auth.access_token(config, store, client, 2112000001)
    assert token
    assert store.data["characters"]["2112000001"]["refresh_token"] == "refresh-2"


def test_refreshed_token_for_another_character_is_refused(config, mocked_sso, make_token):
    mocked_sso.post(sso.TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": make_token(sub="CHARACTER:EVE:999"), "refresh_token": "x"}
        )
    )
    store = MemoryStore(
        {
            "characters": {
                "2112000001": {
                    "name": "P",
                    "refresh_token": "refresh-1",
                    "scopes": [],
                    "added_at": 0,
                }
            }
        }
    )
    with httpx.Client() as client, pytest.raises(sso.SSOError, match="different character"):
        auth.access_token(config, store, client, 2112000001)
    assert store.data["characters"]["2112000001"]["refresh_token"] == "refresh-1"


def test_unknown_character_needs_login(config):
    store = MemoryStore()
    with httpx.Client() as client, pytest.raises(sso.SSOError, match="not logged in"):
        auth.access_token(config, store, client, 1)


def test_list_and_logout():
    store = MemoryStore(
        {
            "characters": {
                "2": {"name": "beta", "refresh_token": "b", "scopes": [], "added_at": 0},
                "1": {"name": "Alpha", "refresh_token": "a", "scopes": [], "added_at": 0},
            }
        }
    )
    assert [c.name for c in auth.list_characters(store)] == ["Alpha", "beta"]
    assert auth.logout(store, 1) is True
    assert auth.logout(store, 1) is False
    assert [c.character_id for c in auth.list_characters(store)] == [2]
