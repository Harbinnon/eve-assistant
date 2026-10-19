"""Alerts (roadmap 7.1): things to do in game to Discord, the tool's own
health to ntfy, nothing else, and each only once.

Discord (a channel's webhook) gets what needs action in game, each with a
link back to the dashboard:

- an industry job has finished;
- a market order has been undercut or outbid;
- a skill queue is empty;
- a courier contract you accepted is due within COURIER_WARNING;
- an extractor on a planet has stopped.

ntfy (a topic's address) gets the tool's health: the problems a background
refresh reports (a login that can't be refreshed, ESI errors, being asked to
slow down), each at most once an hour.

Both addresses let whoever holds them post, so they are secrets: they live in
the secret store, entered through `eve-assistant alerts-setup` (a hidden
prompt), never in config, the database or chat. Every alert sent is kept in
sent_alerts (append-only), and its key stops it being sent twice.
"""

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from eve_assistant import planets, snapshots, trade
from eve_assistant.secrets_store import SecretStore
from eve_assistant.store import utc_now

COURIER_WARNING = timedelta(hours=6)
STORE_ENTRY = "alerts"
DISCORD_PREFIXES = ("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/")
DEFAULT_DASHBOARD = "http://127.0.0.1:8700"


class AlertError(Exception):
    pass


@dataclass(frozen=True)
class Alert:
    key: str  # what it's about; sent once
    channel: str  # discord or ntfy
    text: str
    path: str = "/today"  # the dashboard page it links to


def _at(text: str | None) -> datetime | None:
    if not text:
        return None
    moment = datetime.fromisoformat(text)
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def addresses(store: SecretStore) -> dict[str, str]:
    """The saved Discord webhook and ntfy topic, by channel."""
    return dict(store.load().get(STORE_ENTRY, {}))


def save_address(store: SecretStore, channel: str, address: str | None) -> None:
    """Save (or, with None, forget) a channel's address. Raises AlertError."""
    if channel not in ("discord", "ntfy"):
        raise AlertError(f"Unknown channel '{channel}'.")
    if address is not None:
        address = address.strip()
        if channel == "discord" and not address.startswith(DISCORD_PREFIXES):
            raise AlertError(
                "That isn't a Discord webhook address (https://discord.com/api/webhooks/…)."
            )
        if channel == "ntfy" and not address.startswith("https://"):
            raise AlertError("The ntfy topic's address must start with https://.")
    data = store.load()
    saved = data.setdefault(STORE_ENTRY, {})
    if address is None:
        saved.pop(channel, None)
    else:
        saved[channel] = address
    store.save(data)


def _names(db: sqlite3.Connection) -> dict[int, str]:
    return {r[0]: r[1] for r in db.execute("SELECT character_id, name FROM characters")}


def _jobs(db, character_id: int, name: str, now: datetime) -> list[Alert]:
    found = []
    for job in snapshots.latest(db, character_id, "industry_jobs") or []:
        end = _at(job.get("end_date"))
        if job.get("status") in ("active", "ready") and end and end <= now:
            text = f"{name}: an industry job has finished. Deliver it in game."
            found.append(Alert(f"job:{job['job_id']}", "discord", text))
    return found


def _orders(db, character_id: int, name: str) -> list[Alert]:
    return [
        Alert(
            f"outbid:{w.order_id}",
            "discord",
            f"{name}: your {'buy' if w.is_buy else 'sell'} order for {w.name} in {w.place} has "
            f"been {'outbid' if w.is_buy else 'undercut'}.",
            "/trade",
        )
        for w in trade.watch_orders(db, character_id)
        if w.outbid
    ]


def _queue(db, character_id: int, name: str) -> list[Alert]:
    row = db.execute(
        "SELECT snapshot_id, payload FROM snapshots WHERE character_id = ? AND kind = 'skillqueue' "
        "ORDER BY snapshot_id DESC LIMIT 1",
        (character_id,),
    ).fetchone()
    if row is None or snapshots.latest(db, character_id, "skillqueue"):
        return []
    return [Alert(f"queue:{character_id}:{row['snapshot_id']}", "discord",
                  f"{name}: the skill queue is empty.", "/skills")]  # fmt: skip


def _couriers(db, character_id: int, name: str, now: datetime) -> list[Alert]:
    found = []
    for c in snapshots.latest(db, character_id, "contracts") or []:
        accepted = _at(c.get("date_accepted"))
        if (
            c.get("type") != "courier"
            or c.get("status") != "in_progress"
            or c.get("acceptor_id") != character_id
            or accepted is None
        ):
            continue
        due = accepted + timedelta(days=c.get("days_to_complete") or 0)
        if now < due <= now + COURIER_WARNING:
            hours = (due - now).total_seconds() / 3600
            text = f"{name}: a courier contract is due in {hours:.0f} hours."
            found.append(Alert(f"courier:{c['contract_id']}", "discord", text))
    return found


def _extractors(db, character_id: int, name: str, now: datetime) -> list[Alert]:
    return [
        Alert(
            f"extractor:{character_id}:{t.planet}:{t.stopped_at.isoformat()}",
            "discord",
            f"{name}: the extractor{'s' if t.stopped != 1 else ''} on {t.planet} "
            f"{'have' if t.stopped != 1 else 'has'} stopped.",
            "/characters",
        )
        for t in planets.timers(db, character_id, now)
        if t.stopped and t.stopped_at
    ]


def game_alerts(db: sqlite3.Connection, now: datetime | None = None) -> list[Alert]:
    """Everything in game that needs doing now, for every character."""
    now = now or datetime.now(UTC)
    found = []
    for character_id, name in _names(db).items():
        found += _jobs(db, character_id, name, now)
        found += _orders(db, character_id, name)
        found += _queue(db, character_id, name)
        found += _couriers(db, character_id, name, now)
        found += _extractors(db, character_id, name, now)
    return found


def health_alerts(problems: list[str], now: datetime | None = None) -> list[Alert]:
    """A background refresh's problems, each at most once an hour."""
    hour = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H")
    return [
        Alert(f"health:{hour}:{hashlib.sha256(p.encode()).hexdigest()[:16]}", "ntfy", p)
        for p in dict.fromkeys(problems)
    ]


def _sent(db: sqlite3.Connection, key: str) -> bool:
    return db.execute("SELECT 1 FROM sent_alerts WHERE key = ?", (key,)).fetchone() is not None


def _post(http: httpx.Client, channel: str, address: str, alert: Alert, dashboard: str) -> None:
    link = f"{dashboard}{alert.path}"
    if channel == "discord":
        response = http.post(address, json={"content": f"{alert.text}\n<{link}>"})
    else:
        response = http.post(address, content=alert.text.encode(),
                             headers={"Title": "EVE Assistant", "Click": link})  # fmt: skip
    response.raise_for_status()


def send(
    db: sqlite3.Connection,
    store: SecretStore,
    http: httpx.Client,
    alerts: list[Alert],
    dashboard: str = DEFAULT_DASHBOARD,
) -> tuple[int, list[str]]:
    """Send the alerts not sent before, to channels with an address. Returns
    (how many were sent, problem lines). A channel without an address sends and
    records nothing: whatever is still true when it's set up is sent then."""
    saved = addresses(store)
    count, problems = 0, []
    for alert in alerts:
        address = saved.get(alert.channel)
        if not address or _sent(db, alert.key):
            continue
        try:
            _post(http, alert.channel, address, alert, dashboard)
        except httpx.HTTPError as exc:
            problems.append(
                f"Alert to {alert.channel.capitalize()} failed: {exc.__class__.__name__}"
            )
            continue
        db.execute(
            "INSERT INTO sent_alerts (key, channel, text, sent_at) VALUES (?, ?, ?, ?)",
            (alert.key, alert.channel, alert.text, utc_now()),
        )
        db.commit()
        count += 1
    return count, problems
