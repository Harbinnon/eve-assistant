"""Goals in sequence (roadmap 8.2): a goal that follows others counts their ISK
and training with its own."""

import re

import pytest
from fastapi.testclient import TestClient

from eve_assistant import alerts, cli, goals, skills, snapshots, store
from eve_assistant.web import create_app
from tests.test_goal_fits import BARGE, MINING, _db

# The fixture's character has 1,000,000 ISK and Mining 2.


@pytest.fixture
def db():
    conn = _db()
    yield conn
    conn.close()


def _status(db, goal_id):
    statuses, problems = goals.evaluate_all(db)
    assert problems == []
    return next(s for s in statuses if s.goal.goal_id == goal_id)


def test_a_goal_that_follows_counts_the_isk_before_it(db):
    first = goals.add_goal(db, "isk", 7, isk=5_000_000)
    then = goals.add_goal(db, "isk", 7, isk=3_000_000, after_goal_id=first)
    alone = _status(db, first)
    assert (alone.isk_needed, alone.after, alone.chain_isk) == (4_000_000, None, None)
    following = _status(db, then)
    assert following.after.goal_id == first
    assert following.chain_isk == 8_000_000
    assert following.isk_needed == 7_000_000  # the wallet goes to the first goal first


def test_training_adds_up_with_each_skill_level_once(db):
    first = goals.add_goal(db, "skill", 7, type_id=MINING, level=3)
    then = goals.add_goal(db, "skill", 7, type_id=BARGE, level=1, after_goal_id=first)
    character = skills.load_character_skills(db, 7)
    mining3 = skills.plan_skill(db, character, MINING, 3)
    barge1 = skills.plan_skill(db, character, BARGE, 1)  # needs Mining 3 too
    following = _status(db, then)
    shared = {(s.skill_id, s.level) for s in mining3.steps} & {(s.skill_id, s.level)
                                                                for s in barge1.steps}  # fmt: skip
    assert shared  # Mining 3 is in both
    # Mining 3 counted once, not twice; Alpha trains at half speed.
    repeated = sum(s.minutes_omega for s in barge1.steps if (s.skill_id, s.level) in shared)
    once = mining3.minutes_as_omega + barge1.minutes_as_omega - repeated
    assert following.chain_training == pytest.approx(once / skills.ALPHA_TRAINING_MULTIPLIER)


def test_sequences_are_listed_in_order_and_a_closed_goal_ends_one(db):
    first = goals.add_goal(db, "isk", 7, isk=5_000_000)
    other = goals.add_goal(db, "isk", 7, isk=1_000)
    then = goals.add_goal(db, "isk", 7, isk=3_000_000, after_goal_id=first)
    statuses, _ = goals.evaluate_all(db)
    assert [s.goal.goal_id for s in statuses] == [first, then, other]
    goals.set_status(db, first, "done")
    following = _status(db, then)
    assert (following.after, following.chain_isk, following.isk_needed) == (None, None, 2_000_000)


def test_a_goal_can_only_follow_an_active_goal_of_the_same_character(db):
    snapshots.ensure_character(db, 8, "Hauler")
    first = goals.add_goal(db, "isk", 7, isk=5_000_000)
    with pytest.raises(goals.GoalError, match="same character"):
        goals.add_goal(db, "isk", 8, isk=1, after_goal_id=first)
    with pytest.raises(goals.GoalError, match="no active goal 999"):
        goals.add_goal(db, "isk", 7, isk=1, after_goal_id=999)
    goals.set_status(db, first, "dropped")
    with pytest.raises(goals.GoalError, match=f"no active goal {first}"):
        goals.add_goal(db, "isk", 7, isk=1, after_goal_id=first)


def test_goal_reached_counts_the_goals_before(db):
    first = goals.add_goal(db, "isk", 7, isk=500_000)
    goals.add_goal(db, "isk", 7, isk=800_000, after_goal_id=first)
    reached = [a.key for a in alerts._goals(db)]
    # 1M covers the first, but not the first and second together (1.3M).
    assert reached == [f"goal:{first}"]


def test_the_goals_page_sets_and_shows_a_sequence(tmp_path):
    path = tmp_path / "g.sqlite3"
    _db(path).close()
    with TestClient(create_app(path), base_url="http://127.0.0.1") as client:
        page = client.get("/goals").text
        token = re.search(r'name="token" value="([^"]+)"', page).group(1)
        base = {"token": token, "kind": "isk", "character": "Miner"}
        first_page = client.post("/goals", data={**base, "target": "5000000"}).text
        assert '<select id="after" name="after">' in first_page
        first = re.search(r'<option value="(\d+)">Have 5M ISK', first_page).group(1)
        shown = client.post("/goals", data={**base, "target": "3000000", "after": first}).text
        assert "Then, after “Have 5M ISK”" in shown
        assert "With the goals before" in shown
        assert "8M ISK in all" in shown


def test_the_cli_adds_and_lists_a_sequence(tmp_path, monkeypatch, capsys):
    path = tmp_path / "g.sqlite3"
    _db(path).close()
    real_connect = store.connect
    monkeypatch.setattr(cli.store, "connect", lambda *_a: real_connect(path))
    assert cli.main(["goal", "add", "isk", "5000000", "--character", "Miner"]) == 0
    first = re.search(r"Added goal (\d+)", capsys.readouterr().out).group(1)
    assert cli.main(["goal", "add", "isk", "3000000", "--character", "Miner",
                     "--after", first]) == 0  # fmt: skip
    assert cli.main(["goals"]) == 0
    assert f"After [{first}] Have 5M ISK: 8,000,000 ISK with the goals before" in (
        capsys.readouterr().out
    )
