"""New scopes (roadmap 3.2): skipped until a character logs in again, and said so."""

from types import SimpleNamespace

from fastapi.testclient import TestClient

from eve_assistant import snapshots, store
from eve_assistant.esi import SCOPES
from eve_assistant.web import create_app

NEW = ("esi-characters.read_freelance_jobs.v1", "esi-characters.read_loyalty.v1",
       "esi-killmails.read_killmails.v1")  # fmt: skip


def test_the_new_scopes_are_requested():
    assert set(NEW) <= set(SCOPES)


class _FakeEsi:
    def __init__(self):
        self.paths = []

    def get(self, path, character_id=None, remember=True):
        self.paths.append(path)
        return SimpleNamespace(data=[])


def test_kinds_needing_a_new_scope_wait_for_the_login(monkeypatch):
    kind = snapshots.Kind("loyalty", "/characters/{id}/loyalty/points", scope=NEW[1])
    monkeypatch.setattr(snapshots, "KINDS", (kind,))
    db = store.connect(":memory:")
    snapshots.ensure_character(db, 7, "Miner")
    old_login = _FakeEsi()
    assert snapshots.collect(db, old_login, 7, granted=("esi-skills.read_skills.v1",)) == []
    assert old_login.paths == []  # skipped, not failed
    new_login = _FakeEsi()
    snapshots.collect(db, new_login, 7, granted=SCOPES)
    assert new_login.paths == ["/characters/7/loyalty/points"]
    unknown = _FakeEsi()
    snapshots.collect(db, unknown, 7)  # no scope list given: tried as before
    assert unknown.paths == ["/characters/7/loyalty/points"]
    db.close()


def _characters_page(tmp_path, missing):
    path = tmp_path / "s.sqlite3"
    conn = store.connect(path)
    snapshots.ensure_character(conn, 7, "Miner")
    conn.close()
    with TestClient(
        create_app(path, missing_scopes=lambda: missing), base_url="http://127.0.0.1"
    ) as c:
        return c.get("/characters").text


def test_characters_says_who_must_log_in_again(tmp_path):
    page = _characters_page(tmp_path, {7: NEW})
    assert "1 character needs to log in again" in page
    assert "tick them on your ESI application" in page
    assert '<span class="pill amber">log in again</span>' in page
    assert "log in again" not in _characters_page(tmp_path / "x", {7: ()}).lower().split("<main")[1]
