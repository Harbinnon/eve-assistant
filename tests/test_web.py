import re
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from eve_assistant import cli, snapshots, store, train_next
from eve_assistant.config import Config
from eve_assistant.web import create_app

SHIP, SKILL = 100, 1
LOCAL = "http://127.0.0.1"


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.sqlite3"
    conn = store.connect(path)
    conn.executemany(
        "INSERT INTO sde_types (type_id, name, group_id, published) VALUES (?, ?, ?, 1)",
        [(SKILL, "Mining", 1), (SHIP, "Big Barge", 2), (32880, "Venture", 2)],
    )
    conn.executemany(
        "INSERT INTO sde_groups VALUES (?, ?, ?, 1)", [(1, 16, "Skills"), (2, 6, "Ships")]
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
    with TestClient(app, base_url=LOCAL) as test_client:
        test_client.refresh_mock = refresh
        yield test_client


def _token(html: str) -> str:
    return re.search(r'name="token" value="([^"]+)"', html).group(1)


def test_characters_page_shows_state_and_escapes_player_text(client):
    page = client.get("/characters")
    assert page.status_code == 200
    assert "Test Pilot" in page.text
    assert "Jita" in page.text
    assert "The Forge" in page.text
    assert "12.3M ISK" in page.text
    assert "<script>alert(1)</script>" not in page.text
    assert "&lt;script&gt;" in page.text
    assert "trademarks or registered trademarks of CCP hf." in page.text


def test_characters_and_goals_show_net_worth(client, db_path):
    conn = store.connect(db_path)
    conn.execute("INSERT INTO market_prices VALUES (32880, 2000000, NULL, 'x')")
    conn.execute(
        "INSERT INTO snapshots (character_id, kind, taken_at, content_hash, payload) "
        "VALUES (7, 'wallet', '2026-01-01T00:00:00+00:00', 'h', '10000000')"
    )
    snapshots.record(conn, 7, "assets", [{"type_id": 32880, "quantity": 1}])
    conn.close()
    page = client.get("/characters").text
    assert "Net worth" in page and "14.3M ISK" in page  # 12.3M wallet + a 2M Venture
    assert '<svg class="worth-line"' in page and "<polyline points=" in page
    client.post(
        "/goals",
        data={"token": _token(page), "kind": "isk", "target": "50000000", "character": ""},
        headers={"Origin": LOCAL},
    )
    goals_page = client.get("/goals").text
    assert "Net worth" in goals_page and "14.3M ISK" in goals_page  # all characters


def test_skills_page_shows_plan(client):
    page = client.get("/skills", params={"character": "Test Pilot", "item": "Big Barge"})
    assert page.status_code == 200
    assert "Mining" in page.text
    assert "Omega" in page.text


def test_skills_page_suggests_what_to_train_next(client, monkeypatch):
    found = [
        train_next.Suggestion("Astrogeology", 1, 90.0, 12.5, 1_200_000.0, 0.4, 90_000.0),
        train_next.Suggestion("Mining", 5, 9000.0, 8.0, None, None, None, needs_omega=True),
    ]
    monkeypatch.setattr(train_next, "suggestions", lambda db, cid, limit=3: found)
    monkeypatch.setattr(train_next, "goal_for", lambda db, cid: "Have 101M ISK")
    page = client.get("/skills").text
    assert "Train this next · Test Pilot" in page
    assert "Astrogeology 1" in page and "+12.5 m³/min" in page and "+1.2M" in page
    assert "2.8 days" in page  # 0.4 weeks
    assert "pays back in 0.1 h" in page
    assert "needs Omega" in page and "these times are as Omega" in page
    assert 'name="level" value="5"' in page  # Set as goal, per skill
    assert "Have 101M ISK" in page


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
    assert "Goal added." in page.text
    assert "Fly a Big Barge" in page.text

    goal_id = re.search(r'action="/goals/(\d+)/status"', page.text).group(1)
    closed = client.post(f"/goals/{goal_id}/status", data={"token": token, "status": "done"})
    assert "Goal marked done." in closed.text
    assert "Fly a Big Barge" not in closed.text


def test_isk_goal_shows_progress(client):
    token = _token(client.get("/goals").text)
    client.post(
        "/goals",
        data={"token": token, "kind": "isk", "target": "50000000", "character": "Test Pilot"},
    )
    page = client.get("/goals").text
    assert 'aria-label="25% of the target"' in page  # 12.3M of 50M
    assert '<progress class="isk"' in page


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
    with TestClient(app, base_url=LOCAL) as test_client:
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
    with TestClient(app, base_url=LOCAL) as test_client:
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
    assert made.status_code == 303
    assert made.headers["location"].startswith("/plans/")
    page = planning_client.get(made.headers["location"])
    assert "Plan for Test Pilot" in page.text
    assert "Mine &lt;b&gt;ore&lt;/b&gt; for a while" in page.text  # escaped
    assert "Estimated 200 m3 per minute in your Venture" in page.text
    assert "Rated Risky. Tick “Include risky options”" in page.text
    assert "starting guess" in page.text
    assert "player-text" in page.text
    assert "Ignore previous instructions" in page.text


def test_responding_records_and_mute_shows_in_preferences(planning_client):
    token = _token(planning_client.get("/today").text)
    plan_url = planning_client.post(
        "/today/plan",
        data={"token": token, "character": "Test Pilot", "minutes": "30"},
        follow_redirects=False,
    ).headers["location"]
    page = planning_client.get(plan_url).text
    item_id = re.search(r'action="/plan-items/(\d+)/respond"', page).group(1)

    refused = planning_client.post(
        f"/plan-items/{item_id}/respond", data={"token": token, "response": "do"}
    )
    assert refused.status_code == 400  # "Start this plan" is the one commitment
    assert "Start this plan" in refused.text

    muted = planning_client.post(
        f"/plan-items/{item_id}/respond", data={"token": token, "response": "mute"}
    )
    assert "Muted." in muted.text
    prefs = planning_client.get("/settings").text
    assert "kind:mining" in prefs
    assert "Play time: 6.0 hours a week, your setting" in prefs
    assert "Unmute" in prefs
    learning = planning_client.get("/history").text  # learning lives with History now
    assert "What it has learned" in learning
    assert "Mining speed: not yet checked" in learning
    assert "assumed 10% are not finished" in learning

    unmuted = planning_client.post(
        "/settings/unmute", data={"token": token, "category": "kind:mining"}
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
            "/settings/mute", data={"token": "x", "category": "kind:mining"}
        ).status_code
        == 403
    )


def test_planning_errors_are_shown_not_crashed(db_path):
    def broken(*_args):
        raise RuntimeError("ESI is down")

    with TestClient(create_app(db_path, make_plan=broken), base_url=LOCAL) as test_client:
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
    with TestClient(create_app(db_path), base_url=LOCAL) as test_client:
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
    assert "Open projects" in page
    assert "Haul &lt;ore&gt;" in page
    assert '<progress value="0.0" max="120.0"' in page
    assert 'aria-label="0 of 120 minutes done"' in page
    token = _token(page)
    done = planning_client.post(f"/projects/{project_id}/done", data={"token": token})
    assert "Project marked done." in done.text
    assert "Open projects" not in done.text


def test_play_time_is_set_on_preferences(planning_client):
    token = _token(planning_client.get("/settings").text)
    saved = planning_client.post("/settings/play-time", data={"token": token, "hours": "9.5"})
    assert "Play time saved." in saved.text
    assert 'value="9.5"' in saved.text


# --- security hardening -------------------------------------------------------


@pytest.mark.parametrize("host", ["evil.example", "127.0.0.1.evil.example", "rebind.attacker.com"])
def test_requests_for_other_host_names_are_refused(db_path, host):
    # DNS rebinding: the browser sends the attacker's domain in the Host header.
    with TestClient(create_app(db_path), base_url=LOCAL) as test_client:
        response = test_client.get("/characters", headers={"host": host})
    assert response.status_code == 400
    assert "Test Pilot" not in response.text


@pytest.mark.parametrize("host", ["127.0.0.1:8700", "localhost:8700", "localhost"])
def test_local_host_names_are_served(db_path, host):
    with TestClient(create_app(db_path), base_url=LOCAL) as test_client:
        assert test_client.get("/characters", headers={"host": host}).status_code == 200


def test_security_headers_on_pages_and_static_files(client):
    for path in ("/characters", "/static/style.css"):
        headers = client.get(path).headers
        assert "default-src 'none'" in headers["content-security-policy"]
        assert "frame-ancestors 'none'" in headers["content-security-policy"]
        assert headers["x-frame-options"] == "DENY"
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["referrer-policy"] == "same-origin"
        assert headers["cache-control"] == "no-store"


def test_pages_have_no_inline_styles_or_scripts(planning_client):
    # The Content-Security-Policy blocks both, so none may be used.
    token = _token(planning_client.get("/today").text)
    plan_url = planning_client.post(
        "/today/plan",
        data={"token": token, "character": "Test Pilot", "minutes": "30"},
        follow_redirects=False,
    ).headers["location"]
    for path in (
        "/today",
        plan_url,
        "/characters",
        "/goals",
        "/skills",
        "/preferences",
        "/history",
    ):
        page = planning_client.get(path).text
        assert "<script" not in page, path
        assert "style=" not in page, path
        assert "<style" not in page, path


@pytest.fixture
def session_client(db_path):
    snapshot = mock.Mock(return_value=[])
    app = create_app(db_path, make_plan=_fake_plan_maker(db_path), snapshot=snapshot)
    with TestClient(app, base_url=LOCAL) as test_client:
        test_client.snapshot_mock = snapshot
        yield test_client


def _new_plan(client) -> tuple[str, str]:
    token = _token(client.get("/today").text)
    plan_url = client.post(
        "/today/plan",
        data={"token": token, "character": "Test Pilot", "minutes": "60"},
        follow_redirects=False,
    ).headers["location"]
    return token, plan_url


def test_start_a_plan_then_finish_it(session_client):
    token, plan_url = _new_plan(session_client)
    plan_id = plan_url.rsplit("/", 1)[1]
    assert "Start this plan" in session_client.get(plan_url).text

    started = session_client.post("/sessions/start", data={"token": token, "plan_id": plan_id})
    assert "Session started." in started.text
    assert "Playing now" in started.text
    assert f'following <a href="/plans/{plan_id}">plan {plan_id}</a>' in started.text
    session_client.snapshot_mock.assert_called_once_with(7)
    assert "Start this plan" not in session_client.get(plan_url).text  # already playing

    again = session_client.post("/sessions/start", data={"token": token, "plan_id": plan_id})
    assert "already has a session running" in again.text

    session_id = re.search(r'action="/sessions/(\d+)/finish"', started.text).group(1)
    too_long = session_client.post(
        f"/sessions/{session_id}/finish", data={"token": token, "minutes": "500"}
    )
    assert "can&#39;t have lasted" in too_long.text
    assert session_client.snapshot_mock.call_count == 1  # refused before the snapshot

    done = session_client.post(f"/sessions/{session_id}/finish", data={"token": token})
    assert "Session recorded." in done.text
    assert "Playing now" not in done.text
    assert "Recent sessions" in done.text
    assert "may still change" in done.text
    assert "Ore mined is unknown" in done.text
    assert session_client.snapshot_mock.call_count == 2


def test_cancel_takes_no_snapshot_and_problems_are_shown(session_client):
    token, plan_url = _new_plan(session_client)
    plan_id = plan_url.rsplit("/", 1)[1]
    session_client.snapshot_mock.return_value = ["Test Pilot, wallet: timed out"]
    started = session_client.post("/sessions/start", data={"token": token, "plan_id": plan_id})
    assert "Snapshot problems: Test Pilot, wallet: timed out" in started.text
    assert "Playing now" in started.text  # recorded anyway

    session_id = re.search(r'action="/sessions/(\d+)/cancel"', started.text).group(1)
    cancelled = session_client.post(f"/sessions/{session_id}/cancel", data={"token": token})
    assert "Session cancelled." in cancelled.text
    assert session_client.snapshot_mock.call_count == 1


def test_session_forms_need_the_token_and_known_ids(session_client):
    token, plan_url = _new_plan(session_client)
    plan_id = plan_url.rsplit("/", 1)[1]
    refused = session_client.post("/sessions/start", data={"token": "wrong", "plan_id": plan_id})
    assert refused.status_code == 403
    assert (
        session_client.post("/sessions/start", data={"token": token, "plan_id": "999"}).status_code
        == 404
    )
    assert session_client.post("/sessions/999/finish", data={"token": token}).status_code == 404
    assert session_client.post("/sessions/1/pause", data={"token": token}).status_code == 404
    session_client.snapshot_mock.assert_not_called()


def test_sessions_work_without_a_snapshot_taker(planning_client):
    token, plan_url = _new_plan(planning_client)
    plan_id = plan_url.rsplit("/", 1)[1]
    started = planning_client.post("/sessions/start", data={"token": token, "plan_id": plan_id})
    assert "Snapshots are not available here" in started.text
    assert "Playing now" in started.text


def test_forms_accept_the_dashboards_own_origin_as_a_browser_sends_it(planning_client):
    """A real browser sends the page's origin on form posts (the test client sends
    none), so check the cross-site guard lets the dashboard's own origin through
    and still refuses others, including the "null" origin."""
    token = _token(planning_client.get("/today").text)
    form = {"token": token, "character": "Test Pilot", "minutes": "30"}
    own = planning_client.post(
        "/today/plan", data=form, headers={"origin": LOCAL}, follow_redirects=False
    )
    assert own.status_code == 303
    for origin in ("null", "https://evil.example", "http://127.0.0.1:9999"):
        refused = planning_client.post("/today/plan", data=form, headers={"origin": origin})
        assert refused.status_code == 403


@pytest.mark.parametrize(
    ("security", "shown", "classes"),
    [(1.0, "1.0", "sec sec-10"), (0.94, "0.9", "sec sec-09"),
     (0.45, "0.5", "sec sec-05"),  # halves round up, as in EVE: highsec
     (0.449, "0.4", "sec sec-04"),
     (0.04, "0.1", "sec sec-01"),  # EVE shows anything above 0.0 as at least 0.1
     (0.0, "0.0", "sec sec-00"), (-0.62, "-0.6", "sec sec-00"), (None, "?", "sec")],
)  # fmt: skip
def test_security_is_shown_and_coloured_as_eve_does(security, shown, classes):
    from eve_assistant.web import _security, _security_class

    assert _security(security) == shown
    assert _security_class(security) == classes


# --- Friendly errors and plain language (roadmap 2.3) -------------------------


def test_errors_are_friendly_pages_with_a_way_back(client):
    for response in (
        client.get("/no/such/page"),
        client.post("/today/plan", data={"token": "wrong", "character": "x", "minutes": "5"}),
        client.post("/today/plan", data={"token": _token(client.get("/today").text)}),
    ):
        assert response.headers["content-type"].startswith("text/html")
        assert '"detail"' not in response.text
        assert 'href="/today">Go to Today' in response.text
        assert "default-src 'none'" in response.headers["content-security-policy"]
    assert "Nothing here" in client.get("/no/such/page").text
    assert "Something was missing" in client.post("/goals", data={}).text


def test_back_link_only_for_pages_on_this_dashboard(client):
    page = client.get("/missing", headers={"Referer": f"{LOCAL}/goals?x=1"}).text
    assert 'href="/goals?x=1">Back' in page
    page = client.get("/missing", headers={"Referer": "https://evil.example/goals"}).text
    assert ">Back<" not in page


def test_a_crash_shows_a_page_without_the_details(db_path):
    boom = mock.Mock(side_effect=RuntimeError("secret internals"))
    app = create_app(db_path, refresh=boom)
    with TestClient(app, base_url=LOCAL, raise_server_exceptions=False) as test_client:
        token = _token(test_client.get("/characters").text)
        response = test_client.post("/refresh", data={"token": token})
    assert response.status_code == 500
    assert "Something went wrong" in response.text
    assert "secret internals" not in response.text
    assert "default-src 'none'" in response.headers["content-security-policy"]


class _Text(__import__("html.parser").parser.HTMLParser):
    """What a reader sees: text only, not attribute values or hidden fields."""

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data):
        self.parts.append(data)


def _visible(html: str) -> str:
    reader = _Text()
    reader.feed(html)
    return " ".join(reader.parts)


JARGON = ("snapshot", "config/local.toml", "provisional", "settled", "kind:", "route:")


def test_no_internal_words_on_any_page(planning_client):
    token = _token(planning_client.get("/today").text)
    planning_client.post(
        "/today/plan", data={"token": token, "character": "Test Pilot", "minutes": "45"}
    )
    for path in ("/today", "/characters", "/goals", "/skills", "/history", "/settings"):
        text = _visible(planning_client.get(path).text).lower()
        for word in JARGON:
            assert word not in text, (path, word)
        for colour in ("green", "amber", "red"):
            assert f" {colour} " not in f" {text} ", (path, colour)


def test_risk_is_a_word_and_a_shape(planning_client):
    token = _token(planning_client.get("/today").text)
    planning_client.post(
        "/today/plan", data={"token": token, "character": "Test Pilot", "minutes": "45"}
    )
    page = planning_client.get("/today").text
    assert '<span class="pill risk green">Safe</span>' in page
    prefs = planning_client.get("/history").text
    assert "Share of your net worth at stake" in prefs
    assert '<span class="pill risk red">Risky</span>' in prefs
    assert '<span class="pill risk amber">Caution</span>' in prefs


def test_characters_with_the_same_learning_share_one_line(db_path):
    conn = store.connect(db_path)
    snapshots.ensure_character(conn, 8, "Second Pilot")
    conn.close()
    with TestClient(create_app(db_path), base_url=LOCAL) as test_client:
        prefs = test_client.get("/history").text
    assert prefs.count("Mining speed: not yet checked") == 1
    assert "<strong>Second Pilot, Test Pilot</strong>" in prefs


def test_omega_unlock_is_said_once():
    from types import SimpleNamespace

    import jinja2

    from eve_assistant import skills, web
    from eve_assistant.skills import Reason

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(web.HERE / "templates")), autoescape=True
    )
    env.globals["describe"] = skills.describe
    env.filters["duration"] = skills.format_minutes
    req = SimpleNamespace(
        usable_now=False,
        usable_with_omega_now=True,
        steps=[],
        reasons=[Reason("trained_but_alpha_locked"),
                 Reason("alpha_cap", {"skill": "Mining Barge", "needed": 1, "alpha_max": 0})],
    )  # fmt: skip
    html = env.get_template("_requirement.html").render(req=req, is_alpha=True)
    assert html.count("already trained") == 1
    assert "Mining Barge 1 is above the Alpha limit" in html


# --- One way to commit, and the session view (roadmap 2.4) --------------------


def test_steps_offer_no_do_it(planning_client):
    _token_value, plan_url = _new_plan(planning_client)
    page = planning_client.get(plan_url).text
    assert ">Do it<" not in page
    assert ">Not now<" in page
    assert ">Not this kind<" in page
    assert "Start this plan" in page


def test_today_becomes_the_session_view_while_playing(session_client):
    token, plan_url = _new_plan(session_client)
    plan_id = plan_url.rsplit("/", 1)[1]
    session_client.post("/sessions/start", data={"token": token, "plan_id": plan_id})

    today = session_client.get("/today").text
    assert "Playing now" in today
    assert "Now · step 1 of 1" in today
    assert "Plan my time" not in today  # the planning form waits until you're done
    assert '<meta http-equiv="refresh" content="60; url=/today">' in today
    session_id = re.search(r'id="session-(\d+)"', today).group(1)
    item_id = re.search(r'/steps/(\d+)/done"', today).group(1)

    ticked = session_client.post(
        f"/sessions/{session_id}/steps/{item_id}/done",
        data={"token": token},
        follow_redirects=False,
    )
    assert ticked.headers["location"] == f"/today#session-{session_id}"
    today = session_client.get("/today").text
    assert "Every step is done or skipped" in today
    assert f"/steps/{item_id}/undo" in today

    assert '<meta http-equiv="refresh"' in session_client.get("/characters").text
    assert '<meta http-equiv="refresh"' not in session_client.get("/goals").text  # forms

    session_client.post(f"/sessions/{session_id}/finish", data={"token": token})
    today = session_client.get("/today").text
    assert "Plan my time" in today
    assert '<meta http-equiv="refresh"' not in today
    assert "ticked off" in session_client.get("/history").text


def test_not_now_from_the_session_view_returns_to_it(session_client):
    token, plan_url = _new_plan(session_client)
    plan_id = plan_url.rsplit("/", 1)[1]
    session_client.post("/sessions/start", data={"token": token, "plan_id": plan_id})
    today = session_client.get("/today").text
    session_id = re.search(r'id="session-(\d+)"', today).group(1)
    item_id = re.search(r'/steps/(\d+)/done"', today).group(1)
    response = session_client.post(
        f"/plan-items/{item_id}/respond",
        data={"token": token, "response": "not_now"},
        follow_redirects=False,
    )
    assert response.headers["location"] == f"/today?msg=not_now#session-{session_id}"


def test_ticking_needs_the_token_and_a_real_step(session_client):
    token, plan_url = _new_plan(session_client)
    plan_id = plan_url.rsplit("/", 1)[1]
    session_client.post("/sessions/start", data={"token": token, "plan_id": plan_id})
    assert session_client.post("/sessions/1/steps/1/done", data={"token": "x"}).status_code == 403
    assert (
        session_client.post("/sessions/1/steps/999/done", data={"token": token}).status_code == 400
    )
    assert session_client.post("/sessions/9/steps/1/done", data={"token": token}).status_code == 404
    assert (
        session_client.post("/sessions/1/steps/1/maybe", data={"token": token}).status_code == 404
    )


def test_start_project_is_offered_on_a_projects_first_step(db_path):
    from eve_assistant import planner, projects, risk

    conn = store.connect(db_path)
    project = {"kind": "haul", "key": "k", "title": "Haul ore", "total_minutes": 120, "isk": 5e6}
    plan = planner.record(
        conn,
        planner.Plan(
            character_id=7,
            minutes_available=60,
            items=(
                planner.PlanItem(
                    kind="project_start",
                    key="k",
                    title="Haul ore, first trip",
                    isk=1e6,
                    minutes=30,
                    risk=risk.rate("low", 1e5, 1e8),
                    inputs={"project": project},
                ),
            ),  # fmt: skip
            alternatives=(),
            notes=(),
            net_worth=1e8,
        ),
        {},
    )
    item_id = planner.load_plan(conn, plan.plan_id).items[0].item_id
    conn.close()
    with TestClient(create_app(db_path), base_url=LOCAL) as test_client:
        page = test_client.get(f"/plans/{plan.plan_id}").text
        assert ">Start project<" in page
        started = test_client.post(
            f"/plan-items/{item_id}/respond", data={"token": _token(page), "response": "do"}
        )
        assert "Project started" in started.text
        assert ">Start project<" not in started.text
    conn = store.connect(db_path)
    assert [p.title for p in projects.open_projects(conn, 7)] == ["Haul ore"]
    conn.close()


# --- Plan age and staying in place (roadmap 2.5) ------------------------------


def test_ago_reads_like_a_person_would_say_it(monkeypatch):
    from datetime import UTC, datetime

    from eve_assistant import web

    monkeypatch.setattr(web, "_now", lambda: datetime(2026, 10, 17, 12, 0, tzinfo=UTC))
    assert web._ago("2026-10-17T11:59:40+00:00") == "just now"
    assert web._ago("2026-10-17T11:48:00+00:00") == "12 min ago"
    assert web._ago("2026-10-17T11:00:00+00:00") == "1 hour ago"
    assert web._ago("2026-10-17T06:00:00Z") == "6 hours ago"
    assert web._ago("2026-10-14T12:00:00+00:00") == "3 days ago"
    assert web._ago(None) == "never"


def test_an_old_plan_says_so_and_offers_to_plan_again(planning_client, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from eve_assistant import web

    _token_value, plan_url = _new_plan(planning_client)
    assert "Plan again" not in planning_client.get(plan_url).text  # fresh
    monkeypatch.setattr(web, "_now", lambda: datetime.now(UTC) + timedelta(hours=6))
    page = planning_client.get(plan_url).text
    assert "This plan was made 6 hours ago." in page
    assert ">Plan again</button>" in page
    assert '<input type="hidden" name="character" value="Test Pilot">' in page
    assert '<input type="hidden" name="minutes" value="60">' in page


def test_answering_a_step_returns_to_it(planning_client):
    token, plan_url = _new_plan(planning_client)
    page = planning_client.get(plan_url).text
    item_id = re.search(r'<li class="step" id="item-(\d+)"', page).group(1)
    response = planning_client.post(
        f"/plan-items/{item_id}/respond",
        data={"token": token, "response": "not_now"},
        follow_redirects=False,
    )
    assert response.headers["location"].endswith(f"?msg=not_now#item-{item_id}")


# --- Active character switcher (roadmap 2.7) ----------------------------------


def _second_character(db_path):
    conn = store.connect(db_path)
    snapshots.ensure_character(conn, 8, "Alpha Pilot")  # first by name, but offline
    snapshots.record(conn, 8, "online", {"online": False})
    conn.close()


def test_the_online_character_is_active_by_default(db_path):
    _second_character(db_path)
    with TestClient(create_app(db_path), base_url=LOCAL) as test_client:
        page = test_client.get("/today").text
    assert '<option value="7" selected>Test Pilot</option>' in page
    assert "For <strong>Test Pilot</strong>" in page


def test_switching_character_follows_on_today_skills_and_goals(db_path):
    _second_character(db_path)
    app = create_app(db_path, make_plan=_fake_plan_maker(db_path))
    with TestClient(app, base_url=LOCAL) as test_client:
        token, _plan_url = _new_plan(test_client)  # a plan for Test Pilot
        assert "Plan for Test Pilot" in test_client.get("/today").text
        switched = test_client.post(
            "/character",
            data={"token": token, "character_id": "8", "return_to": "/skills"},
            follow_redirects=False,
        )
        assert switched.headers["location"] == "/skills"
        assert switched.headers["set-cookie"].startswith("character=8;")
        test_client.cookies.set("character", "8")
        today = test_client.get("/today").text
        assert "For <strong>Alpha Pilot</strong>" in today
        assert "Plan for Test Pilot" not in today  # Today shows the active character's plans
        assert "For <strong>Alpha Pilot</strong>" in test_client.get("/skills").text
        assert "<option selected>Alpha Pilot</option>" in test_client.get("/goals").text


def test_only_known_characters_can_be_chosen(client):
    token = _token(client.get("/today").text)
    refused = client.post("/character", data={"token": token, "character_id": "999"})
    assert refused.status_code == 400
    client.cookies.set("character", "not-a-number")
    assert '<option value="7" selected>Test Pilot</option>' in client.get("/today").text


# --- Skills to Goals, and better goals (roadmap 2.8) ---------------------------


def test_a_ship_becomes_a_goal_with_progress_in_two_clicks(client):
    checked = client.get("/skills", params={"item": "Big Barge"}).text  # click 1: Check
    assert '<datalist id="ship-names">' in checked
    assert '<option value="Big Barge">' in checked
    assert ">Set as goal</button>" in checked
    added = client.post(  # click 2: Set as goal
        "/goals",
        data={"token": _token(checked), "kind": "ship", "target": "Big Barge",
              "character": "Test Pilot"},
    )  # fmt: skip
    assert "Goal added." in added.text
    assert "Fly a Big Barge" in added.text
    assert "% of the skill points trained" in added.text
    assert '<progress value="' in added.text


def test_goal_names_are_suggested_while_typing(client):
    page = client.get("/goals").text
    assert (
        '<input id="target" name="target" required placeholder="Retriever" list="goal-names"'
        in page
    )
    assert '<option value="Mining">' in page  # skills
    assert '<option value="Venture">' in page  # and ships


def test_isk_goals_read_the_same_way_as_other_isk(client):
    token = _token(client.get("/goals").text)
    client.post(
        "/goals",
        data={"token": token, "kind": "isk", "target": "2460000000", "character": "Test Pilot"},
    )
    assert "Have 2.46B ISK" in client.get("/goals").text


def test_dropping_a_goal_asks_first(client):
    token = _token(client.get("/goals").text)
    client.post(
        "/goals",
        data={"token": token, "kind": "ship", "target": "Big Barge", "character": "Test Pilot"},
    )
    page = client.get("/goals").text
    goal_id = re.search(r'href="/goals/(\d+)/drop">Drop</a>', page).group(1)
    confirm = client.get(f"/goals/{goal_id}/drop").text
    assert "Drop this goal?" in confirm
    assert "Fly a Big Barge" in confirm
    assert 'href="/goals">Keep it</a>' in confirm
    assert "Fly a Big Barge" in client.get("/goals").text  # nothing dropped yet
    dropped = client.post(f"/goals/{goal_id}/status", data={"token": token, "status": "dropped"})
    assert "Goal dropped." in dropped.text
    assert client.get("/goals/999/drop").status_code == 404


def test_dropping_a_project_asks_first(planning_client, db_path):
    from eve_assistant import projects

    conn = store.connect(db_path)
    project_id = projects.start(conn, 7, "haul", "1:2:x", "Haul ore", 120, 5e6, {})
    conn.close()
    page = planning_client.get("/today").text
    assert f'href="/projects/{project_id}/drop">Drop</a>' in page
    confirm = planning_client.get(f"/projects/{project_id}/drop").text
    assert "Drop this project?" in confirm
    assert f'action="/projects/{project_id}/dropped"' in confirm


def test_times_say_how_long_ago_or_until(monkeypatch):
    from datetime import UTC, datetime

    from eve_assistant import web

    monkeypatch.setattr(web, "_now", lambda: datetime(2026, 10, 17, 12, 0, tzinfo=UTC))
    assert web._when_ago("2026-10-17T06:00:00+00:00") == "2026-10-17 06:00 UTC · 6 hours ago"
    assert web._when_ago("2026-10-19T12:00:00+00:00") == "2026-10-19 12:00 UTC · in 2 days"
    assert web._when_ago("2026-10-17T12:00:20+00:00").endswith("· just now")
    assert web._when_ago(None) == "never"


# --- Navigation (roadmap 2.9) -------------------------------------------------


def test_preferences_became_settings(client):
    old = client.get("/preferences", follow_redirects=False)
    assert old.status_code == 308
    assert old.headers["location"] == "/settings"
    page = client.get("/settings").text
    assert "<h1>Settings</h1>" in page
    assert 'href="/settings" aria-current="page">Settings</a>' in page
    assert "Risk matrix" not in page  # moved to History and learning
    assert "<h1>History and learning</h1>" in client.get("/history").text


# --- Add a character from the dashboard (roadmap 2.10) --------------------------


def _wait_for(predicate, seconds=3.0):
    import time

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_a_character_can_be_added_from_the_dashboard(db_path):
    import threading

    browser_done = threading.Event()
    calls = []

    def add_character():
        calls.append(1)
        browser_done.wait(5)
        return "New Pilot"

    with TestClient(create_app(db_path, add_character=add_character), base_url=LOCAL) as client:
        page = client.get("/characters").text
        assert ">Add a character</button>" in page
        token = _token(page)
        started = client.post("/characters/add", data={"token": token}, follow_redirects=False)
        assert started.headers["location"] == "/characters"
        waiting = client.get("/characters").text
        assert "EVE's login page is open in your browser" in waiting
        assert '<meta http-equiv="refresh" content="5; url=/characters">' in waiting
        assert "disabled>Add a character" in waiting
        client.post("/characters/add", data={"token": token})  # pressed again: no second login
        browser_done.set()
        assert _wait_for(lambda: "Added New Pilot." in client.get("/characters").text)
        assert calls == [1]


def test_a_failed_login_is_shown(db_path):
    def add_character():
        raise RuntimeError("the browser window was closed")

    with TestClient(create_app(db_path, add_character=add_character), base_url=LOCAL) as client:
        client.post("/characters/add", data={"token": _token(client.get("/characters").text)})
        assert _wait_for(
            lambda: (
                "The login didn&#39;t finish: the browser window was closed"
                in client.get("/characters").text
            )
        )


def test_adding_needs_the_dashboard_to_offer_it(client):
    page = client.get("/characters").text
    assert "Add a character" not in page
    response = client.post("/characters/add", data={"token": _token(page)})
    assert response.status_code == 400
    assert "eve-assistant login" in response.text


# --- Portraits and ship renders (roadmap 2.11) ----------------------------------


def test_images_are_served_by_the_dashboard(db_path):
    png = b"\x89PNG\r\n\x1a\nfake"
    asked = []

    def fetch(address):
        asked.append(address)
        return png

    with TestClient(create_app(db_path, fetch_image=fetch), base_url=LOCAL) as client:
        portrait = client.get("/images/portrait/7.png")
        assert portrait.status_code == 200
        assert portrait.headers["content-type"] == "image/png"
        assert portrait.headers["cache-control"] == "private, max-age=86400"
        assert portrait.content == png
        assert client.get("/images/portrait/999.png").status_code == 404  # not our character
        assert client.get("/images/logo/1.png").status_code == 404
        page = client.get("/characters").text
        assert '<img class="portrait" src="/images/portrait/7.png"' in page
        assert '<img class="render" src="/images/render/32880.png"' in page
    assert asked == ["https://images.evetech.net/characters/7/portrait?size=128"]


def test_a_placeholder_when_no_image_is_available(client):
    response = client.get("/images/render/32880.png")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert "<polygon" in response.text


def test_today_shows_the_next_step_and_a_timeline(planning_client):
    _token_value, plan_url = _new_plan(planning_client)
    page = planning_client.get("/today").text
    assert "Next up · step 1 of 1" in page
    assert '<ol class="steps timeline">' in page
    assert '<img class="render" src="/images/render/32880.png"' in page  # the ship it flies


# --- Phone layout (roadmap 2.12) -------------------------------------------------


class _Tables(__import__("html.parser").parser.HTMLParser):
    """Each table's headings, and each body cell's data-label."""

    def __init__(self):
        super().__init__()
        self.tables: list[dict] = []
        self._in_head = self._in_th = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "table":
            self.tables.append({"grid": "keep-grid" in (attrs.get("class") or ""),
                                "heads": [], "rows": []})  # fmt: skip
        elif tag == "thead":
            self._in_head = True
        elif tag == "th" and self._in_head:
            self._in_th = True
            self.tables[-1]["heads"].append("")
        elif tag == "tr" and not self._in_head and self.tables:
            self.tables[-1]["rows"].append([])
        elif tag == "td" and self.tables and self.tables[-1]["rows"]:
            self.tables[-1]["rows"][-1].append(attrs.get("data-label"))

    def handle_endtag(self, tag):
        if tag == "thead":
            self._in_head = False
        elif tag == "th":
            self._in_th = False

    def handle_data(self, data):
        if self._in_th and self._in_head:
            self.tables[-1]["heads"][-1] += data.strip()


def test_tables_can_become_cards_on_a_phone(planning_client, db_path):
    from eve_assistant import projects

    _token_value, plan_url = _new_plan(planning_client)
    conn = store.connect(db_path)
    projects.start(conn, 7, "haul", "1:2:x", "Haul ore", 120, 5e6, {})
    conn.close()
    checked = 0
    for path in (plan_url, "/today", "/history", "/settings", "/skills?item=Big%20Barge"):
        reader = _Tables()
        reader.feed(planning_client.get(path).text)
        for table in reader.tables:
            if table["grid"]:
                continue
            for row in table["rows"]:
                for head, label in zip(table["heads"], row, strict=False):
                    if head:
                        assert label == head, (path, head, label)
                        checked += 1
    assert checked > 10


def test_a_compact_menu_for_narrow_screens(client):
    page = client.get("/goals").text
    assert '<details class="nav-narrow">' in page
    assert "<summary>Menu</summary>" in page
    assert page.count('<a href="/goals" aria-current="page">Goals</a>') == 2  # wide and narrow
    assert page.startswith("<!doctype html>")


# --- Who should play today (roadmap 3.4) ------------------------------------------


def _paying_plan_maker(db_path, isk_by_character):
    from eve_assistant import planner, risk

    def make(character_id, minutes, allow_lowsec, allow_risky):
        conn = store.connect(db_path)
        try:
            item = planner.PlanItem(
                kind="mining", key=str(character_id), title=f"Mine for {character_id}",
                isk=isk_by_character[character_id], minutes=minutes,
                risk=risk.rate("low", 100_000, 10_000_000),
            )  # fmt: skip
            plan = planner.Plan(character_id, minutes, (item,), (), (), 10_000_000)
            return planner.record(conn, plan, {}).plan_id
        finally:
            conn.close()

    return make


def test_comparing_characters_says_who_should_play(db_path):
    _second_character(db_path)
    maker = _paying_plan_maker(db_path, {7: 1_000_000, 8: 4_000_000})
    with TestClient(create_app(db_path, make_plan=maker), base_url=LOCAL) as client:
        page = client.get("/today").text
        assert ">Compare characters</button>" in page
        compared = client.post(
            "/today/plan",
            data={"token": _token(page), "character": "Test Pilot", "minutes": "60",
                  "compare": "true"},
            follow_redirects=False,
        )  # fmt: skip
        assert compared.headers["location"].startswith("/today?compare=")
        view = client.get(compared.headers["location"]).text
    assert "Who should play · 60 minutes" in view
    assert "<strong>Alpha Pilot</strong> would make the most" in view
    assert "3M more than Test Pilot" in view
    assert view.index("Play Alpha Pilot") < view.index("Play Test Pilot")  # best first


def test_compare_is_offered_only_with_more_than_one_character(planning_client):
    assert "Compare characters" not in planning_client.get("/today").text


def test_the_compare_list_only_takes_plan_numbers(planning_client):
    page = planning_client.get("/today?compare=1,x,%3Cscript%3E").text
    assert "Who should play" not in page  # no such plans
    assert "<script>" not in page


# --- Omega comparison and a PLEX goal (roadmap 3.5) -------------------------------


def test_goals_say_what_omega_would_change(client, monkeypatch):
    from eve_assistant import omega
    from eve_assistant.omega import Mining, OmegaComparison

    monkeypatch.setattr(
        omega,
        "compare",
        lambda _db, _cid: OmegaComparison(
            "Test Pilot", Mining("Venture", 384.0), Mining("Procurer", 833.0), 92.0, 4_735_090.0
        ),
    )
    page = client.get("/goals").text
    assert "What Omega would change · Test Pilot" in page
    assert "m³/min · Procurer" in page
    assert "it pays for itself only past about" in page
    added = client.post(
        "/goals",
        data={"token": _token(page), "kind": "omega", "target": "1", "character": "Test Pilot"},
    )
    assert "Omega for 1 month (500 PLEX)" in added.text
