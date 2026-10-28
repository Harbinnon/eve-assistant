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
    alerts,
    auth,
    backups,
    campaigns,
    contracts,
    corporation,
    freelance,
    gamelogs,
    goals,
    industry,
    losses,
    market,
    market_data,
    outcomes,
    planets,
    planner,
    sde,
    signals,
    snapshots,
    sovereignty,
    store,
    structures,
    trade,
)
from eve_assistant.config import Config
from eve_assistant.esi import PLANETS_SCOPE
from eve_assistant.esi.client import EsiClient, EsiError, EsiRateLimited
from eve_assistant.esi.sso import SSOError
from eve_assistant.secrets_store import SecretStore

ESI_LOCK = threading.Lock()
MARKET_LOCK = threading.Lock()  # market scans: their own lock, so refreshes aren't held up
# Fees assumed when picking items to check the daily volume of (any character's are close).
DEFAULT_TRADE_FEES = trade.Fees(0.075, {hub.station_id: 0.03 for hub in market.HUBS})


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
            report.problems.extend(_refresh_signals(db, esi, datetime.now(UTC)))
            report.problems.extend(_refresh_sovereignty(db, esi, datetime.now(UTC)))
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


def _hubs_to_scan(db, now: datetime, force: bool) -> list:
    """Hubs whose data is old: over 30 minutes, or over 5 when asked to refresh now."""
    if not force:
        return market_data.stale(db, now)
    found = []
    for hub in market.HUBS:
        at = market_data.scanned_at(db, hub)
        if at is None or now - datetime.fromisoformat(at) >= FORCE_AFTER:
            found.append(hub)
    return found


def scan_markets(
    config: Config,
    db_path: Path | str = store.DEFAULT_DB_PATH,
    force: bool = False,
    now: datetime | None = None,
) -> list[str]:
    """Scan the trade hubs whose data is old, then fetch daily volumes for items
    worth listing. One scan at a time; a second ask while one runs does nothing.
    Returns problem lines."""
    if not MARKET_LOCK.acquire(blocking=False):
        return []
    try:
        now = now or datetime.now(UTC)
        db = store.connect(db_path)
        try:
            with auth.http_client(config) as http:
                esi = EsiClient(http, db)
                problems = market_data.scan(db, esi, now, _hubs_to_scan(db, now, force))
                wanted: dict[int, list[int]] = {}
                for found in (
                    trade.listing_candidates(db, DEFAULT_TRADE_FEES),
                    trade.station_volume_candidates(db, DEFAULT_TRADE_FEES),
                ):
                    for region_id, type_ids in found.items():
                        wanted.setdefault(region_id, []).extend(type_ids)
                for region_id, type_ids in wanted.items():
                    market_data.volumes(db, esi, region_id, type_ids, now)
                return problems
        finally:
            db.close()
    finally:
        MARKET_LOCK.release()


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
    problems = []
    if kinds is None or "killmails" in kinds:
        try:
            losses.update(db, esi, character_id)
        except EsiError as exc:
            problems.append(f"{name}, losses: {exc}")
    problems.extend(_extras(db, esi, character_id, name, kinds, granted))
    contracts.sync_projects(db, character_id)  # delivered couriers close their projects
    outcomes.update(db, character_id)  # newer data may settle earlier sessions
    failed = [r for r in results if r.outcome == "failed"]
    changed = sum(r.outcome == "changed" for r in results)
    line = (
        f"{name}: {changed} changed, "
        f"{len(results) - changed - len(failed)} unchanged, {len(failed)} failed"
    )
    return line, [*(f"{name}, {r.kind}: {r.detail}" for r in failed), *problems]


def _extras(db, esi: EsiClient, character_id: int, name: str, kinds, granted) -> list[str]:
    """The character's corporation (after its public record) and its colonies'
    layouts (after the colony list). Returns problem lines."""
    problems = []
    if kinds is None or "public" in kinds:
        try:
            corporation.refresh(db, esi, character_id)
        except EsiError as exc:
            problems.append(f"{name}, corporation: {exc}")
    if (kinds is None or "planets" in kinds) and (granted is None or PLANETS_SCOPE in granted):
        try:
            planets.refresh(db, esi, character_id)
        except EsiError as exc:
            problems.append(f"{name}, planets: {exc}")
    return problems


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
    granted, name = logged_in[character_id].scopes, logged_in[character_id].name
    db = store.connect(db_path)
    try:
        # A character added a moment ago isn't in the database yet; its snapshots need it.
        snapshots.ensure_character(db, character_id, name)
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
FORCE_AFTER = timedelta(minutes=5)  # ESI caches market orders this long
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


def _refresh_signals(db, esi: EsiClient, now: datetime) -> list[str]:
    """Kills, incursions and insurance prices; a problem line if ESI fails."""
    try:
        signals.refresh(db, esi, now)
    except EsiError as exc:
        return [f"Risk signals: {exc}"]
    return []


def _refresh_sovereignty(db, esi: EsiClient, now: datetime) -> list[str]:
    """Who holds each system, twice a day; a problem line if ESI fails."""
    if not sovereignty.due(db, now):
        return []
    try:
        sovereignty.refresh(db, esi, now)
    except EsiError as exc:
        return [f"Sovereignty: {exc}"]
    return []


def _public_due(db, now: datetime) -> bool:
    return (
        _prices_due(db, now)
        or _freelance_due(db, now)
        or signals.due(db, now)
        or industry.due(db, now)
        or campaigns.due(db, now)
        or sovereignty.due(db, now)
    )


def _refresh_public(db, esi: EsiClient, now: datetime) -> list[str]:
    """Market prices, Freelance Jobs and risk signals, each when it's due."""
    problems = []
    if _prices_due(db, now):
        try:
            goals.update_prices(db, esi)
        except EsiError as exc:
            problems.append(f"Market prices: {exc}")
    if _freelance_due(db, now):
        problems.extend(_refresh_freelance(db, esi, now))
    if signals.due(db, now):
        problems.extend(_refresh_signals(db, esi, now))
    if industry.due(db, now):
        try:
            industry.refresh(db, esi, now)
        except EsiError as exc:
            problems.append(f"Industry cost indices: {exc}")
    if campaigns.due(db, now):
        try:
            campaigns.refresh(db, esi, now)
        except EsiError as exc:
            problems.append(f"Military campaigns: {exc}")
    problems.extend(_refresh_sovereignty(db, esi, now))
    return problems


def _check_static_data(config: Config, db_path, now: datetime) -> list[str]:
    """Reload the static data if a newer loader needs it (from the zip already on
    disk), and once a day note which build CCP has published."""
    db = store.connect(db_path)
    try:
        sde.reload_if_needed(db)
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
        if not work and not _public_due(db, now):
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
            report.problems.extend(_refresh_public(db, esi, now))
        outcomes.update(db)
        report.problems.extend(send_alerts(config, secrets, db, report.problems, now))
    finally:
        db.close()
    return report


def send_alerts(config: Config, secrets: SecretStore, db, problems: list[str], now) -> list[str]:
    """Game alerts to Discord and this tick's problems to ntfy, when set up
    (roadmap 7.1). Returns problem lines about sending."""
    if not alerts.addresses(secrets):
        return []
    found = alerts.game_alerts(db, now) + alerts.health_alerts(problems, now)
    # Links point where the owner can open them: the hosted copy's public address.
    dashboard = config.hosted.public_origin if config.hosted else alerts.DEFAULT_DASHBOARD
    with auth.http_client(config) as http:  # carries the descriptive User-Agent
        return alerts.send(db, secrets, http, found, dashboard)[1]
