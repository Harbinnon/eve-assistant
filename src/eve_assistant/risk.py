"""The risk matrix: how likely a loss is, against how much is exposed.

Design: docs/design.md section 4.8. Exposure is measured as a share of the
character's net worth, so losing 20M means more to a new character than to
an established one. The chance band of a route or system comes from its
security and, while they're fresh, the live signals (signals.py): ships
lost along it in the last hour, camps and incursions. The payout EVE makes
for a lost hull is taken off the expected loss.

The loss chances below are starting guesses. loss_chances() learns from the
player's own losses (losses.py) against the trips plans rated: each step of
a finished session that wasn't declined, with something at stake, is a trip
in its band, and a loss during that session counts against the riskiest of
its steps. The starting guess keeps PRIOR_TRIPS trips' weight, so a single
loss can't swing a chance far:

    chance = (guess * PRIOR_TRIPS + losses) / (PRIOR_TRIPS + trips)

Every rating says whether its chance is still a guess or how many trips it
was learned from. "none" (Exordium) stays at zero: no player can attack there.

The net-worth lines move with the player's choices (design 4.6, risk
appetite). Each Caution or Risky step with something at stake is a vote:
taken (answered "do", or left unanswered in a session that was played) or
declined ("not now" or muted). The lines are scaled by

    2 ** ((taken - declined) / (taken + declined + PRIOR_VOTES))

so between half and twice where they started: wider (fewer cautions) for a
player who takes such steps, narrower for one who declines them.
"""

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, field

from eve_assistant import goals, losses, routes, signals, snapshots, worth
from eve_assistant.skills import Reason

# "none": Exordium, where PvP is switched off, so other players cannot take anything.
BANDS = ("none", "very_low", "low", "medium", "high")

# Starting guesses: chance of losing what is exposed, per trip or session.
LOSS_CHANCE = {"none": 0.0, "very_low": 0.001, "low": 0.005, "medium": 0.03, "high": 0.10}

# Exposed share of net worth: upper bounds of the four columns.
SHARE_COLUMNS = (0.02, 0.10, 0.25)

PRIOR_TRIPS = 100.0  # how many trips' weight the starting guesses keep
PRIOR_VOTES = 5.0  # how many choices' weight the starting lines keep

MATRIX = {
    "none": ("green", "green", "green", "green"),
    "very_low": ("green", "green", "amber", "red"),
    "low": ("green", "amber", "amber", "red"),
    "medium": ("amber", "amber", "red", "red"),
    "high": ("amber", "red", "red", "red"),
}


@dataclass(frozen=True)
class RiskRating:
    band: str
    rating: str  # green, amber, red
    exposure: float
    share_of_net_worth: float
    expected_loss: float
    payout: float = 0.0  # what EVE pays for the hull if it's lost
    chance: float = 0.0
    trips: int = 0  # the chance was learned from this many trips; 0 = a starting guess

    def as_dict(self) -> dict:
        return {
            "band": self.band,
            "rating": self.rating,
            "exposure": self.exposure,
            "share_of_net_worth": self.share_of_net_worth,
            "expected_loss": self.expected_loss,
            "payout": self.payout,
            "chance": self.chance,
            "trips": self.trips,
            "chance_is_a_guess": self.trips == 0,
        }


@dataclass(frozen=True)
class Chance:
    """A band's chance of loss per trip, and what it was learned from."""

    band: str
    chance: float
    trips: int = 0
    losses: int = 0

    @property
    def learned(self) -> bool:
        return self.trips > 0


@dataclass(frozen=True)
class Model:
    """What ratings use: each band's chance, and the net-worth columns."""

    chances: dict[str, Chance] = field(
        default_factory=lambda: {band: Chance(band, LOSS_CHANCE[band]) for band in BANDS}
    )
    columns: tuple[float, ...] = SHARE_COLUMNS
    losses_outside_sessions: int = 0
    taken: int = 0  # Caution or Risky steps taken
    declined: int = 0  # and declined

    @property
    def lines_moved(self) -> float:
        """How far the lines are from where they started (1 = not at all)."""
        return self.columns[0] / SHARE_COLUMNS[0]


@dataclass(frozen=True)
class Assessment:
    """A route's or system's chance band, and what it was decided from."""

    band: str
    reasons: tuple[Reason, ...] = ()


def band_for_security(lowest_security: float | None, region_name: str | None = None) -> str:
    if region_name == "Exordium":
        return "none"  # PvP is switched off there
    if lowest_security is None:
        return "high"
    rounded = round(lowest_security, 1)
    if rounded >= 0.5:
        return "low"
    if rounded > 0.0:
        return "medium"
    return "high"


def _names(db: sqlite3.Connection, system_ids) -> list[str]:
    found = []
    for system_id in system_ids:
        row = db.execute(
            "SELECT name FROM sde_solar_systems WHERE system_id = ?", (system_id,)
        ).fetchone()
        found.append(row["name"] if row else str(system_id))
    return found


def _worse(a: str, b: str) -> str:
    return max(a, b, key=BANDS.index)


def _by_kills(lowest: float, any_kills: bool) -> str:
    """Quiet or busy, by security: highsec very low or low, lowsec low or medium."""
    rounded = round(lowest, 1)
    if rounded >= 0.5:
        return "low" if any_kills else "very_low"
    if rounded > 0.0:
        return "medium" if any_kills else "low"
    return "high"


def assess(
    db: sqlite3.Connection, systems, region_name: str | None = None, now: str | None = None
) -> Assessment:
    """The chance band for travelling through, or working in, these systems."""
    systems = [s for s in dict.fromkeys(systems) if s is not None]
    if region_name == "Exordium":
        return Assessment("none")  # PvP is switched off there
    lowest = routes.lowest_security(db, systems) if systems else None
    if lowest is None:
        return Assessment("high")
    if not signals.fresh(db, now):
        return Assessment(band_for_security(lowest))  # the plan notes it once
    kills = signals.kills_in(db, systems)
    band = _by_kills(lowest, bool(kills))
    if kills:
        most = sorted(kills, key=kills.get, reverse=True)[:3]
        reasons = [Reason("recent_kills", {
            "kills": sum(kills.values()),
            "most": [[name, kills[s]] for name, s in zip(_names(db, most), most, strict=True)],
        })]  # fmt: skip
    else:
        reasons = [Reason("no_recent_kills", {"systems": len(systems)})]
    hot = {s: found for s, found in signals.hotspots(db, now).items() if s in systems}
    for system_id, (count, highsec) in sorted(hot.items(), key=lambda h: -h[1][0]):
        name = _names(db, [system_id])[0]
        if highsec:
            band = _worse(band, "medium")
            reasons.append(Reason("hotspot", {"system": name, "kills": count}))
        else:
            band = "high"
            reasons.append(Reason("camped", {"system": name, "kills": count}))
    infested = signals.incursions_in(db, systems)
    if infested:
        band = _worse(band, "medium")
        reasons.append(Reason("incursion", {"systems": _names(db, infested)}))
    return Assessment(band, tuple(reasons))


def rate(
    band: str, exposure: float, net_worth: float, payout: float = 0.0, model: Model | None = None
) -> RiskRating:
    """payout: what EVE pays for the hull if it's lost, taken off the expected loss.
    model: learned chances and columns; the starting guesses without one."""
    model = model or Model()
    chance = model.chances[band]
    if net_worth > 0:
        share = exposure / net_worth
    elif exposure > 0:
        share = 1.0
    else:
        share = 0.0
    column = sum(share > limit for limit in model.columns)
    return RiskRating(
        band=band,
        rating=MATRIX[band][column],
        exposure=exposure,
        share_of_net_worth=share,
        expected_loss=chance.chance * max(0.0, exposure - payout),
        payout=payout,
        chance=chance.chance,
        trips=chance.trips,
    )


def _steps_taken(db: sqlite3.Connection, plan_id: int) -> list[str]:
    """The bands of a plan's steps the player didn't decline, with something at stake."""
    rows = db.execute(
        "SELECT i.details, (SELECT r.response FROM plan_responses r WHERE r.item_id = i.item_id "
        " ORDER BY r.response_id DESC LIMIT 1) AS response "
        "FROM plan_items i WHERE i.plan_id = ? AND i.position IS NOT NULL",
        (plan_id,),
    )
    bands = []
    for row in rows:
        stake = json.loads(row["details"]).get("risk", {})
        if row["response"] in (None, "do") and stake.get("band") in BANDS and stake.get("exposure"):
            bands.append(stake["band"])
    return bands


def trips_and_losses(db: sqlite3.Connection) -> tuple[Counter, dict[int, str | None]]:
    """Trips per band, and each loss's band: the riskiest step of the session
    it happened in, or None outside any recorded session."""
    trips: Counter = Counter()
    windows = []
    for session in db.execute(
        "SELECT s.character_id, s.plan_id, s.started_at, e.ended_at FROM play_sessions s "
        "JOIN play_session_ends e USING (session_id) "
        "WHERE e.how = 'finished' AND s.plan_id IS NOT NULL"
    ).fetchall():
        bands = _steps_taken(db, session["plan_id"])
        trips.update(bands)
        if bands:
            riskiest = max(bands, key=BANDS.index)
            windows.append((session["character_id"], session["started_at"], session["ended_at"],
                            riskiest))  # fmt: skip
    attributed = {
        loss.killmail_id: next(
            (
                b
                for who, start, end, b in windows
                if who == loss.character_id and start <= loss.killed_at <= end
            ),
            None,
        )  # fmt: skip
        for loss in losses.all_losses(db)
    }
    return trips, attributed


def votes(db: sqlite3.Connection) -> tuple[int, int]:
    """Caution and Risky steps with something at stake: (taken, declined)."""
    taken = declined = 0
    played = {
        r[0]
        for r in db.execute(
            "SELECT s.plan_id FROM play_sessions s JOIN play_session_ends e USING (session_id) "
            "WHERE e.how = 'finished' AND s.plan_id IS NOT NULL"
        )
    }
    rows = db.execute(
        "SELECT i.plan_id, i.details, (SELECT r.response FROM plan_responses r "
        " WHERE r.item_id = i.item_id ORDER BY r.response_id DESC LIMIT 1) AS response "
        "FROM plan_items i WHERE i.position IS NOT NULL AND i.risk_rating IN ('amber', 'red')"
    )
    for row in rows:
        if not json.loads(row["details"]).get("risk", {}).get("exposure"):
            continue
        if row["response"] == "do" or (row["response"] is None and row["plan_id"] in played):
            taken += 1
        elif row["response"] in ("not_now", "mute"):
            declined += 1
    return taken, declined


def lines(taken: int, declined: int) -> tuple[float, ...]:
    """The net-worth columns, moved by the player's choices."""
    scale = 2 ** ((taken - declined) / (taken + declined + PRIOR_VOTES))
    return tuple(limit * scale for limit in SHARE_COLUMNS)


def model(db: sqlite3.Connection) -> Model:
    """Ratings as learned so far: chances from your losses and trips, and the
    lines from your choices."""
    trips, attributed = trips_and_losses(db)
    lost = Counter(band for band in attributed.values() if band)
    outside = sum(band is None for band in attributed.values())
    chances = {}
    for band in BANDS:
        learned = (LOSS_CHANCE[band] * PRIOR_TRIPS + lost[band]) / (PRIOR_TRIPS + trips[band])
        chances[band] = Chance(band, 0.0 if band == "none" else learned, trips[band], lost[band])
    taken, declined = votes(db)
    return Model(chances, lines(taken, declined), outside, taken, declined)


def item_value(db: sqlite3.Connection, type_id: int) -> float:
    return goals.price_of(db, type_id) or 0.0


def net_worth(db: sqlite3.Connection, character_id: int) -> float:
    """Wallet plus assets at CCP's average prices (rough, but consistent)."""
    wallet = float(snapshots.latest(db, character_id, "wallet") or 0.0)
    return wallet + worth.asset_value(db, snapshots.latest(db, character_id, "assets") or [])


def ship_exposure(db: sqlite3.Connection, character_id: int) -> float:
    """The active ship's hull plus everything inside it."""
    ship = snapshots.latest(db, character_id, "ship") or {}
    if not ship:
        return 0.0
    assets = snapshots.latest(db, character_id, "assets") or []
    inside = sum(
        item_value(db, a["type_id"]) * a.get("quantity", 1)
        for a in assets
        if a.get("location_id") == ship.get("ship_item_id")
    )
    return item_value(db, ship["ship_type_id"]) + inside
