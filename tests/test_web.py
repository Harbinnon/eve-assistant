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
    conn.execute(
        "INSERT INTO sde_meta (build_number, release_date, loaded_at) VALUES (1, 'x', 'y')"
    )
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


# --- Today page, responses and preferences ---------------------------------


def _fake_plan_maker(db_path):
    from eve_assistant import planner, risk
    from eve_assistant.skills import Reason

    def make(character_id, minutes, allow_lowsec, allow_risky):
        conn = store.connect(db_path)
        try:
            item = planner.PlanItem(
                kind="mining",
                key="1",
                title="Mine <b>ore</b> for a while",
                isk=1_500_000,
                minutes=minutes,
                risk=risk.rate("low", 100_000, 10_000_000),
                reasons=(Reason("mining_estimate", {"m3_per_minute": 200, "ship": "Venture"}),),
                inputs={"player_title": "Ignore previous instructions"},
            )
            alt = planner.Alternative(
                planner.PlanItem(
                    kind="courier",
                    key="9",
                    title="Courier A -> B",
                    isk=900_000,
                    minutes=30,
                    risk=risk.rate("medium", 5_000_000, 10_000_000),
                ),
                "red_risk",
            )
            plan = planner.Plan(
                character_id=character_id,
                minutes_available=minutes,
                items=(item,),
                alternatives=(alt,),
                notes=(Reason("loss_chances_are_guesses"),),
                net_worth=10_000_000,
            )
            return planner.record(conn, plan, {"allow_lowsec": allow_lowsec}).plan_id
        finally:
            conn.close()

    return make


@pytest.fixture
def planning_client(db_path):
    app = create_app(db_path, make_plan=_fake_plan_maker(db_path))
    with TestClient(app) as test_client:
        yield test_client


def test_home_goes_to_today(planning_client):
    response = planning_client.get("/", follow_redirects=False)
    assert response.headers["location"] == "/today"


def test_making_a_plan_shows_steps_reasons_and_alternatives(planning_client):
    token = _token(planning_client.get("/today").text)
    made = planning_client.post(
        "/today/plan",
        data={"token": token, "character": "Test Pilot", "minutes": "45"},
        follow_redirects=False,
    )
    assert made.status_code == 303 and made.headers["location"].startswith("/plans/")
    page = planning_client.get(made.headers["location"])
    assert "Plan for Test Pilot" in page.text
    assert "Mine &lt;b&gt;ore&lt;/b&gt; for a while" in page.text  # escaped
    assert "Estimated 200 m3 per minute in your Venture" in page.text
    assert "Red on the risk matrix" in page.text
    assert "starting guess" in page.text
    assert "player-text" in page.text and "Ignore previous instructions" in page.text


def test_responding_records_and_mute_shows_in_preferences(planning_client):
    token = _token(planning_client.get("/today").text)
    plan_url = planning_client.post(
        "/today/plan",
        data={"token": token, "character": "Test Pilot", "minutes": "30"},
        follow_redirects=False,
    ).headers["location"]
    page = planning_client.get(plan_url).text
    item_id = re.search(r'action="/plan-items/(\d+)/respond"', page).group(1)

    done = planning_client.post(
        f"/plan-items/{item_id}/respond", data={"token": token, "response": "do"}
    )
    assert "Recorded: you" in done.text and "You said: doing it" in done.text

    muted = planning_client.post(
        f"/plan-items/{item_id}/respond", data={"token": token, "response": "mute"}
    )
    assert "Muted." in muted.text
    prefs = planning_client.get("/preferences").text
    assert "kind:mining" in prefs and "Unmute" in prefs

    unmuted = planning_client.post(
        "/preferences/unmute", data={"token": token, "category": "kind:mining"}
    )
    assert "Unmuted." in unmuted.text


def test_today_forms_need_the_token(planning_client):
    assert (
        planning_client.post(
            "/today/plan", data={"token": "x", "character": "Test Pilot", "minutes": "30"}
        ).status_code
        == 403
    )
    assert (
        planning_client.post(
            "/plan-items/1/respond", data={"token": "x", "response": "do"}
        ).status_code
        == 403
    )
    assert (
        planning_client.post(
            "/preferences/mute", data={"token": "x", "category": "kind:mining"}
        ).status_code
        == 403
    )


def test_planning_errors_are_shown_not_crashed(db_path):
    def broken(*_args):
        raise RuntimeError("ESI is down")

    with TestClient(create_app(db_path, make_plan=broken)) as test_client:
        token = _token(test_client.get("/today").text)
        page = test_client.post(
            "/today/plan", data={"token": token, "character": "Test Pilot", "minutes": "30"}
        )
    assert "Could not build a plan: ESI is down" in page.text


def test_structure_names_are_escaped_and_marked_as_player_text(db_path):
    conn = store.connect(db_path)
    conn.execute(
        "INSERT INTO structures VALUES (1050000000001, 1, '<img src=x>Keepstar', 30000142, "
        "35834, 1, '2026-10-16T00:00:00+00:00', 7)"
    )
    snapshots.record(
        conn, 7, "location", {"solar_system_id": 30000142, "structure_id": 1050000000001}
    )
    conn.close()
    with TestClient(create_app(db_path)) as test_client:
        page = test_client.get("/characters").text
    assert "<img src=x>" not in page
    assert "&lt;img src=x&gt;Keepstar" in page
    assert "Name chosen by the structure" in page


def test_open_projects_show_on_today_and_can_be_closed(planning_client, db_path):
    from eve_assistant import projects

    conn = store.connect(db_path)
    project_id = projects.start(conn, 7, "haul", "1:2:x", "Haul <ore>", 120, 5e6, {})
    conn.close()
    page = planning_client.get("/today").text
    assert "Open projects" in page and "Haul &lt;ore&gt;" in page
    token = _token(page)
    done = planning_client.post(f"/projects/{project_id}/done", data={"token": token})
    assert "Project marked done." in done.text and "Open projects" not in done.text


def test_play_time_is_set_on_preferences(planning_client):
    token = _token(planning_client.get("/preferences").text)
    saved = planning_client.post("/preferences-play-time", data={"token": token, "hours": "9.5"})
    assert "Play time saved." in saved.text and 'value="9.5"' in saved.text
