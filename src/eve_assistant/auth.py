"""Logging characters in and keeping their tokens fresh.

Service-layer functions: the CLI and, later, the dashboard call these.
Refresh tokens live only in the secret store, keyed by character ID.
"""

import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from eve_assistant.config import Config
from eve_assistant.esi import SCOPES, sso, user_agent
from eve_assistant.esi.callback import wait_for_code
from eve_assistant.secrets_store import SecretStore

REVOKE_PAGE = "https://community.eveonline.com/support/third-party-applications/"


@dataclass(frozen=True)
class LoggedInCharacter:
    character_id: int
    name: str
    scopes: tuple[str, ...]
    added_at: float

    @property
    def missing_scopes(self) -> tuple[str, ...]:
        return tuple(s for s in SCOPES if s not in self.scopes)


def http_client(config: Config) -> httpx.Client:
    return httpx.Client(headers={"User-Agent": user_agent(config.contact)}, timeout=20)


def _characters(data: dict) -> dict:
    return data.setdefault("characters", {})


def _to_character(character_id: str, entry: dict) -> LoggedInCharacter:
    return LoggedInCharacter(
        character_id=int(character_id),
        name=entry["name"],
        scopes=tuple(entry["scopes"]),
        added_at=entry["added_at"],
    )


def login_character(
    config: Config,
    store: SecretStore,
    client: httpx.Client,
    open_browser: Callable[[str], object] = webbrowser.open,
    wait: Callable[[str, str], str] = wait_for_code,
) -> LoggedInCharacter:
    """Run the browser login for one character and store its refresh token."""
    pkce = sso.make_pkce_pair()
    state = sso.make_state()
    url = sso.authorize_url(config.client_id, config.callback_url, SCOPES, state, pkce.challenge)
    open_browser(url)
    code = wait(config.callback_url, state)

    tokens = sso.exchange_code(client, config.client_id, code, pkce.verifier)
    identity = sso.verify_access_token(
        tokens.access_token, sso.fetch_jwks(client), config.client_id
    )

    data = store.load()
    entry = {
        "name": identity.name,
        "refresh_token": tokens.refresh_token,
        "scopes": list(identity.scopes),
        "added_at": time.time(),
    }
    _characters(data)[str(identity.character_id)] = entry
    store.save(data)
    return _to_character(str(identity.character_id), entry)


def list_characters(store: SecretStore) -> list[LoggedInCharacter]:
    data = store.load()
    return sorted(
        (_to_character(cid, entry) for cid, entry in _characters(data).items()),
        key=lambda c: c.name.lower(),
    )


def access_token(
    config: Config, store: SecretStore, client: httpx.Client, character_id: int
) -> str:
    """A fresh access token for the character. Saves the rotated refresh token."""
    data = store.load()
    entry = _characters(data).get(str(character_id))
    if entry is None:
        raise sso.SSOError(f"Character {character_id} is not logged in. Run: eve-assistant login")
    tokens = sso.refresh(client, config.client_id, entry["refresh_token"])
    identity = sso.verify_access_token(
        tokens.access_token, sso.fetch_jwks(client), config.client_id
    )
    if identity.character_id != character_id:
        raise sso.SSOError("The refreshed token belongs to a different character.")
    entry["refresh_token"] = tokens.refresh_token
    entry["scopes"] = list(identity.scopes)
    store.save(data)
    return tokens.access_token


def logout(store: SecretStore, character_id: int) -> bool:
    """Forget a character's token locally. Returns False if it was not stored."""
    data = store.load()
    removed = _characters(data).pop(str(character_id), None)
    if removed is None:
        return False
    store.save(data)
    return True
