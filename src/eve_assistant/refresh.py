"""One refresh: snapshot every logged-in character and update prices.

Shared by `eve-assistant snapshot` and the dashboard's Refresh button.
"""

from dataclasses import dataclass, field
from pathlib import Path

from eve_assistant import auth, goals, outcomes, planner, snapshots, store, structures
from eve_assistant.config import Config
from eve_assistant.esi.client import EsiClient, EsiError, EsiRateLimited
from eve_assistant.esi.sso import SSOError
from eve_assistant.secrets_store import SecretStore


@dataclass
class RefreshReport:
    lines: list[str] = field(default_factory=list)  # one per character, for display
    problems: list[str] = field(default_factory=list)
    priced: int = 0


def refresh_all(
    config: Config, secrets: SecretStore, db_path: Path | str = store.DEFAULT_DB_PATH
) -> RefreshReport:
    report = RefreshReport()
    characters = auth.list_characters(secrets)
    if not characters:
        report.problems.append("No characters logged in. Run: eve-assistant login")
        return report
    db = store.connect(db_path)
    try:
        with auth.http_client(config) as client:
            tokens = auth.AccessTokens(config, secrets, client)
            esi = EsiClient(client, db, token_for=tokens.get)
            for character in characters:
                snapshots.ensure_character(db, character.character_id, character.name)
                try:
                    line, problems = _snapshot_one(db, esi, character.character_id, character.name)
                except EsiRateLimited as exc:
                    report.problems.append(_rate_limited(exc))
                    return report
                except SSOError as exc:
                    report.problems.append(f"{character.name}: {exc}")
                    continue
                report.lines.append(line)
                report.problems.extend(problems)
            try:
                report.priced = goals.update_prices(db, esi)
            except EsiError as exc:
                report.problems.append(f"Market prices: {exc}")
    finally:
        db.close()
    return report


def _rate_limited(exc: EsiRateLimited) -> str:
    return f"{exc} Try again in about {int(exc.retry_after)} seconds."


def _snapshot_one(db, esi: EsiClient, character_id: int, name: str) -> tuple[str, list[str]]:
    """Snapshot one character. Returns a summary line and problem lines."""
    results = snapshots.collect(db, esi, character_id)
    structures.refresh(db, esi, character_id)
    outcomes.update(db, character_id)  # newer data may settle earlier sessions
    failed = [r for r in results if r.outcome == "failed"]
    changed = sum(r.outcome == "changed" for r in results)
    line = (
        f"{name}: {changed} changed, "
        f"{len(results) - changed - len(failed)} unchanged, {len(failed)} failed"
    )
    return line, [f"{name}, {r.kind}: {r.detail}" for r in failed]


def snapshot_character(
    config: Config,
    secrets: SecretStore,
    character_id: int,
    db_path: Path | str = store.DEFAULT_DB_PATH,
) -> list[str]:
    """Snapshot one character, e.g. at the start and end of a play session.
    Returns problem lines; empty when everything was collected."""
    if character_id not in {c.character_id for c in auth.list_characters(secrets)}:
        return ["This character is not logged in, so no snapshot was taken."]
    db = store.connect(db_path)
    try:
        row = db.execute(
            "SELECT name FROM characters WHERE character_id = ?", (character_id,)
        ).fetchone()
        name = row["name"] if row else str(character_id)
        with auth.http_client(config) as client:
            esi = EsiClient(client, db, token_for=auth.AccessTokens(config, secrets, client).get)
            try:
                return _snapshot_one(db, esi, character_id, name)[1]
            except EsiRateLimited as exc:
                return [_rate_limited(exc)]
            except (SSOError, EsiError) as exc:
                return [f"{name}: {exc}"]
    finally:
        db.close()


def plan_now(
    config: Config,
    secrets: SecretStore,
    character_id: int,
    minutes: float,
    allow_lowsec: bool = False,
    allow_risky: bool = False,
    db_path: Path | str = store.DEFAULT_DB_PATH,
) -> int:
    """Build and record a plan with live market data. Returns the plan ID."""
    db = store.connect(db_path)
    try:
        with auth.http_client(config) as client:
            esi = EsiClient(client, db, token_for=auth.AccessTokens(config, secrets, client).get)
            plan = planner.build_plan(
                db, esi, character_id, minutes, allow_lowsec=allow_lowsec, allow_risky=allow_risky
            )
            plan = planner.record(
                db, plan, {"allow_lowsec": allow_lowsec, "risky": allow_risky, "source": "web"}
            )
        return plan.plan_id
    finally:
        db.close()
