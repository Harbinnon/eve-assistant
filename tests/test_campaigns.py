"""Military campaigns (roadmap 5.6): running objectives that fit what the
planner models are mentioned in plans; nothing while none runs."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from eve_assistant import campaigns, explain, store

NOW = datetime(2026, 10, 17, 18, tzinfo=UTC)


class FakeEsi:
    def __init__(self, campaign_state, objective_state="Active"):
        self.answers = {
            "/military-campaigns": {"campaigns": [
                {"id": "c1", "state": campaign_state, "progress": 3}]},
            "/military-campaigns/c1/objectives": {"objectives": [
                {"id": "mine", "state": objective_state, "progress": 10},
                {"id": "pvp", "state": "Active", "progress": 1}]},
        }  # fmt: skip
        self.calls = []

    def get(self, path, **_kwargs):
        self.calls.append(path)
        return SimpleNamespace(data=self.answers[path])


@pytest.fixture
def db():
    conn = store.connect(":memory:")
    conn.execute("INSERT INTO sde_campaigns VALUES ('c1', 'The Blessed Exchange')")
    location = [{"key": "location", "matcher": {"values": [
        {"valueType": "solarsystem", "values": ["30002187", "30002188"]}]}}]  # fmt: skip
    conn.executemany(
        "INSERT INTO sde_campaign_objectives VALUES (?, 'c1', ?, ?, 'industrialist', ?, ?, "
        "1000035, 0.1, 1, ?)",
        [("mine", "Mine for the Deaconry", "MineOre", 150_000, 300, json.dumps(location)),
         ("pvp", "Hunt capsuleers", "KillCapsuleer", 500_000, 900, "[]")],
    )  # fmt: skip
    yield conn
    conn.close()


def test_running_objectives_that_fit_an_activity(db):
    esi = FakeEsi("Active")
    campaigns.refresh(db, esi, NOW)
    assert "/military-campaigns/c1/objectives" in esi.calls
    (objective,) = campaigns.open_objectives(db)  # combat isn't planned here
    assert (objective.title, objective.activity) == ("Mine for the Deaconry", "mining")
    assert objective.systems == (30002187, 30002188)
    text = explain.describe("campaign_objective", {
        "campaign": objective.campaign, "title": objective.title, "activity": "mining",
        "isk": objective.isk, "lp": objective.lp, "systems": 2})  # fmt: skip
    assert text.startswith("Military campaign “The Blessed Exchange” is running:")
    assert "pays 150k ISK and 300 LP each time, plus standing (mining, in 2 systems)" in text
    assert not campaigns.due(db, NOW)


def test_nothing_while_no_campaign_runs(db):
    esi = FakeEsi("Completed")
    campaigns.refresh(db, esi, NOW)
    assert esi.calls == ["/military-campaigns"]  # objectives only for running campaigns
    assert campaigns.open_objectives(db) == []
    assert campaigns.campaigns_known(db) == 1
    campaigns.refresh(db, FakeEsi("Active", objective_state="Completed"), NOW)
    assert campaigns.open_objectives(db) == []
