"""Your own losses (roadmap 4.2): only your victims' killmails are kept, and
they turn the guessed loss chances into learned ones."""

import json
from types import SimpleNamespace

import pytest

from eve_assistant import explain, losses, planner, risk, sessions, snapshots, store

ME, ALT, STRANGER = 7, 8, 95094576
VENTURE, ORE, CONTAINER = 32880, 1230, 3297


class FakeEsi:
    def __init__(self, mails):
        self.mails = mails
        self.calls = []

    def get(self, path, remember=True, **_kwargs):
        self.calls.append((path, remember))
        return SimpleNamespace(data=self.mails[int(path.split("/")[2])])


def _mail(killmail_id, victim, attacker, when="2026-10-17T10:30:00Z"):
    return {
        "killmail_id": killmail_id,
        "killmail_time": when,
        "solar_system_id": 30000001,
        "victim": {
            "character_id": victim,
            "ship_type_id": VENTURE,
            "items": [
                {"item_type_id": ORE, "quantity_destroyed": 300, "quantity_dropped": 200},
                {
                    "item_type_id": CONTAINER,
                    "quantity_dropped": 1,
                    "items": [{"item_type_id": ORE, "quantity_destroyed": 10}],
                },
            ],
        },  # fmt: skip
        "attackers": [{"character_id": attacker, "corporation_id": 98000001}],
    }


@pytest.fixture
def db():
    conn = store.connect(":memory:")
    snapshots.ensure_character(conn, ME, "Miner")
    snapshots.ensure_character(conn, ALT, "Hauler")
    conn.execute("INSERT INTO market_prices VALUES (?, 400000, NULL, 'x')", (VENTURE,))
    conn.execute("INSERT INTO market_prices VALUES (?, 10, NULL, 'x')", (ORE,))
    yield conn
    conn.close()


def test_only_your_own_losses_are_kept_and_attackers_never(db):
    snapshots.record(db, ME, "killmails", [
        {"killmail_id": 1, "killmail_hash": "a"},  # Miner lost a Venture
        {"killmail_id": 2, "killmail_hash": "b"},  # Miner shot someone else
    ])  # fmt: skip
    esi = FakeEsi({1: _mail(1, ME, STRANGER), 2: _mail(2, STRANGER, ME)})
    assert losses.update(db, esi, ME) == 1
    assert all(remember is False for _path, remember in esi.calls)  # never cached
    (loss,) = losses.all_losses(db)
    assert loss.killed_at == "2026-10-17T10:30:00+00:00"
    assert dict(loss.items) == {ORE: 510, CONTAINER: 1}  # the container's ore included
    assert losses.value(db, loss) == pytest.approx(400_000 + 510 * 10)
    dump = "\n".join(db.iterdump())
    assert str(STRANGER) not in dump
    assert "98000001" not in dump

    assert losses.update(db, esi, ME) == 0  # both seen: neither fetched again
    assert len(esi.calls) == 2


def test_fetches_are_capped_per_refresh(db, monkeypatch):
    monkeypatch.setattr(losses, "MAX_FETCH", 2)
    snapshots.record(db, ME, "killmails",
                     [{"killmail_id": n, "killmail_hash": "h"} for n in range(1, 6)])  # fmt: skip
    esi = FakeEsi({n: _mail(n, ME, STRANGER) for n in range(1, 6)})
    assert losses.update(db, esi, ME) == 2
    assert losses.update(db, esi, ME) == 2
    assert losses.update(db, esi, ME) == 1


def _played(db, steps, hour, character=ME):
    """A finished session whose plan had these (band, exposure, response) steps."""
    plan = planner.Plan(
        character_id=character, minutes_available=60, items=(), alternatives=(), notes=(),
        net_worth=0.0,
    )  # fmt: skip
    plan_id = planner.record(db, plan, {}).plan_id
    for n, (band, exposure, response) in enumerate(steps):
        item_id = db.execute(
            "INSERT INTO plan_items (plan_id, position, kind, key, title, isk, minutes, "
            "risk_rating, expected_loss, details) VALUES (?, ?, 'mining', ?, 'x', 1, 10, 'green', "
            "0, ?)",
            (plan_id, n, f"k{n}", json.dumps({"risk": {"band": band, "exposure": exposure}})),
        ).lastrowid
        if response:
            planner.respond(db, item_id, response)
    session = sessions.start(db, character, plan_id, now=f"2026-10-17T{hour:02d}:00:00+00:00")
    sessions.finish(db, session.session_id, now=f"2026-10-17T{hour:02d}:59:00+00:00")


def _lose(db, killmail_id, hour, character=ME):
    db.execute(
        "INSERT INTO own_losses VALUES (?, ?, ?, 30000001, ?, '[]', 'x')",
        (killmail_id, character, f"2026-10-17T{hour:02d}:30:00+00:00", VENTURE),
    )


def test_learned_chances_count_trips_and_losses_per_band(db):
    _played(db, [("low", 1e6, None), ("medium", 2e6, "do")], hour=1)
    _played(db, [("low", 1e6, None), ("very_low", 0.0, None)], hour=2)  # nothing at stake
    _played(db, [("low", 1e6, "not_now")], hour=3)  # declined: not a trip
    _lose(db, 1, hour=1)  # during the first: counts against "medium", its riskiest step
    _lose(db, 2, hour=5)  # outside any session
    _lose(db, 3, hour=2, character=ALT)  # another character's: not in Miner's sessions
    learned = risk.model(db)
    low, medium = learned.chances["low"], learned.chances["medium"]
    assert (low.trips, low.losses) == (2, 0)
    assert (medium.trips, medium.losses) == (1, 1)
    assert medium.chance == pytest.approx((0.03 * 100 + 1) / 101)
    assert low.chance == pytest.approx(0.005 * 100 / 102)
    assert learned.chances["very_low"].trips == 0
    assert learned.chances["none"].chance == 0.0
    assert learned.losses_outside_sessions == 2


def test_ratings_say_where_their_chance_came_from(db):
    _played(db, [("low", 1e6, None)], hour=1)
    rating = risk.rate("low", 1_000_000.0, 1e8, model=risk.model(db))
    assert rating.trips == 1
    assert "learned from 1 of your trips" in explain.risk_summary(rating.as_dict())
    guess = risk.rate("low", 1_000_000.0, 1e8)
    assert "starting guess" in explain.risk_summary(guess.as_dict())
    text = explain.describe("loss_chances_learned", {"trips": 12, "losses": 1})
    assert text.startswith("Loss chances are learned from 12 of your trips and 1 loss,")
