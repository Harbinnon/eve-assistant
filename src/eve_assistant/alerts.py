"""Alerts (roadmap 7.1): things to do in game to Discord, the tool's own
health to ntfy, nothing else, and each only once.

Discord (a channel's webhook) gets what needs action in game, each with a
link back to the dashboard:

- an industry job has finished;
- a market order has sold (or a buy order filled): gone from your orders, with
  a matching trade in the wallet since it was placed;
- a market order has been undercut or outbid;
- a skill queue is empty;
- a courier contract you accepted is due within COURIER_WARNING;
- an extractor on a planet has stopped;
- a goal has been reached: its skills trained and its ISK in the wallet.

ntfy (a topic's address) gets the tool's health: the problems a background
refresh reports (a login that can't be refreshed, ESI errors, being asked to
slow down), each at most once an hour.

Both addresses let whoever holds them post, so they are secrets: they live in
the secret store, entered through `eve-assistant alerts-setup` (a hidden
prompt), never in config, the database or chat. On the hosted server they can
come from its root-owned env file instead (ENV_ADDRESSES, which win), with an
ntfy token for a topic that needs one. Every alert sent is kept in
sent_alerts (append-only), and its key stops it being sent twice.
"""

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from eve_assistant import goals, planets, skills, snapshots, trade
from eve_assistant.secrets_store import SecretStore
from eve_assistant.store import utc_now

COURIER_WARNING = timedelta(hours=6)
STORE_ENTRY = "alerts"
DISCORD_PREFIXES = ("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/")
DEFAULT_DASHBOARD = "http://127.0.0.1:8700"
ENV_ADDRESSES = {"discord": "EVE_ASSISTANT_DISCORD_WEBHOOK", "ntfy": "EVE_ASSISTANT_NTFY_URL"}
NTFY_AUTH_ENV = "EVE_ASSISTANT_NTFY_TOKEN"


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


def addresses(store: SecretStore | None) -> dict[str, str]:
    """The Discord webhook and ntfy topic, by channel: from the environment if
    set there, otherwise as saved in the secret store."""
    found = dict(store.load().get(STORE_ENTRY, {})) if store is not None else {}
    for channel, name in ENV_ADDRESSES.items():
        if os.environ.get(name):
            found[channel] = os.environ[name]
    return found


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


def _two_latest(db, character_id: int, kind: str) -> tuple[list, list]:
    """The latest payload of a kind and the one before it."""
    rows = db.execute(
        "SELECT payload FROM snapshots WHERE character_id = ? AND kind = ? "
        "ORDER BY snapshot_id DESC LIMIT 2",
        (character_id, kind),
    ).fetchall()
    payloads = [json.loads(r["payload"]) for r in rows]
    return (payloads + [[], []])[0], (payloads + [[], []])[1]


def _sold(db, character_id: int, name: str) -> list[Alert]:
    """Orders gone since the last check that the wallet shows trades for."""
    now_open, before = _two_latest(db, character_id, "orders")
    still = {o.get("order_id") for o in now_open if isinstance(o, dict)}
    trades = [t for t in snapshots.latest(db, character_id, "wallet_transactions") or []
              if isinstance(t, dict)]  # fmt: skip
    found = []
    for order in before:
        if not isinstance(order, dict) or order.get("order_id") in still:
            continue
        is_buy = bool(order.get("is_buy_order"))
        if any(
            t.get("type_id") == order.get("type_id")
            and t.get("location_id") == order.get("location_id")
            and bool(t.get("is_buy")) == is_buy
            and (t.get("date") or "") >= (order.get("issued") or "")
            for t in trades
        ):
            what = skills.type_name(db, order.get("type_id"))
            done = "filled" if is_buy else "sold"
            found.append(Alert(f"sold:{order['order_id']}", "discord",
                               f"{name}: your {'buy' if is_buy else 'sell'} order for {what} "
                               f"has {done}.", "/trade"))  # fmt: skip
    return found


def _goals(db) -> list[Alert]:
    """Goals whose skills are trained and whose ISK is in the wallet."""
    found = []
    for goal in goals.active_goals(db):
        try:
            status = goals.evaluate(db, goal)
        except skills.SkillsError:
            continue
        trained = status.requirement is None or status.requirement.usable_now
        paid = status.isk_needed is None or status.isk_needed <= 0
        known = status.requirement is not None or status.isk_needed is not None
        if known and trained and paid:
            found.append(Alert(f"goal:{goal.goal_id}", "discord",
                               f"Goal reached: {goal.label}.", "/goals"))  # fmt: skip
    return found


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
        found += _sold(db, character_id, name)
        found += _queue(db, character_id, name)
        found += _couriers(db, character_id, name, now)
        found += _extractors(db, character_id, name, now)
    return found + _goals(db)


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
        headers = {"Title": "EVE Assistant", "Click": link}
        if os.environ.get(NTFY_AUTH_ENV):
            headers["Authorization"] = f"Bearer {os.environ[NTFY_AUTH_ENV]}"
        response = http.post(address, content=alert.text.encode(), headers=headers)
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
