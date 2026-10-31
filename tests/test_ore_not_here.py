"""An ore the player says isn't around ("Not this ore"): only in ore anomalies,
say, which no API shows. It's ruled out of plans until unmuted in Settings."""

import re

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from eve_assistant import planner, preferences, store
from eve_assistant.activities import mining as mining_activity
from eve_assistant.esi.client import EsiClient
from eve_assistant.web import create_app
from tests.test_together import BASE, HOME, LOCAL, ORE, _mock_esi, _token, _world


def test_an_ore_can_be_ruled_out_and_back_in():
    db = _world()
    assert preferences.mute(db, preferences.ore_category(ORE))
    assert preferences.ruled_out_ores(db) == {ORE}
    with pytest.raises(preferences.PreferenceError):
        preferences.mute(db, "ore:Ytirium")
    assert preferences.unmute(db, preferences.ore_category(ORE))
    assert preferences.ruled_out_ores(db) == set()


def test_a_ruled_out_ore_isnt_chosen_unless_named():
    db = _world()
    preferences.mute(db, preferences.ore_category(ORE))
    notes = []
    choices = mining_activity._ore_choices(db, 7, HOME, None, notes)
    assert ORE not in choices
    assert [n.detail for n in notes if n.code == "ores_ruled_out"] == [{"ores": ["Veldspar"]}]
    assert mining_activity._ore_choices(db, 7, HOME, ["Veldspar"], []) == [ORE]


def _plan_with_mining(db) -> tuple[int, int, int]:
    with httpx.Client() as http:
        plan = planner.build_plan(db, EsiClient(http, db, base_url=BASE), 7, 60)
    stored = planner.record(db, plan, {})
    rows = db.execute(
        "SELECT item_id, kind FROM plan_items WHERE plan_id = ? AND position IS NOT NULL",
        (stored.plan_id,),
    ).fetchall()
    mining = next(r["item_id"] for r in rows if r["kind"] == "mining")
    other = next(r["item_id"] for r in rows if r["kind"] != "mining")
    return stored.plan_id, mining, other


@respx.mock
def test_not_this_ore_rules_out_the_steps_ore_only():
    _mock_esi()
    db = _world()
    _plan_id, mining, other = _plan_with_mining(db)
    with pytest.raises(planner.PlanError, match="Only a mining step"):
        planner.respond(db, other, "mute", scope="ore")
    planner.respond(db, mining, "mute", scope="ore")
    assert preferences.ruled_out_ores(db) == {ORE}
    assert "kind:mining" not in preferences.active_mutes(db)  # mining itself stays


@respx.mock
def test_the_dashboard_offers_not_this_ore_and_settings_undoes_it(tmp_path):
    _mock_esi()
    path = tmp_path / "o.sqlite3"
    db = _world(path)
    plan_id, mining, _other = _plan_with_mining(db)
    db.close()
    with TestClient(create_app(path), base_url=LOCAL) as client:
        page = client.get(f"/plans/{plan_id}").text
        assert "Not this ore" in page
        answer = {"token": _token(page), "response": "mute", "scope": "ore"}
        sent = client.post(
            f"/plan-items/{mining}/respond",
            data={**answer, "return_to": "https://elsewhere.example/x"},
            follow_redirects=False,
        )
        assert sent.headers["location"].startswith(f"/plans/{plan_id}?msg=ore_ruled_out")
        back = client.post(
            f"/plan-items/{mining}/respond",
            data={**answer, "return_to": "/teams/3"},
            follow_redirects=False,
        )
        assert back.headers["location"] == "/teams/3?msg=ore_ruled_out"
        settings = client.get("/settings").text
        assert "Mining Veldspar" in settings
        assert "(you said it isn't around)" in settings
        client.post(
            "/settings/unmute",
            data={"token": _token(settings), "category": f"ore:{ORE}"},
        )
        assert "Mining Veldspar" not in client.get("/settings").text
    assert preferences.ruled_out_ores(store.connect(path)) == set()
    assert re.search(r"Not this ore", page)
