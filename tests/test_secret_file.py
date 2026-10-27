"""The key-encrypted secret file for the hosted copy on Linux (roadmap 7.2),
and alert addresses from the server's environment."""

import os
import sys

import httpx
import pytest
import respx

from eve_assistant import alerts, secrets_store, store
from eve_assistant.secrets_store import EncryptedFileStore, SecretStoreError


@pytest.fixture
def key():
    return secrets_store.new_key()


def test_secrets_survive_a_round_trip_and_the_file_is_unreadable(tmp_path, key):
    path = tmp_path / "state" / "secrets"
    found = EncryptedFileStore(path, key)
    assert found.load() == {}
    found.save({"characters": {"7": {"refresh_token": "abc123"}}})
    assert b"abc123" not in path.read_bytes()
    assert EncryptedFileStore(path, key).load() == {
        "characters": {"7": {"refresh_token": "abc123"}}
    }
    assert not path.with_name("secrets.tmp").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_only_its_owner_can_read_the_file(tmp_path, key):
    path = tmp_path / "secrets"
    EncryptedFileStore(path, key).save({})
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_a_wrong_or_missing_key_is_a_clear_error(tmp_path, key, monkeypatch):
    path = tmp_path / "secrets"
    EncryptedFileStore(path, key).save({"x": 1})
    other_key = EncryptedFileStore(path, secrets_store.new_key())
    with pytest.raises(SecretStoreError, match="can't be read with this key"):
        other_key.load()
    path.write_bytes(path.read_bytes()[:-3] + b"xyz")  # altered
    right_key = EncryptedFileStore(path, key)
    with pytest.raises(SecretStoreError, match="can't be read with this key"):
        right_key.load()
    monkeypatch.delenv(secrets_store.KEY_ENV, raising=False)
    with pytest.raises(SecretStoreError, match="EVE_ASSISTANT_SECRET_KEY isn't set"):
        EncryptedFileStore(path)
    with pytest.raises(SecretStoreError, match="44 characters"):
        EncryptedFileStore(path, "not-a-key")


def test_the_key_and_path_come_from_the_environment(tmp_path, key, monkeypatch):
    monkeypatch.setenv(secrets_store.KEY_ENV, key)
    monkeypatch.setenv(secrets_store.PATH_ENV, str(tmp_path / "s"))
    monkeypatch.setattr(secrets_store.sys, "platform", "linux")
    found = secrets_store.default_store()
    assert isinstance(found, EncryptedFileStore)
    assert found.path == tmp_path / "s"


@respx.mock
def test_alert_addresses_and_the_ntfy_token_from_the_environment(monkeypatch):
    monkeypatch.setenv("EVE_ASSISTANT_NTFY_URL", "https://ntfy.example/eve")
    monkeypatch.setenv("EVE_ASSISTANT_NTFY_TOKEN", "tk_test")  # pragma: allowlist secret
    assert alerts.addresses(None) == {"ntfy": "https://ntfy.example/eve"}
    route = respx.post("https://ntfy.example/eve").mock(return_value=httpx.Response(200))
    db = store.connect(":memory:")
    with httpx.Client() as http:
        assert alerts.send(db, None, http, alerts.health_alerts(["ESI is down"])) == (1, [])
    assert route.calls[0].request.headers["Authorization"] == "Bearer tk_test"
    db.close()
