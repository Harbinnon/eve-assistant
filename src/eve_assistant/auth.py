"""Logging characters in and keeping their tokens fresh.

Service-layer functions: the CLI and, later, the dashboard call these.
Refresh tokens live only in the secret store, keyed by character ID.
"""

import contextlib
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from eve_assistant.config import Config
from eve_assistant.esi import SCOPES, scopes, sso, user_agent
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
        return self.missing(SCOPES)

    def missing(self, wanted: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(s for s in wanted if s not in self.scopes)


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


@dataclass(frozen=True)
class PendingLogin:
    """A login sent to EVE's page, waiting for EVE to send the browser back."""

    url: str  # EVE's login page, for this login
    state: str  # must come back unchanged
    verifier: str  # the PKCE secret half, kept here
    started: float


def start_login(config: Config) -> PendingLogin:
    pkce = sso.make_pkce_pair()
    state = sso.make_state()
    url = sso.authorize_url(
        config.client_id, config.callback_url, scopes(config.planets), state, pkce.challenge
    )
    return PendingLogin(url, state, pkce.verifier, time.time())


def login_character(
    config: Config,
    store: SecretStore,
    client: httpx.Client,
    open_browser: Callable[[str], object] = webbrowser.open,
    wait: Callable[[str, str], str] = wait_for_code,
    store_lock: contextlib.AbstractContextManager | None = None,
) -> LoggedInCharacter:
    """Run the browser login for one character and store its refresh token.

    store_lock is held only while the token store is read and written (not
    while waiting for the browser), so a dashboard refreshing tokens in the
    background can't save over the new character."""
    pending = start_login(config)
    open_browser(pending.url)
    code = wait(config.callback_url, pending.state)
    return finish_login(config, store, client, pending, code, store_lock)


def finish_login(
    config: Config,
    store: SecretStore,
    client: httpx.Client,
    pending: PendingLogin,
    code: str,
    store_lock: contextlib.AbstractContextManager | None = None,
) -> LoggedInCharacter:
    """Swap the code EVE sent back for tokens and store the refresh token. The
    hosted copy calls this from its own /callback page."""
    tokens = sso.exchange_code(client, config.client_id, code, pending.verifier)
    identity = sso.verify_access_token(
        tokens.access_token, sso.fetch_jwks(client), config.client_id
    )

    with store_lock or contextlib.nullcontext():
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
    return refresh_access(config, store, client, character_id).access_token


class AccessTokens:
    """Hands out access tokens, refreshing only when one is about to expire."""

    MARGIN_SECONDS = 60

    def __init__(self, config: Config, store: SecretStore, client: httpx.Client):
        self._config, self._store, self._client = config, store, client
        self._cache: dict[int, sso.TokenSet] = {}

    def get(self, character_id: int) -> str:
        cached = self._cache.get(character_id)
        if cached is None or cached.expires_at - time.time() < self.MARGIN_SECONDS:
            cached = refresh_access(self._config, self._store, self._client, character_id)
            self._cache[character_id] = cached
        return cached.access_token


def refresh_access(
    config: Config, store: SecretStore, client: httpx.Client, character_id: int
) -> sso.TokenSet:
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
    return tokens


def logout(store: SecretStore, character_id: int) -> bool:
    """Forget a character's token locally. Returns False if it was not stored."""
    data = store.load()
    removed = _characters(data).pop(str(character_id), None)
    if removed is None:
        return False
    store.save(data)
    return True
