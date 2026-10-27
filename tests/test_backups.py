import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from eve_assistant import backups, cli, config, refresh, sde, snapshots, store
from eve_assistant.config import Config, load_config
from eve_assistant.web import create_app

NOW = datetime(2026, 10, 17, 18, 0, tzinfo=UTC)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "live" / "eve-assistant.sqlite3"
    conn = store.connect(path)
    snapshots.ensure_character(conn, 7, "Miner")
    snapshots.record(conn, 7, "wallet", 1_000_000.0)
    conn.execute(
        "INSERT INTO sde_types (type_id, name, group_id, published) VALUES (1, 'Ore', 1, 1)"
    )
    conn.execute(
        "INSERT INTO sde_meta (build_number, release_date, loaded_at, loader_version) "
        "VALUES (1, 'x', 'y', ?)",
        (sde.LOADER_VERSION,),
    )
    conn.execute("INSERT INTO esi_cache VALUES ('k', NULL, 0, '{}')")
    conn.commit()
    conn.close()
    return path


def _open(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def test_a_backup_keeps_history_and_leaves_out_what_can_be_downloaded(db_path, tmp_path):
    made = backups.make(db_path, tmp_path / "backups", NOW)
    assert made.path.name == "eve-assistant-20261017-180000.sqlite3"
    copy = _open(made.path)
    assert copy.execute("SELECT name FROM characters").fetchone()["name"] == "Miner"
    assert copy.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 1
    for table in ("sde_types", "sde_meta", "esi_cache"):
        assert copy.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0  # noqa: S608
    copy.close()
    assert not list((tmp_path / "backups").glob("*.partial"))


def test_the_default_folder_is_next_to_the_database(db_path):
    made = backups.make(db_path, None, NOW)
    assert made.path.parent == db_path.parent / "backups"


def test_retention_keeps_recent_days_and_one_per_month(db_path, tmp_path):
    folder = tmp_path / "backups"
    folder.mkdir()
    times = [NOW - timedelta(days=d) for d in range(40)] + [NOW - timedelta(hours=2)]
    times += [NOW - timedelta(days=31 * m) for m in range(2, 16)]
    for when in times:
        (folder / f"eve-assistant-{when:%Y%m%d-%H%M%S}.sqlite3").write_bytes(b"x")
    (folder / "notes.txt").write_text("not a backup", encoding="utf-8")
    backups.prune(folder, NOW)
    kept = backups.list_backups(folder)
    days = [b.taken_at for b in kept if (NOW - b.taken_at).days < backups.KEEP_DAILY]
    assert len(days) == backups.KEEP_DAILY  # one per day, the newest of each
    assert NOW - timedelta(hours=2) not in days  # same day as NOW: only the newest stays
    months = {f"{b.taken_at:%Y%m}" for b in kept}
    assert len(months) == backups.KEEP_MONTHLY
    assert (folder / "notes.txt").exists()


def test_due_once_a_day_and_problems_are_reported(db_path, tmp_path):
    folder = tmp_path / "backups"
    assert backups.due(folder, NOW)
    assert backups.run_if_due(folder, db_path, NOW) == []
    assert not backups.due(folder, NOW + timedelta(hours=5))
    assert backups.due(folder, NOW + timedelta(days=1))
    broken = backups.run_if_due(tmp_path / "x", tmp_path / "nothing" / "here.sqlite3", NOW)
    assert broken[0].startswith("Daily backup failed")


def test_restore_moves_the_current_database_aside(db_path, tmp_path):
    made = backups.make(db_path, tmp_path / "backups", NOW)
    conn = store.connect(db_path)
    snapshots.ensure_character(conn, 8, "Added after the backup")
    conn.close()

    aside = backups.restore(made.path, db_path, NOW + timedelta(hours=1))
    assert aside.name == "eve-assistant.before-restore-20261017-190000.sqlite3"
    assert _open(aside).execute("SELECT COUNT(*) FROM characters").fetchone()[0] == 2
    restored = store.connect(db_path)
    assert [r["name"] for r in restored.execute("SELECT name FROM characters")] == ["Miner"]
    assert sde.loaded_build(restored) is None  # reload the static data next
    restored.close()


def test_restore_refuses_files_that_are_not_backups(db_path, tmp_path):
    other = tmp_path / "other.sqlite3"
    sqlite3.connect(other).execute("CREATE TABLE t (x)").connection.close()
    with pytest.raises(backups.BackupError, match="isn't an EVE Assistant database"):
        backups.restore(other, db_path)
    with pytest.raises(backups.BackupError, match="not found"):
        backups.restore(tmp_path / "missing.sqlite3", db_path)
    assert _open(db_path).execute("SELECT COUNT(*) FROM characters").fetchone()[0] == 1


def test_a_failed_restore_puts_the_original_back(db_path, tmp_path, monkeypatch):
    made = backups.make(db_path, tmp_path / "backups", NOW)

    def broken_connect(*_args, **_kwargs):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(backups.store, "connect", broken_connect)
    with pytest.raises(sqlite3.OperationalError):
        backups.restore(made.path, db_path, NOW)
    assert _open(db_path).execute("SELECT name FROM characters").fetchone()[0] == "Miner"
    assert not list(db_path.parent.glob("*.before-restore-*"))


def test_the_background_refresh_makes_the_daily_backup(db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(refresh.auth, "list_characters", lambda _s: [])
    config = Config("abc", "http://localhost", None, "local", backups_dir=tmp_path / "b")
    refresh.auto_refresh_once(config, None, db_path, now=NOW)
    assert len(backups.list_backups(tmp_path / "b")) == 1


def test_cli_backup_list_and_restore(db_path, tmp_path, monkeypatch, capsys):
    folder = tmp_path / "cli-backups"
    monkeypatch.setattr(store, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(cli, "_backup_folder", lambda: folder)
    assert cli.main(["backup"]) == 0
    assert "Backup saved" in capsys.readouterr().out
    assert cli.main(["backups"]) == 0
    listed = capsys.readouterr().out
    assert "eve-assistant-" in listed
    made = backups.list_backups(folder)[0].path
    assert cli.main(["restore", str(made)]) == 2  # needs --confirm
    assert "--confirm" in capsys.readouterr().out
    assert cli.main(["restore", str(made), "--confirm"]) == 0
    assert "sde-update" in capsys.readouterr().out


def test_settings_page_shows_backups(db_path, tmp_path):
    folder = tmp_path / "shown"
    app = create_app(db_path, backups_dir=folder)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert "No backup yet" in client.get("/preferences").text
        backups.make(db_path, folder, NOW)
        page = client.get("/preferences").text
    assert "Last backup 2026-10-17 18:00 UTC" in page
    assert "1 kept in" in page


def test_backup_folder_setting(tmp_path):
    path = tmp_path / "local.toml"
    path.write_text(
        '[esi]\nclient_id = "abc"\n[backups]\nfolder = "' + tmp_path.as_posix() + '/b"\n',
        encoding="utf-8",
    )
    assert load_config(path).backups_dir == tmp_path / "b"
    path.write_text('[esi]\nclient_id = "abc"\n', encoding="utf-8")
    assert load_config(path).backups_dir is None


def test_backups_are_ignored_by_git():
    gitignore = (config.PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "var/" in gitignore  # backups default to var/backups
