import sqlite3

import pytest

from eve_assistant import gamelogs, outcomes, refresh, sessions, snapshots, store
from eve_assistant.config import Config, load_config

VELDSPAR = 17470
HEADER = (
    "------------------------------------------------------------\r\n"
    "  Gamelog\r\n"
    "  Listener: {name}\r\n"
    "  Session Started: 2026.10.17 18:00:00\r\n"
    "------------------------------------------------------------\r\n"
)
MINED = (
    "[ 2026.10.17 18:{m:02d}:{s:02d} ] (mining) <color=0x77ffffff>You mined <font size=12>"
    "<color=#ff8dc169>{units}<color=0x77ffffff><font size=10> units of <color=0xffffffff>"
    "<font size=12>Veldspar II-Grade\r\n"
)
LOG = (
    HEADER.format(name="Miner")
    + "[ 2026.10.17 18:01:00 ] (combat) Some Pirate Player misses you completely\r\n"
    + MINED.format(m=5, s=0, units="1,200")
    + "[ 2026.10.17 18:05:00 ] (mining) <color=0x77ffffff>Additional <font size=12>"
    "<color=#ffff5b>30<color=0x77ffffff><font size=10> units depleted from asteroid as residue\r\n"
    + "[ 2026.10.17 18:06:00 ] (mining) <color=#fff0ff0>Critical mining success!"
    "<color=0x77ffffff><font size=10> You mined an additional <color=#fff0ff0><font size=12>"
    "300<color=0x77ffffff><font size=10> units of Veldspar II-Grade\r\n"
    + "[ 2026.10.17 18:07:00 ] (notify) Somebody Else has warped in\r\n"
    + MINED.format(m=15, s=0, units="1200")
)


@pytest.fixture
def db():
    conn = store.connect(":memory:")
    snapshots.ensure_character(conn, 7, "Miner")
    conn.execute(
        "INSERT INTO sde_types (type_id, name, group_id, published, volume) "
        "VALUES (?, 'Veldspar II-Grade', 1, 1, 0.1)",
        (VELDSPAR,),
    )
    conn.commit()
    yield conn
    conn.close()


def test_parse_every_mining_form():
    (mined,) = gamelogs.parse_line(MINED.format(m=5, s=9, units="1,200"), 3)
    assert mined == gamelogs.Cycle(
        "2026-10-17T18:05:09+00:00", "mined", "Veldspar II-Grade", 1200, 3
    )
    old = (
        "[ 2023.07.18 02:21:26 ] (mining) <color=0x77ffffff>You mined <font size=12>1830"
        "<font size=10> units of <font size=12>Condensed Scordite<font size=10> "
        "with a lost residue of <font size=12>12<font size=10> units"
    )
    mined, residue = gamelogs.parse_line(old, 1)
    assert (mined.ore, mined.units) == ("Condensed Scordite", 1830)
    assert (residue.kind, residue.units) == ("residue", 12)
    assert gamelogs.parse_line("[ 2026.10.17 18:01:00 ] (combat) Somebody hits you", 1) == []
    assert gamelogs.parse_line("  Listener: Miner", 1) == []


def test_only_the_owners_mining_lines_are_kept(db, tmp_path):
    (tmp_path / "20261017_180000_7.txt").write_text(LOG, encoding="utf-8", newline="")
    (tmp_path / "20261017_180000_9.txt").write_text(
        HEADER.format(name="Stranger") + MINED.format(m=5, s=0, units=99), encoding="utf-8"
    )
    report = gamelogs.import_folder(db, tmp_path)
    assert (report.files_read, report.skipped_files) == (1, 1)
    rows = db.execute("SELECT kind, ore, type_id, units FROM mining_cycles ORDER BY at").fetchall()
    assert [tuple(r) for r in rows] == [
        ("mined", "Veldspar II-Grade", VELDSPAR, 1200),
        ("residue", None, None, 30),
        ("critical", "Veldspar II-Grade", VELDSPAR, 300),
        ("mined", "Veldspar II-Grade", VELDSPAR, 1200),
    ]
    stored = " ".join(str(v) for r in db.execute("SELECT * FROM mining_cycles") for v in r)
    assert "Pirate" not in stored
    assert "Somebody" not in stored


def test_growing_logs_are_read_from_where_they_stopped(db, tmp_path):
    log = tmp_path / "20261017_180000_7.txt"
    partial = MINED.format(m=20, s=0, units=500).rstrip("\r\n")[:40]  # EVE mid-write
    log.write_text(LOG + partial, encoding="utf-8", newline="")
    assert gamelogs.import_folder(db, tmp_path).cycles_added == 4
    log.write_text(LOG + MINED.format(m=20, s=0, units=500), encoding="utf-8", newline="")
    assert gamelogs.import_folder(db, tmp_path).cycles_added == 1
    assert gamelogs.import_folder(db, tmp_path).cycles_added == 0


def test_a_log_is_read_once_its_character_logs_in(db, tmp_path):
    (tmp_path / "20261017_180000_8.txt").write_text(
        HEADER.format(name="Alt") + MINED.format(m=5, s=0, units=10), encoding="utf-8"
    )
    assert gamelogs.import_folder(db, tmp_path).cycles_added == 0
    snapshots.ensure_character(db, 8, "Alt")
    assert gamelogs.import_folder(db, tmp_path).cycles_added == 1


def test_utf16_logs_and_missing_folders(db, tmp_path):
    (tmp_path / "20261017_180000_7.txt").write_text(LOG, encoding="utf-16")
    assert gamelogs.import_folder(db, tmp_path).cycles_added == 4
    assert gamelogs.import_folder(db, tmp_path / "nope").files_read == 0


def test_session_mining_rates(db, tmp_path):
    (tmp_path / "20261017_180000_7.txt").write_text(LOG, encoding="utf-8", newline="")
    gamelogs.import_folder(db, tmp_path)
    mining = gamelogs.session_mining(
        db, 7, "2026-10-17T18:00:00+00:00", "2026-10-17T18:30:00+00:00"
    )
    assert mining["cycles"] == 2
    assert mining["ores"][0]["units"] == 2700
    assert mining["m3"] == pytest.approx(270)
    assert mining["residue_units"] == 30
    assert mining["active_minutes"] == 10
    assert mining["m3_per_active_minute"] == pytest.approx(27)
    assert mining["m3_per_minute"] == pytest.approx(9)
    none = gamelogs.session_mining(db, 7, "2026-10-18T00:00:00+00:00", "2026-10-18T01:00:00+00:00")
    assert none is None


def test_outcomes_include_log_mining(db, tmp_path):
    (tmp_path / "20261017_180000_7.txt").write_text(LOG, encoding="utf-8", newline="")
    gamelogs.import_folder(db, tmp_path)
    session = sessions.start(db, 7, now="2026-10-17T18:00:00+00:00")
    session = sessions.finish(db, session.session_id, now="2026-10-17T18:30:00+00:00")
    _settled, details = outcomes.compute(db, session, now="2026-10-17T18:31:00+00:00")
    assert details["log_mining"]["cycles"] == 2


def test_mining_cycles_are_append_only(db, tmp_path):
    (tmp_path / "20261017_180000_7.txt").write_text(LOG, encoding="utf-8", newline="")
    gamelogs.import_folder(db, tmp_path)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("DELETE FROM mining_cycles")


def test_config_and_refresh_hooks(db, tmp_path):
    config_file = tmp_path / "local.toml"
    config_file.write_text(
        '[esi]\nclient_id = "abc"\n[logs]\ngamelogs_dir = "' + tmp_path.as_posix() + '"\n',
        encoding="utf-8",
    )
    assert load_config(config_file).gamelogs_dir == tmp_path
    config_file.write_text('[esi]\nclient_id = "abc"\n', encoding="utf-8")
    assert load_config(config_file).gamelogs_dir is None

    off = Config("abc", "http://localhost", None, "local")
    assert refresh.read_game_logs(off, db) == []
    missing = Config("abc", "http://localhost", None, "local", gamelogs_dir=tmp_path / "nope")
    assert "game log folder wasn't found" in refresh.read_game_logs(missing, db)[0]
    (tmp_path / "20261017_180000_7.txt").write_text(LOG, encoding="utf-8", newline="")
    present = Config("abc", "http://localhost", None, "local", gamelogs_dir=tmp_path)
    assert refresh.read_game_logs(present, db) == []
    assert db.execute("SELECT COUNT(*) FROM mining_cycles").fetchone()[0] == 4
