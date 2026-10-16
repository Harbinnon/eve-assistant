"""Market fees and refining tax.

Rules as documented by EVE University (Tax and Reprocessing pages, checked
October 2026; sales tax per patch 22.02). They live here, in one place, so a
balance change is a one-file edit.
"""

import sqlite3
from dataclasses import dataclass

from eve_assistant import snapshots

SALES_TAX_BASE = 0.075
SALES_TAX_CUT_PER_ACCOUNTING = 0.11  # of the base, per level

BROKER_FEE_BASE = 0.03
BROKER_FEE_CUT_PER_LEVEL = 0.003
BROKER_FEE_PER_FACTION_STANDING = 0.0003
BROKER_FEE_PER_CORP_STANDING = 0.0002
BROKER_FEE_MINIMUM = 0.01

# Exordium (the starter region) adds this to non-instant market orders.
EXORDIUM_ORDER_SURCHARGE = 0.05

REPROCESS_TAX_FULL_STANDING = 6.67  # standing at which NPC refining tax reaches 0


def sales_tax(accounting_level: int) -> float:
    return SALES_TAX_BASE * (1 - SALES_TAX_CUT_PER_ACCOUNTING * accounting_level)


def broker_fee(broker_relations: int, faction_standing: float, corp_standing: float) -> float:
    fee = (
        BROKER_FEE_BASE
        - BROKER_FEE_CUT_PER_LEVEL * broker_relations
        - BROKER_FEE_PER_FACTION_STANDING * max(faction_standing, 0)
        - BROKER_FEE_PER_CORP_STANDING * max(corp_standing, 0)
    )
    return max(BROKER_FEE_MINIMUM, fee)


def reprocess_tax(station_take: float, standing: float) -> float:
    """NPC station refining tax: full at 0 standing, none at 6.67."""
    share = min(max(standing, 0) / REPROCESS_TAX_FULL_STANDING, 1.0)
    return station_take * (1 - share)


@dataclass(frozen=True)
class Standings:
    """A character's unmodified standings, from its standings snapshot."""

    by_id: dict[int, float]

    def toward(self, entity_id: int | None) -> float:
        return self.by_id.get(entity_id, 0.0) if entity_id is not None else 0.0


def load_standings(db: sqlite3.Connection, character_id: int) -> Standings:
    rows = snapshots.latest(db, character_id, "standings") or []
    return Standings({r["from_id"]: float(r["standing"]) for r in rows})


def station_owner(db: sqlite3.Connection, station_id: int) -> tuple[int | None, int | None]:
    """(owner corporation, its faction) for an NPC station."""
    row = db.execute(
        "SELECT st.owner_id, c.faction_id FROM sde_npc_stations st "
        "LEFT JOIN sde_npc_corporations c ON c.corporation_id = st.owner_id "
        "WHERE st.station_id = ?",
        (station_id,),
    ).fetchone()
    return (row[0], row[1]) if row else (None, None)
