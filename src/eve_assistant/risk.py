"""The risk matrix: how likely a loss is, against how much is exposed.

Design: docs/design.md section 4.8. Exposure is measured as a share of the
character's net worth, so losing 20M means more to a new character than to
an established one. The chance band of a route or system comes from its
security and, while they're fresh, the live signals (signals.py): ships
lost along it in the last hour, camps and incursions. The payout EVE makes
for a lost hull is taken off the expected loss.

ALL loss probabilities below are starting guesses, flagged as such in every
result, and are meant to be replaced by learned values.
"""

import sqlite3
from dataclasses import dataclass

from eve_assistant import goals, routes, signals, snapshots, worth
from eve_assistant.skills import Reason

# "none": Exordium, where PvP is switched off, so other players cannot take anything.
BANDS = ("none", "very_low", "low", "medium", "high")

# Starting guesses: chance of losing what is exposed, per trip or session.
LOSS_CHANCE = {"none": 0.0, "very_low": 0.001, "low": 0.005, "medium": 0.03, "high": 0.10}

# Exposed share of net worth: upper bounds of the four columns.
SHARE_COLUMNS = (0.02, 0.10, 0.25)

# Ships and pods lost in one system in the last hour from which it counts as camped.
CAMP_KILLS = 5

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

    def as_dict(self) -> dict:
        return {
            "band": self.band,
            "rating": self.rating,
            "exposure": self.exposure,
            "share_of_net_worth": self.share_of_net_worth,
            "expected_loss": self.expected_loss,
            "payout": self.payout,
            "chance_is_a_guess": True,
        }


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
        reasons = [Reason("recent_kills", {"kills": sum(kills.values()),
                                           "systems": _names(db, kills)})]  # fmt: skip
    else:
        reasons = [Reason("no_recent_kills", {"systems": len(systems)})]
    camped = {s: n for s, n in kills.items() if n >= CAMP_KILLS}
    if camped:
        band = "high"
        worst = max(camped, key=camped.get)
        reasons.append(Reason("camped", {"system": _names(db, [worst])[0], "kills": camped[worst]}))
    infested = signals.incursions_in(db, systems)
    if infested:
        band = _worse(band, "medium")
        reasons.append(Reason("incursion", {"systems": _names(db, infested)}))
    return Assessment(band, tuple(reasons))


def rate(band: str, exposure: float, net_worth: float, payout: float = 0.0) -> RiskRating:
    """payout: what EVE pays for the hull if it's lost, taken off the expected loss."""
    if net_worth > 0:
        share = exposure / net_worth
    elif exposure > 0:
        share = 1.0
    else:
        share = 0.0
    column = sum(share > limit for limit in SHARE_COLUMNS)
    return RiskRating(
        band=band,
        rating=MATRIX[band][column],
        exposure=exposure,
        share_of_net_worth=share,
        expected_loss=LOSS_CHANCE[band] * max(0.0, exposure - payout),
        payout=payout,
    )


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
