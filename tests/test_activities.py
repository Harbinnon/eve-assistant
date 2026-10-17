"""The planner's activities: one module each, one interface."""

import ast
from pathlib import Path

from eve_assistant import activities, planner, snapshots, store

# Models only activities may use; the planner ranks and fits, nothing more.
ACTIVITY_MODELS = {"couriers", "fittings", "mining", "ore", "routes", "structures", "corrections"}


def test_every_activity_has_the_interface():
    names = [a.__name__.rsplit(".", 1)[1] for a in activities.ALL]
    assert names == ["skills", "projects", "selling", "mining", "gas", "couriers"]
    for activity in activities.ALL:
        assert callable(activity.candidates), activity.__name__
        assert callable(activity.learned), activity.__name__


def test_the_planner_knows_no_activity_internals():
    tree = ast.parse(Path(planner.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "eve_assistant":
            imported |= {alias.name for alias in node.names}
    assert not imported & ACTIVITY_MODELS, imported & ACTIVITY_MODELS


def test_learning_is_collected_from_the_activities():
    db = store.connect(":memory:")
    snapshots.ensure_character(db, 7, "Miner")
    learned = activities.learned(db, 7)
    assert [c.what for c in learned] == [
        "sale_price",
        "mining_rate",
        "gas_rate",
        "minutes_per_jump",
    ]
    db.close()
