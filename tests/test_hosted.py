"""Hosted mode (roadmap 7.2): the address rule, and the backup state file."""

import hashlib
import json
from datetime import UTC, datetime

import pytest

from eve_assistant import backups, hosted, snapshots, store

PROXY, HOME = "10.9.8.1", "10.9.8.0/24"  # made up: the real ones live in the server's config


@pytest.mark.parametrize(
    ("path", "client", "ok"),
    [
        ("/characters", PROXY, True),  # through the proxy and its sign-in
        ("/characters", "10.9.8.50", False),  # the home network can't skip the sign-in
        ("/ingest/gamelogs", "10.9.8.50", True),  # the gaming PC, straight in
        ("/ingest/gamelogs", PROXY, False),  # never through the proxy (the internet)
        ("/ingest", PROXY, False),
        ("/ingest/gamelogs", "192.0.2.7", False),  # nowhere else
        ("/ingestion", "10.9.8.50", False),  # not the ingest route
        ("/characters", None, False),
        ("/characters", "not an address", False),
    ],
)
def test_who_may_reach_what(path, client, ok):
    assert hosted.allowed(path, client, PROXY, HOME) is ok


def test_each_backup_leaves_a_state_file_to_check_it_by(tmp_path):
    db_path = tmp_path / "eve.sqlite3"
    conn = store.connect(db_path)
    snapshots.ensure_character(conn, 7, "Miner")
    snapshots.record(conn, 7, "wallet", 1.0)
    conn.close()
    now = datetime(2026, 10, 19, 3, 0, tzinfo=UTC)
    made = backups.make(db_path, tmp_path / "backups", now)
    state = json.loads((tmp_path / "backups" / "latest.json").read_text(encoding="utf-8"))
    assert state["completed_at"] == "2026-10-19T03:00:00+00:00"
    assert state["file"] == made.path.name
    assert state["bytes"] == made.path.stat().st_size
    assert state["sha256"] == hashlib.sha256(made.path.read_bytes()).hexdigest()
    assert state["rows"]["snapshots"] == 1
    assert state["rows"]["plans"] == 0
    assert state["rows"]["schema_version"] >= 24
    assert not (tmp_path / "backups" / "latest.json.tmp").exists()
