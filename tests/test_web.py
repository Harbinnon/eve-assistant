import re
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from eve_assistant import cli, snapshots, store
from eve_assistant.config import Config
from eve_assistant.web import create_app

SHIP, SKILL = 100, 1


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.sqlite3"
    conn = store.connect(path)
    conn.executemany(
        "INSERT INTO sde_types (type_id, name, group_id, published) VALUES (?, ?, 1, 1)",
        [(SKILL, "Mining"), (SHIP, "Big Barge"), (32880, "Venture")],
    )
    conn.executemany(
        "INSERT INTO sde_type_attributes VALUES (?, ?, ?)",
        [
            (SKILL, 275, 1.0),
            (SKILL, 180, 167),
            (SKILL, 181, 168),
            (SHIP, 182, SKILL),
            (SHIP, 277, 2),
        ],  # fmt: skip
    )
    conn.execute("INSERT INTO sde_clone_grade_skills VALUES (1, 'Alpha Test', ?, 4)", (SKILL,))
    conn.execute("INSERT INTO sde_solar_systems VALUES (30000142, 'Jita', 1, 1, 0.946, 'B')")
    conn.execute("INSERT INTO sde_regions VALUES (1, 'The Forge')")
    conn.execute("INSERT INTO sde_meta VALUES (1, 'x', 'y')")
    snapshots.ensure_character(conn, 7, "Test Pilot")
    for kind, payload in {
        "public": {"race_id": 1},
        "attributes": {"charisma": 20, "intelligence": 20, "memory": 20, "perception": 20,
                       "willpower": 20},
        "skills": {"total_sp": 50000, "skills": [
            {"skill_id": SKILL, "trained_skill_level": 1, "active_skill_level": 1,
             "skillpoints_in_skill": 250}]},
        "wallet": 12_345_678.0,
        "location": {"solar_system_id": 30000142, "station_id": 60003760},
        "ship": {"ship_type_id": 32880, "ship_name": "<script>alert(1)</script>"},
        "online": {"online": True},
        "skillqueue": [],
    }.items():  # fmt: skip
        snapshots.record(conn, 7, kind, payload)
    conn.close()
    return path


@pytest.fixture
def client(db_path):
    refresh = mock.Mock(return_value=[])
    app = create_app(db_path, refresh=refresh)
    with TestClient(app) as test_client:
        test_client.refresh_mock = refresh
        yield test_client


def _token(html: str) -> str:
    return re.search(r'name="token" value="([^"]+)"', html).group(1)


def test_characters_page_shows_state_and_escapes_player_text(client):
    page = client.get("/characters")
    assert page.status_code == 200
    assert "Test Pilot" in page.text
    assert "Jita" in page.text and "The Forge" in page.text
    assert "12.3M ISK" in page.text
    assert "<script>alert(1)</script>" not in page.text
    assert "&lt;script&gt;" in page.text
    assert "trademarks or registered trademarks of CCP hf." in page.text


def test_skills_page_shows_plan(client):
    page = client.get("/skills", params={"character": "Test Pilot", "item": "Big Barge"})
    assert page.status_code == 200
    assert "Mining" in page.text and "Omega" in page.text


def test_skills_page_explains_unknown_items(client):
    page = client.get("/skills", params={"character": "Test Pilot", "item": "Big"})
    assert "Did you mean: Big Barge" in page.text


def test_add_and_close_a_goal(client):
    token = _token(client.get("/goals").text)
    added = client.post(
        "/goals",
        data={"token": token, "kind": "ship", "target": "Big Barge", "character": "Test Pilot"},
        follow_redirects=False,
    )
    assert added.status_code == 303
    page = client.get(added.headers["location"])
    assert "Goal added." in page.text and "Fly a Big Barge" in page.text

    goal_id = re.search(r'action="/goals/(\d+)/status"', page.text).group(1)
    closed = client.post(f"/goals/{goal_id}/status", data={"token": token, "status": "done"})
    assert "Goal marked done." in closed.text
    assert "Fly a Big Barge" not in closed.text


def test_bad_goal_input_is_explained(client):
    token = _token(client.get("/goals").text)
    page = client.post("/goals", data={"token": token, "kind": "isk", "target": "lots"})
    assert "Enter a number of ISK." in page.text


def test_forms_reject_wrong_token_and_other_sites(client):
    assert client.post("/refresh", data={"token": "wrong"}).status_code == 403
    token = _token(client.get("/characters").text)
    other_site = client.post(
        "/refresh", data={"token": token}, headers={"origin": "https://evil.example"}
    )
    assert other_site.status_code == 403
    client.refresh_mock.assert_not_called()


def test_refresh_button_runs_refresh(client):
    token = _token(client.get("/characters").text)
    page = client.post("/refresh", data={"token": token})
    client.refresh_mock.assert_called_once()
    assert "Character data refreshed" in page.text


def test_refresh_problems_are_shown(db_path):
    app = create_app(db_path, refresh=lambda: ["Market prices: ESI returned 502."])
    with TestClient(app) as test_client:
        token = _token(test_client.get("/characters").text)
        page = test_client.post("/refresh", data={"token": token})
    assert "ESI returned 502" in page.text


def test_serve_refuses_hosted_mode(capsys):
    hosted = Config(client_id="x", callback_url="http://localhost:1/c", contact=None, mode="hosted")
    with mock.patch.object(cli, "load_config", return_value=hosted):
        assert cli.main(["serve"]) == 2
    assert "only runs locally" in capsys.readouterr().out
