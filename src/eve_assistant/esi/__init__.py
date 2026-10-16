"""Talking to CCP's API (ESI) and login service (EVE SSO)."""

from eve_assistant import __version__

# ESI versions its behaviour by date. Pinned, and moved deliberately after
# checking what changed (GET https://esi.evetech.net/meta/compatibility-dates).
COMPATIBILITY_DATE = "2026-08-18"

ESI_BASE_URL = "https://esi.evetech.net"

# Every scope the tool asks for. Read-only, each justified in docs/design.md
# section 9. Must match what the ESI application has enabled.
SCOPES = (
    "esi-skills.read_skills.v1",
    "esi-skills.read_skillqueue.v1",
    "esi-clones.read_clones.v1",
    "esi-clones.read_implants.v1",
    "esi-assets.read_assets.v1",
    "esi-wallet.read_character_wallet.v1",
    "esi-industry.read_character_mining.v1",
    "esi-industry.read_character_jobs.v1",
    "esi-characters.read_blueprints.v1",
    "esi-characters.read_standings.v1",
    "esi-markets.read_character_orders.v1",
    "esi-markets.structure_markets.v1",
    "esi-contracts.read_character_contracts.v1",
    "esi-location.read_location.v1",
    "esi-location.read_ship_type.v1",
    "esi-location.read_online.v1",
    "esi-fittings.read_fittings.v1",
    "esi-universe.read_structures.v1",
)


def user_agent(contact: str | None) -> str:
    """CCP asks for a descriptive User-Agent with a way to reach the operator."""
    base = f"eve-assistant/{__version__} (personal planner)"
    return f"{base} contact: {contact}" if contact else base
