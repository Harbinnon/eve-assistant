"""Refreshing from EVE: snapshots, prices, game logs (and the daily backup).

`refresh_all` is the Refresh button and `eve-assistant snapshot`;
`auto_refresh_once` is one tick of the dashboard's background refresh, which
fetches only what is due. Everything that talks to ESI goes through one lock,
so a background tick and a button press never refresh login tokens at once.
"""

import functools
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from eve_assistant import (
    auth,
    backups,
    contracts,
    freelance,
    gamelogs,
    goals,
    outcomes,
    planner,
    sde,
    snapshots,
    store,
    structures,
)
from eve_assistant.config import Config
from eve_assistant.esi.client import EsiClient, EsiError, EsiRateLimited
from eve_assistant.esi.sso import SSOError
from eve_assistant.secrets_store import SecretStore

ESI_LOCK = threading.Lock()


def _one_at_a_time(function):
    @functools.wraps(function)
    def locked(*args, **kwargs):
        with ESI_LOCK:
            return function(*args, **kwargs)

    return locked


@dataclass
class RefreshReport:
    lines: list[str] = field(default_factory=list)  # one per character, for display
    problems: list[str] = field(default_factory=list)
    priced: int = 0


@_one_at_a_time
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
        report.problems.extend(read_game_logs(config, db))
        with auth.http_client(config) as client:
            tokens = auth.AccessTokens(config, secrets, client)
            esi = EsiClient(client, db, token_for=tokens.get)
            for character in characters:
                snapshots.ensure_character(db, character.character_id, character.name)
                try:
                    line, problems = _snapshot_one(
                        db, esi, character.character_id, character.name, granted=character.scopes
                    )
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


def read_game_logs(config: Config, db) -> list[str]:
    """Import new mining lines from EVE's game logs, if a folder is configured.
    Done before snapshots, so the outcomes worked out after them include it."""
    if config.gamelogs_dir is None or config.mode != "local":
        return []
    if not config.gamelogs_dir.is_dir():
        return [
            f"EVE's game log folder wasn't found at {config.gamelogs_dir} "
            "(see First run in the README)."
        ]
    gamelogs.import_folder(db, config.gamelogs_dir)
    return []


def _rate_limited(exc: EsiRateLimited) -> str:
    return f"{exc} Try again in about {int(exc.retry_after)} seconds."


def _snapshot_one(
    db,
    esi: EsiClient,
    character_id: int,
    name: str,
    kinds: set[str] | None = None,
    granted: tuple[str, ...] | None = None,
) -> tuple[str, list[str]]:
    """Snapshot one character (every kind, or only `kinds`). Returns a summary
    line and problem lines."""
    results = snapshots.collect(db, esi, character_id, kinds, granted)
    if kinds is None or "location" in kinds or "assets" in kinds:
        structures.refresh(db, esi, character_id)
    contracts.sync_projects(db, character_id)  # delivered couriers close their projects
    outcomes.update(db, character_id)  # newer data may settle earlier sessions
    failed = [r for r in results if r.outcome == "failed"]
    changed = sum(r.outcome == "changed" for r in results)
    line = (
        f"{name}: {changed} changed, "
        f"{len(results) - changed - len(failed)} unchanged, {len(failed)} failed"
    )
    return line, [f"{name}, {r.kind}: {r.detail}" for r in failed]


@_one_at_a_time
def snapshot_character(
    config: Config,
    secrets: SecretStore,
    character_id: int,
    db_path: Path | str = store.DEFAULT_DB_PATH,
) -> list[str]:
    """Snapshot one character, e.g. at the start and end of a play session.
    Returns problem lines; empty when everything was collected."""
    logged_in = {c.character_id: c for c in auth.list_characters(secrets)}
    if character_id not in logged_in:
        return ["This character is not logged in, so no snapshot was taken."]
    granted = logged_in[character_id].scopes
    db = store.connect(db_path)
    try:
        row = db.execute(
            "SELECT name FROM characters WHERE character_id = ?", (character_id,)
        ).fetchone()
        name = row["name"] if row else str(character_id)
        log_problems = read_game_logs(config, db)
        with auth.http_client(config) as client:
            esi = EsiClient(client, db, token_for=auth.AccessTokens(config, secrets, client).get)
            try:
                problems = _snapshot_one(db, esi, character_id, name, granted=granted)[1]
                return log_problems + problems
            except EsiRateLimited as exc:
                return [*log_problems, _rate_limited(exc)]
            except (SSOError, EsiError) as exc:
                return [*log_problems, f"{name}: {exc}"]
    finally:
        db.close()


@_one_at_a_time
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


# --- background refresh ------------------------------------------------------

# How often each kind is fetched in the background. EVE caches most data
# anyway (wallet 2 minutes, mining ledger 10, journal and assets an hour), so
# asking more often than this gains nothing.
FAST_KINDS = frozenset({"wallet", "location", "ship"})
MEDIUM_KINDS = frozenset({"mining_ledger", "industry_jobs", "orders", "skillqueue", "contracts"})
ONLINE_FAST = timedelta(minutes=2)
ONLINE_MEDIUM = timedelta(minutes=15)
EVERYTHING = timedelta(hours=1)
ONLINE_CHECK = timedelta(minutes=5)  # how soon a character logging in is noticed
PRICES_EVERY = timedelta(hours=6)
FREELANCE_EVERY = timedelta(minutes=30)
FREELANCE_SETTING = "freelance_checked"


def interval(kind: str, online: bool) -> timedelta:
    if kind == "online":
        return ONLINE_FAST if online else ONLINE_CHECK
    if online and kind in FAST_KINDS:
        return ONLINE_FAST
    if online and kind in MEDIUM_KINDS:
        return ONLINE_MEDIUM
    return EVERYTHING


def due_kinds(db, character_id: int, now: datetime) -> set[str]:
    """Kinds whose last check is older than their interval."""
    online = bool((snapshots.latest(db, character_id, "online") or {}).get("online"))
    checked = snapshots.last_checked(db, character_id)
    due = set()
    for kind in snapshots.KINDS:
        last = checked.get(kind.name)
        if last is None or now - datetime.fromisoformat(last) >= interval(kind.name, online):
            due.add(kind.name)
    return due


@dataclass
class AutoReport:
    checked: dict[str, int] = field(default_factory=dict)  # character name -> kinds fetched
    problems: list[str] = field(default_factory=list)
    retry_after: float | None = None  # seconds, when EVE asked us to slow down


def _freelance_due(db, now: datetime) -> bool:
    row = db.execute("SELECT value FROM settings WHERE key = ?", (FREELANCE_SETTING,)).fetchone()
    return row is None or now - datetime.fromisoformat(row[0]) >= FREELANCE_EVERY


def _refresh_freelance(db, esi: EsiClient, now: datetime) -> list[str]:
    """Freelance Jobs' public listing; a problem line if ESI fails."""
    try:
        freelance.refresh(db, esi)
    except EsiError as exc:
        return [f"Freelance Jobs: {exc}"]
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (FREELANCE_SETTING, now.isoformat(timespec="seconds")),
    )
    db.commit()
    return []


def _prices_due(db, now: datetime) -> bool:
    row = db.execute("SELECT MAX(fetched_at) FROM market_prices").fetchone()
    return row[0] is None or now - datetime.fromisoformat(row[0]) >= PRICES_EVERY


def _check_static_data(config: Config, db_path, now: datetime) -> list[str]:
    """Once a day, note which static data build CCP has published."""
    db = store.connect(db_path)
    try:
        if not sde.check_due(db, now):
            return []
        with auth.http_client(config) as http:
            sde.check_for_update(db, http, now)
    except sde.SdeError as exc:
        return [str(exc)]
    finally:
        db.close()
    return []


@_one_at_a_time
def auto_refresh_once(
    config: Config,
    secrets: SecretStore,
    db_path: Path | str = store.DEFAULT_DB_PATH,
    now: datetime | None = None,
) -> AutoReport:
    """One background tick: fetch whatever is due for each logged-in character."""
    report = AutoReport()
    now = now or datetime.now(UTC)
    report.problems.extend(backups.run_if_due(config.backups_dir, db_path, now))
    report.problems.extend(_check_static_data(config, db_path, now))
    characters = auth.list_characters(secrets)
    if not characters:
        return report
    db = store.connect(db_path)
    try:
        report.problems.extend(read_game_logs(config, db))
        work = []
        for character in characters:
            snapshots.ensure_character(db, character.character_id, character.name)
            due = due_kinds(db, character.character_id, now)
            if due:
                work.append((character, due))
        if not work and not _prices_due(db, now) and not _freelance_due(db, now):
            outcomes.update(db)
            return report
        with auth.http_client(config) as client:
            esi = EsiClient(client, db, token_for=auth.AccessTokens(config, secrets, client).get)
            for character, due in work:
                try:
                    _line, problems = _snapshot_one(
                        db, esi, character.character_id, character.name, due, character.scopes
                    )
                except EsiRateLimited as exc:
                    report.problems.append(_rate_limited(exc))
                    report.retry_after = exc.retry_after
                    return report
                except SSOError as exc:
                    report.problems.append(f"{character.name}: {exc}")
                    continue
                report.checked[character.name] = len(due)
                report.problems.extend(problems)
            if _prices_due(db, now):
                try:
                    goals.update_prices(db, esi)
                except EsiError as exc:
                    report.problems.append(f"Market prices: {exc}")
            if _freelance_due(db, now):
                report.problems.extend(_refresh_freelance(db, esi, now))
        outcomes.update(db)
    finally:
        db.close()
    return report
