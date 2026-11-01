"""send-logs on a PC with no dashboard: the characters come from config."""

from types import SimpleNamespace

import pytest

from eve_assistant import cli, config, snapshots, store

BASE = '[esi]\nclient_id = "abc"\n'


def _config(tmp_path, send_logs=""):
    path = tmp_path / "local.toml"
    path.write_text(BASE + send_logs, encoding="utf-8")
    return config.load_config(path)


def test_characters_are_read_trimmed_once_each_without_placeholders(tmp_path):
    names = '["Miner One", " Hauler Two ", "<your alt>", "Miner One"]'
    loaded = _config(tmp_path, f"[send_logs]\ncharacters = {names}\n")
    assert loaded.send_logs_characters == ("Miner One", "Hauler Two")
    assert _config(tmp_path).send_logs_characters == ()


@pytest.mark.parametrize("value", ['"Miner One"', "[1, 2]"])
def test_characters_must_be_a_list_of_names(tmp_path, value):
    with pytest.raises(config.ConfigError, match=r"\[send_logs\] characters"):
        _config(tmp_path, f"[send_logs]\ncharacters = {value}\n")


def test_listed_characters_need_no_database_and_none_is_made(tmp_path):
    loaded = _config(tmp_path, '[send_logs]\ncharacters = ["Miner One"]\n')
    missing = tmp_path / "var" / "eve-assistant.sqlite3"
    assert cli._send_logs_owners(loaded, missing) == {"Miner One"}
    assert not missing.exists()


def test_characters_logged_in_on_this_pc_count_too(tmp_path):
    path = tmp_path / "eve-assistant.sqlite3"
    with store.connect(path) as db:
        snapshots.ensure_character(db, 2112000001, "Logged In Here")
    db.close()
    listed = _config(tmp_path, '[send_logs]\ncharacters = ["Miner One"]\n')
    assert cli._send_logs_owners(listed, path) == {"Miner One", "Logged In Here"}
    assert cli._send_logs_owners(_config(tmp_path), path) == {"Logged In Here"}


def test_with_no_characters_anywhere_it_says_how_to_list_them(tmp_path, monkeypatch, capsys):
    logs = f'[logs]\ngamelogs_dir = "{tmp_path.as_posix()}"\n'
    loaded = _config(tmp_path, f'[send_logs]\nurl = "http://10.9.8.21:8700"\n{logs}')
    missing = tmp_path / "var" / "eve-assistant.sqlite3"
    monkeypatch.setattr(cli, "load_config", lambda: loaded)
    monkeypatch.setattr(cli, "default_store", lambda: object())
    monkeypatch.setattr(cli.logsender, "token", lambda _store: "tok")
    monkeypatch.setattr(store, "DEFAULT_DB_PATH", missing)
    assert cli._cmd_send_logs(SimpleNamespace(setup=False, watch=None)) == 1
    assert "[send_logs] characters" in capsys.readouterr().out
    assert not missing.exists()
