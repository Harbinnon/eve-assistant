import sys

import pytest

from eve_assistant.config import ConfigError, load_config
from eve_assistant.secrets_store import DpapiFileStore, SecretStoreError


def test_missing_config_explains_what_to_do(tmp_path):
    with pytest.raises(ConfigError, match="local.example.toml"):
        load_config(tmp_path / "local.toml")


def test_placeholder_client_id_is_rejected(tmp_path):
    path = tmp_path / "local.toml"
    path.write_text('[esi]\nclient_id = "<your client id>"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="No ESI client ID"):
        load_config(path)


def test_config_reads_values_and_drops_placeholder_contact(tmp_path):
    path = tmp_path / "local.toml"
    path.write_text(
        '[esi]\nclient_id = "abc"\ncontact = "<your EVE character name or email>"\n',
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.client_id == "abc"
    assert config.contact is None
    assert config.callback_url == "http://localhost:8765/callback"
    assert config.mode == "local"


def test_bad_mode_is_rejected(tmp_path):
    path = tmp_path / "local.toml"
    path.write_text('[esi]\nclient_id = "abc"\n[app]\nmode = "cloud"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="mode"):
        load_config(path)


windows_only = pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")


@windows_only
def test_dpapi_round_trip_and_file_is_not_plaintext(tmp_path):
    store = DpapiFileStore(tmp_path / "secrets.dpapi")
    assert store.load() == {}
    store.save({"characters": {"1": {"refresh_token": "very-secret-value"}}})
    assert b"very-secret-value" not in (tmp_path / "secrets.dpapi").read_bytes()
    assert store.load()["characters"]["1"]["refresh_token"] == "very-secret-value"


@windows_only
def test_dpapi_rejects_a_damaged_file(tmp_path):
    path = tmp_path / "secrets.dpapi"
    path.write_bytes(b"not a dpapi blob")
    with pytest.raises(SecretStoreError, match="could not be decrypted"):
        DpapiFileStore(path).load()
