"""The dashboard: server-rendered pages over the service layer.

Local mode only for now: it listens on 127.0.0.1 and has no login, so it
refuses to run in hosted mode until hosted mode (with a login) is built.
Forms carry a per-process token and cross-site posts are rejected, so a
web page in another tab cannot change anything here.

When given an auto_refresh function, the dashboard runs it in the
background every minute while it is open (it fetches only what is due).
"""

import asyncio
import contextlib
import json
import logging
import math
import secrets
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from eve_assistant import (
    accounts,
    activities,
    backups,
    checklist,
    corporation,
    corrections,
    detection,
    explain,
    fittings,
    gamelogs,
    goals,
    history,
    images,
    losses,
    market,
    market_data,
    numbers,
    omega,
    outcomes,
    overview,
    pace,
    planets,
    planner,
    preferences,
    projects,
    risk,
    sde,
    session_flow,
    sessions,
    skills,
    sovereignty,
    store,
    together,
    trade,
    train_next,
    worth,
)
from eve_assistant import hosted as hosted_rules
from eve_assistant.esi import sso
from eve_assistant.goals import GoalError
from eve_assistant.skills import SkillsError
from eve_assistant.web import charts

# make_plan(character_id, minutes, allow_lowsec, allow_risky) -> plan_id
PlanMaker = Callable[[int, float, bool, bool], int]
# make_team_plan(character_ids, minutes, allow_lowsec, allow_risky) -> team plan ID
TeamPlanner = Callable[[list[int], float, bool, bool], int]
# Planning together takes minutes (several plans, live markets): longer than a
# reverse proxy waits for a page. So it runs in the background, and a waiting
# page checks back every few seconds.
TEAM_PLANNING_RELOAD_SECONDS = 5
MAX_TEAM_JOBS = 20  # planning jobs remembered (finished ones only matter briefly)
SnapshotTaker = Callable[[int], list[str]]
# Returns an object with .problems (list[str]) and .retry_after (seconds or None).
AutoRefresher = Callable[[], object]
AUTO_TICK_SECONDS = 60.0
INGEST_MAX_BYTES = 1_000_000
INGEST_PER_HOUR = 120
log = logging.getLogger(__name__)
TODAY = "/today"
GOALS = "/goals"
SETTINGS = "/settings"
CHARACTERS = "/characters"
# Every character, by name: the switch, History and the learning lines.
CHARACTERS_BY_NAME = "SELECT character_id, name FROM characters ORDER BY name"
CHARACTER_NAMES = "SELECT character_id, name FROM characters"
UNKNOWN_ACTION = "Unknown action."
COMMIT_WITH_THE_PLAN = (
    "Steps aren't taken on one by one: press “Start this plan” to start the whole "
    "plan, then tick steps off as you go."
)

HERE = Path(__file__).parent

# Only requests addressed to this PC by name are answered. This blocks DNS
# rebinding: a website that points its own domain at 127.0.0.1 still sends
# its domain in the Host header, and is refused.
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")

# Sent with every response. The pages use no scripts and only their own
# stylesheet, so the policy can be strict.
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'self'; img-src 'self' data:; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    # same-origin, not no-referrer: under no-referrer browsers send "Origin: null"
    # on the dashboard's own form posts, which the cross-site check rightly
    # rejects. Other sites still get no referrer.
    "Referrer-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Cache-Control": "no-store",
}
# EVE's login page, where the hosted copy's Add a character form is sent on to.
SSO_ORIGIN = "{0.scheme}://{0.netloc}".format(urlsplit(sso.AUTHORIZE_URL))


def security_headers_for(hosted) -> dict[str, str]:
    """The headers every response gets. In hosted mode the CSP's form-action
    also allows EVE's login host, and nothing else: "Add a character" is a
    form that redirects there, and browsers apply form-action to the whole
    redirect chain. Local mode opens EVE's login in the browser itself, so it
    keeps 'self' only."""
    if hosted is None:
        return SECURITY_HEADERS
    policy = SECURITY_HEADERS["Content-Security-Policy"].replace(
        "form-action 'self'", f"form-action 'self' {SSO_ORIGIN}"
    )
    return {**SECURITY_HEADERS, "Content-Security-Policy": policy}


# While a session runs these pages reload themselves (a meta refresh: no
# JavaScript), so the session view's timer and wallet stay current.
RELOAD_SECONDS = 60
RELOADING_PAGES = ("today", "characters", "history")

# Adding a character: how often Characters reloads while EVE's login page is
# open, and how long the result stays on the page.
LOGIN_RELOAD_SECONDS = 5
LOGIN_NEWS_MINUTES = 15

# At most this many plans side by side in "who should play".
MAX_COMPARED = 10

# Past this age a plan's prices and contracts have likely moved: say so.
PLAN_STALE_MINUTES = 60

# Risk ratings in words; the stored ratings stay green, amber and red.
RISK_LABELS = {"green": "Safe", "amber": "Caution", "red": "Risky"}

# Error pages: a heading per status, and words for Starlette's own errors.
ERROR_TITLES = {
    400: "That didn't work",
    403: "Not allowed",
    404: "Nothing here",
    405: "Nothing here",
    422: "Something was missing",
    500: "Something went wrong",
}
NO_PAGE = "There's no page at this address."
NO_BANNER = SimpleNamespace(needs_update=False)  # error pages skip the database

# The active character (header switch), kept in a cookie like the theme.
CHARACTER_COOKIE = "character"

# Colour themes, kept in a cookie set by the form in every page's footer.
THEMES = ("dark", "light", "system")
# The cookie gets one of these constants, never the posted text itself.
THEME_VALUES = {theme: theme for theme in THEMES}
DEFAULT_THEME = "dark"
THEME_COOKIE = "theme"
THEME_MAX_AGE = 365 * 24 * 3600
# Pages the theme form may return to; anything else goes to Today.
# Each maps to itself, so the redirect is always one of these constants.
TRADE = "/trade"
RETURN_PAGES = {
    page: page for page in (TODAY, CHARACTERS, GOALS, "/skills", "/history", SETTINGS, TRADE)
}
TRADE_MODES = ("instant", "listed", "station")
AMOUNT_UNITS = {"k": 1e3, "m": 1e6, "b": 1e9}

BAND_LABELS = {
    "none": "No player risk (Exordium)",
    "very_low": "Very low (quiet highsec)",
    "low": "Low (busy highsec, quiet lowsec)",
    "medium": "Medium (active lowsec, gank hotspots, incursions)",
    "high": "High (camped lowsec, nullsec, unknown)",
}

MESSAGES = {
    "market_refreshing": "Reading the trade hubs' markets from EVE: about a minute.",
    "goal_added": "Goal added.",
    "account_set": "Account saved.",
    "goal_done": "Goal marked done.",
    "goal_dropped": "Goal dropped.",
    "refreshed": "Character data refreshed from EVE.",
    "refresh_problems": "The refresh finished, but not everything could be read (below).",
    "sde_update_requested": "Static data update asked for: it starts within a minute and "
    "takes a minute or two.",
    "do": "Project started: it's under Open projects.",
    "not_now": "Recorded: not now.",
    "mute": "Muted. You can undo that in Settings.",
    "muted": "Muted.",
    "unmuted": "Unmuted.",
    "project_done": "Project marked done.",
    "project_dropped": "Project dropped.",
    "play_time": "Play time saved.",
    "buyback": "Buyback saved: plans compare it with the market.",
    "buyback_cleared": "Buyback removed.",
    "bridges": "Jump bridges saved.",
    "session_started": "Session started. Press “I’m done” when you stop.",
    "session_finished": "Session recorded.",
    "session_cancelled": "Session cancelled.",
    "detected_recorded": "Session recorded. Its outcome is worked out from EVE's data.",
    "detected_dismissed": "Got it, that one won't be offered again.",
}


def _isk(value) -> str:
    return numbers.isk(value)


def _share(value: float) -> str:
    """0.02 -> '2%', 0.0283 -> '2.83%'."""
    return f"{value * 100:.3g}%"


def _when(value) -> str:
    """ISO UTC timestamp -> '2026-10-16 02:50 UTC'."""
    if not value:
        return "never"
    return f"{value[:10]} {value[11:16]} UTC"


def _losses(conn) -> list[SimpleNamespace]:
    """Your losses for History: who, where, what, its value, and the session's band."""
    names = dict(conn.execute(CHARACTER_NAMES).fetchall())
    _trips, attributed = risk.trips_and_losses(conn)
    rows = []
    for loss in losses.all_losses(conn):
        system = conn.execute(
            "SELECT name, security_status FROM sde_solar_systems WHERE system_id = ?",
            (loss.system_id,),
        ).fetchone()
        rows.append(
            SimpleNamespace(
                at=loss.killed_at,
                character=names.get(loss.character_id, str(loss.character_id)),
                system=system["name"] if system else str(loss.system_id),
                security=system["security_status"] if system else None,
                ship=skills.type_name(conn, loss.ship_type_id),
                value=losses.value(conn, loss),
                band=attributed.get(loss.killmail_id),
            )
        )
    return rows


def _corps_and_planets(conn, character_ids) -> tuple[dict, dict]:
    """Each character's corporation (with its tax) and its colonies' timers."""
    corps, timers = {}, {}
    for cid in character_ids:
        corp = corporation.of(conn, cid)
        if corp is not None:
            corps[cid] = SimpleNamespace(corp=corp, tax=corporation.tax(conn, cid))
        timers[cid] = planets.timers(conn, cid, _now())
    return corps, timers


def _amount(text: str, default: float) -> float:
    """ "929m", "1.5b", "40,000" or empty (the default) as a number."""
    cleaned = text.replace(",", "").replace("_", "").strip().lower()
    if not cleaned:
        return default
    scale = AMOUNT_UNITS.get(cleaned[-1])
    number = cleaned[:-1] if scale is not None else cleaned
    try:
        return float(number) * (scale or 1.0)
    except ValueError:
        return default


def _worth(conn, character_ids) -> SimpleNamespace | None:
    """Today's net worth and the line to it, for the Characters and Goals pages."""
    points = worth.history(conn, character_ids)
    if not points:
        return None
    return SimpleNamespace(
        now=points[-1], line=charts.line([(p.at, p.total) for p in points]), days=worth.DAYS
    )


def _now() -> datetime:
    return datetime.now(UTC)


def _minutes_from_now(value) -> float | None:
    """Minutes since a time (negative for a time still to come)."""
    if not value:
        return None
    then = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    return (_now() - then).total_seconds() / 60


def _minutes_since(value) -> float | None:
    minutes = _minutes_from_now(value)
    return None if minutes is None else max(0.0, minutes)


def _span(minutes: float) -> str:
    if minutes < 60:
        return f"{minutes:.0f} min"
    hours = minutes / 60
    if hours < 48:
        return f"{hours:.0f} hour{'s' if round(hours) != 1 else ''}"
    return f"{hours / 24:.0f} days"


def _when_ago(value) -> str:
    """'2026-10-16 02:50 UTC · 6 hours ago', or '· in 2 days' for times to come."""
    minutes = _minutes_from_now(value)
    if minutes is None:
        return "never"
    if abs(minutes) < 1:
        relative = "just now"
    elif minutes > 0:
        relative = f"{_span(minutes)} ago"
    else:
        relative = f"in {_span(-minutes)}"
    return f"{_when(str(value))} · {relative}"


def _ago(value) -> str:
    """ISO UTC timestamp -> '12 min ago', '6 hours ago', '3 days ago'."""
    minutes = _minutes_since(value)
    if minutes is None:
        return "never"
    if minutes < 1:
        return "just now"
    return f"{_span(minutes)} ago"


def _theme(request: Request) -> str:
    theme = request.cookies.get(THEME_COOKIE, DEFAULT_THEME)
    return theme if theme in THEMES else DEFAULT_THEME


def _safe_back(path: str) -> str:
    """A path on this dashboard for an error page's "Back" link, else Today.
    Never another site ("//host", "/\\host") or a header break."""
    if (
        not path.startswith("/")
        or path.startswith("//")
        or "\\" in path
        or any(ord(ch) < 32 for ch in path)
    ):
        return TODAY
    return path


def _security_tenths(value: float) -> int:
    """Security as EVE shows it, in tenths: the nearest tenth, halves up,
    except that anything above 0.0 and below 0.05 shows as 0.1."""
    if 0 < value < 0.05:
        return 1
    return math.floor(value * 10 + 0.5)


def _security(value) -> str:
    """A system's security as EVE shows it, e.g. '0.5'."""
    return "?" if value is None else f"{_security_tenths(value) / 10:.1f}"


def _security_class(value) -> str:
    """CSS classes for a system's security: 'sec' plus its tenth, 'sec-10'
    (1.0) down to 'sec-00' (0.0 and below), for EVE's colour for it."""
    if value is None:
        return "sec"
    return f"sec sec-{max(0, min(10, _security_tenths(value))):02d}"


@dataclass(frozen=True)
class AccountStatus:
    """What the Characters and Settings pages say about logins and features."""

    missing_scopes: Callable[[], dict[int, tuple[str, ...]]] | None = None
    planets_on: bool = False
    alert_channels: Callable[[], set[str]] | None = None


@dataclass(frozen=True)
class Hosting:
    """What a hosted copy adds (roadmap 7.2)."""

    rules: Any  # config.Hosted: its addresses, hostname and owner
    login: Any  # .start() -> auth.PendingLogin, .finish(pending, code) -> name
    ingest_token: str | None = None  # the log sender's token; None: no /ingest/


def create_app(
    db_path: Path | str = store.DEFAULT_DB_PATH,
    refresh: Callable[[], list[str]] | None = None,
    make_plan: PlanMaker | None = None,
    snapshot: SnapshotTaker | None = None,
    make_team_plan: TeamPlanner | None = None,
    allowed_hosts: tuple[str, ...] = LOCAL_HOSTS,
    auto_refresh: AutoRefresher | None = None,
    backups_dir: Path | str | None = None,
    add_character: Callable[[], str] | None = None,
    fetch_image: images.Fetch | None = None,
    scan_markets: Callable[..., list[str]] | None = None,
    account: "AccountStatus | None" = None,
    hosting: "Hosting | None" = None,
) -> FastAPI:
    """refresh: called by the Refresh button; returns problem lines, empty when all went well.
    make_plan: builds and records a plan (needs ESI), returning its ID.
    make_team_plan: the same for characters played together, returning the team plan's ID.
    snapshot: snapshots one character (needs ESI), returning problem lines.
    auto_refresh: one background refresh tick (needs ESI), run every AUTO_TICK_SECONDS."""
    account = account or AccountStatus()
    missing_scopes, planets_on = account.missing_scopes, account.planets_on
    alert_channels = account.alert_channels
    hosted = hosting.rules if hosting else None
    web_login = hosting.login if hosting else None
    ingest_token = hosting.ingest_token if hosting else None
    auto_status: dict = {"enabled": auto_refresh is not None, "last_run": None, "problems": []}
    # Adding a character runs EVE's login in the background (it waits for the
    # browser); one at a time.
    login: dict = {"state": None, "message": None, "at": None}
    login_running = threading.Lock()
    last_refresh: dict = {"problems": []}  # the Refresh button's last problems, for Characters
    pending_logins: dict = {}  # hosted: logins sent to EVE, by state
    ingest_times: deque = deque()  # when each accepted /ingest/ request came
    # The trade hubs' markets are read in the background (about a minute), when the
    # Trade page or a plan wants them and they're old; one scan at a time.
    market_status: dict = {"running": False, "problems": []}
    team_jobs: dict[str, dict] = {}  # planning together in the background, by job ID

    def start_team_plan(ids: list[int], names: list[str], minutes: float, lowsec, risky) -> str:
        job = secrets.token_urlsafe(9)
        team_jobs[job] = {"state": "running", "started": time.monotonic(), "names": names,
                          "minutes": minutes}  # fmt: skip
        for old in list(team_jobs)[:-MAX_TEAM_JOBS]:
            team_jobs.pop(old, None)

        def run() -> None:
            try:
                team_id = make_team_plan(ids, minutes, lowsec, risky)
            except together.TogetherError as exc:
                team_jobs[job].update(state="failed", error=_described(exc.reasons))
            except Exception as exc:  # noqa: BLE001  # shown on the waiting page, never lost
                log.warning("Planning together failed: %s", exc)
                team_jobs[job].update(state="failed", error=f"Could not build a plan: {exc}")
            else:
                team_jobs[job].update(state="done", team_id=team_id)

        threading.Thread(target=run, daemon=True).start()
        return job

    def start_market_scan(force: bool = False) -> None:
        if scan_markets is None or market_status["running"]:
            return
        market_status["running"] = True

        def run() -> None:
            try:
                market_status["problems"] = scan_markets(force=force)
            except Exception as exc:  # noqa: BLE001  # shown on the Trade page
                log.warning("Market scan failed: %s", exc)
                market_status["problems"] = [f"Market scan failed: {exc}"]
            finally:
                market_status["running"] = False

        threading.Thread(target=run, daemon=True).start()

    async def auto_loop() -> None:
        while True:
            delay = AUTO_TICK_SECONDS  # read each time, so tests can make it short
            try:
                report = await asyncio.to_thread(auto_refresh)
                auto_status["problems"] = list(report.problems)
                if report.retry_after:
                    delay = max(delay, report.retry_after)
            except Exception as exc:  # noqa: BLE001  # keep the loop alive; shown on every page
                log.warning("Background refresh failed: %s", exc)
                auto_status["problems"] = [f"Background refresh failed: {exc}"]
            auto_status["last_run"] = store.utc_now()
            await asyncio.sleep(delay)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        task = asyncio.create_task(auto_loop()) if auto_refresh is not None else None
        yield
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = FastAPI(
        title="EVE Assistant", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    images_dir = Path(db_path).parent / "images"
    hosts = [*allowed_hosts, *((hosted.hostname, hosted.ingest_host) if hosted else ())]
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)

    headers_here = security_headers_for(hosted)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for name, value in headers_here.items():
            response.headers.setdefault(name, value)
        return response

    if hosted is not None:
        # Added last, so it runs first: before the host check or anything costly.
        @app.middleware("http")
        async def hosted_guard(request: Request, call_next):
            """Hosted mode (roadmap 7.2): the dashboard only through the proxy, with
            the owner's sign-in identity; /ingest/ only straight from the home
            network. A refusal is a quick 403, never a dropped connection, so a
            monitor can tell the app is up and enforcing its rule."""
            client = request.client.host if request.client else None
            path = request.url.path
            if not hosted_rules.allowed(path, client, hosted.proxy, hosted.home_network):
                return PlainTextResponse("Forbidden", status_code=403)
            if not hosted_rules.is_ingest(path):
                identity = request.headers.get("x-auth-id") or hosted.dev_identity
                if identity != hosted.owner_discord_id:
                    return PlainTextResponse("Forbidden", status_code=403)
            return await call_next(request)

    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["isk"] = _isk
    templates.env.filters["duration"] = skills.format_minutes
    templates.env.filters["security_class"] = _security_class
    templates.env.filters["security"] = _security
    templates.env.filters["when"] = _when
    templates.env.filters["ago"] = _ago
    templates.env.filters["when_ago"] = _when_ago
    templates.env.filters["share"] = _share
    templates.env.globals["minutes_since"] = _minutes_since
    templates.env.globals["plan_stale_minutes"] = PLAN_STALE_MINUTES
    templates.env.globals["describe"] = skills.describe
    templates.env.globals["explain"] = explain.describe
    templates.env.globals["risk_summary"] = explain.risk_summary
    templates.env.globals["why_not"] = explain.WHY_NOT
    templates.env.globals["why_not_here"] = planner.why_not
    templates.env.globals["per_hour"] = numbers.per_hour
    templates.env.globals["plan_value"] = planner.plan_value
    templates.env.globals["risk_labels"] = RISK_LABELS
    templates.env.globals["days_to_sell"] = trade.days_to_sell
    templates.env.globals["category_labels"] = preferences.KNOWN
    form_token = secrets.token_urlsafe(32)

    def db() -> sqlite3.Connection:
        return store.connect(db_path)

    def active_character(request: Request, conn) -> tuple[int, str] | None:
        """The header switch's character: the cookie's if it names one of
        ours, else whoever is online, else the first; None without any."""
        names = dict(conn.execute(CHARACTER_NAMES).fetchall())
        chosen = request.cookies.get(CHARACTER_COOKIE, "")
        character_id = int(chosen) if chosen.isdigit() and int(chosen) in names else None
        if character_id is None:
            character_id = overview.default_character(conn)
        return (character_id, names[character_id]) if character_id is not None else None

    def render(request: Request, name: str, **context) -> HTMLResponse:
        message = MESSAGES.get(request.query_params.get("msg", ""))
        conn = db()
        try:
            static_data = sde.update_status(conn)
            playing = bool(sessions.open_sessions(conn))
            everyone = conn.execute(CHARACTERS_BY_NAME)
            everyone = [(r["character_id"], r["name"]) for r in everyone]
            active = active_character(request, conn)
        finally:
            conn.close()
        page = name.split(".")[0]
        return templates.TemplateResponse(
            request,
            name,
            {
                "form_token": form_token,
                "message": message,
                "auto": auto_status,
                "static_data": static_data,
                "page": page,
                "reload": RELOAD_SECONDS if playing and page in RELOADING_PAGES else None,
                "theme": _theme(request),
                "here": request.url.path,  # where the theme form returns to
                "switch_characters": everyone,
                "active": {"id": active[0], "name": active[1]} if active else None,
                **context,
            },  # fmt: skip
        )

    def render_error(request: Request, status: int, detail: str) -> HTMLResponse:
        """A page for any error, with a way back. It doesn't touch the
        database, so it still works when that is the problem."""
        referer = urlsplit(request.headers.get("referer", ""))
        back_to = None
        if referer.netloc == request.url.netloc and referer.path != request.url.path:
            back_to = _safe_back(referer.path + (f"?{referer.query}" if referer.query else ""))
        response = templates.TemplateResponse(
            request,
            "error.html",
            {
                "form_token": form_token,
                "message": None,
                "auto": auto_status,
                "static_data": NO_BANNER,
                "page": "error",
                "theme": _theme(request),
                "here": TODAY,
                "status": status,
                "title": ERROR_TITLES.get(status, "That didn't work"),
                "detail": detail,
                "back_to": back_to,
            },  # fmt: skip
            status_code=status,
        )
        response.headers.update(SECURITY_HEADERS)  # a 500 skips the header middleware
        return response

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        if exc.status_code in (404, 405) and exc.detail in ("Not Found", "Method Not Allowed"):
            return render_error(request, exc.status_code, NO_PAGE)
        return render_error(request, exc.status_code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def form_error(request: Request, _exc: RequestValidationError):
        return render_error(
            request,
            422,
            "Something the form needs was missing or not a number. Go back and try again.",
        )

    @app.exception_handler(Exception)
    async def server_error(request: Request, _exc: Exception):
        # The server still logs the traceback in its window: Starlette re-raises after this.
        return render_error(
            request,
            500,
            "Something went wrong on the dashboard's side; the details are in its window. "
            "Going back and trying again may work.",
        )

    def check_form(request: Request, token: str) -> None:
        origin = request.headers.get("origin")
        # Behind the hosted copy's proxy the browser is on https://<hostname>
        # while the app itself is spoken to over plain http.
        expected = hosted.public_origin if hosted else str(request.base_url).rstrip("/")
        if origin and origin.rstrip("/") != expected:
            raise HTTPException(
                403,
                "This form was sent from another website, so it was refused. If you pressed a "
                "button on this dashboard, reload the page and try again.",
            )
        if not secrets.compare_digest(token, form_token):
            raise HTTPException(403, "This form has expired. Reload the page and try again.")

    def back(path: str, anchor: str = "", **params) -> RedirectResponse:
        query = f"?{urlencode(params)}" if params else ""
        return RedirectResponse(f"{path}{query}{'#' + anchor if anchor else ''}", status_code=303)

    @app.get("/")
    def home():
        return back(TODAY)

    @app.post("/character")
    def choose_character(
        request: Request,
        token: str = Form(...),
        character_id: str = Form(...),
        return_to: str = Form(""),
    ):
        check_form(request, token)
        conn = db()
        try:
            # Each known ID maps to itself, so the cookie never holds posted text.
            known = {
                str(r[0]): str(r[0]) for r in conn.execute("SELECT character_id FROM characters")
            }
        finally:
            conn.close()
        chosen = known.get(character_id)
        if chosen is None:
            raise HTTPException(400, "That character isn't logged in here.")
        response = RedirectResponse(RETURN_PAGES.get(return_to, TODAY), status_code=303)
        response.set_cookie(
            CHARACTER_COOKIE,
            chosen,
            max_age=THEME_MAX_AGE,
            path="/",
            secure=True,
            httponly=True,
            samesite="strict",
        )
        return response

    @app.post("/theme")
    def choose_theme(
        request: Request, token: str = Form(...), theme: str = Form(...), return_to: str = Form("")
    ):
        check_form(request, token)
        chosen = THEME_VALUES.get(theme)
        if chosen is None:
            raise HTTPException(400, "Unknown theme.")
        # Back to one of the dashboard's own pages, never an address from the form.
        target = RETURN_PAGES.get(return_to, TODAY)
        response = RedirectResponse(target, status_code=303)
        # Secure is fine over plain http here: browsers treat 127.0.0.1 and localhost
        # as secure, and hosted mode will use HTTPS.
        response.set_cookie(
            THEME_COOKIE,
            chosen,
            max_age=THEME_MAX_AGE,
            path="/",
            secure=True,
            httponly=True,
            samesite="strict",
        )
        return response

    def _names(conn) -> list[str]:
        return [r["name"] for r in conn.execute("SELECT name FROM characters ORDER BY name")]

    @app.get(TODAY, response_class=HTMLResponse)
    def today_page(request: Request, error: str | None = None, compare: str = ""):
        conn = db()
        try:
            names = _names(conn)
            compared = []
            for text in compare.split(",")[:MAX_COMPARED]:
                if text.isdigit():
                    try:
                        compared.append(planner.load_plan(conn, int(text)))
                    except planner.PlanError:
                        continue
            compared = planner.who_should_play(compared)
            last_team_id = together.latest_team_id(conn)
            active = active_character(request, conn)
            latest = planner.latest_plan_id(conn, active[0] if active else None)
            recent = planner.load_plan(conn, latest) if latest else None
            ship_type_id = (
                overview.summarize(conn, recent.character_id).ship_type_id if recent else None
            )
            open_projects = _open_projects(conn)
            playing = sessions.open_sessions(conn)
            session_views = checklist.views(conn)
            results = outcomes.recent(conn, 5)
            noticed = detection.suggestions(conn)
            outbid = sum(w.outbid for w in trade.watch_orders(conn, active[0])) if active else 0
            stopped = [
                (name, timer)
                for cid, name in conn.execute("SELECT character_id, name FROM characters")
                for timer in planets.timers(conn, cid, _now())
                if timer.stopped
            ]
        finally:
            conn.close()
        return render(
            request,
            "today.html",
            character_names=names,
            plan=recent,
            results=results,
            error=error,
            can_plan=make_plan is not None,
            open_projects=open_projects,
            open_sessions=playing,
            session_views=session_views,  # while playing, Today is the session view
            noticed=noticed,
            ship_type_id=ship_type_id,
            compared=compared,
            outbid=outbid,
            stopped_planets=stopped,
            can_team_plan=make_team_plan is not None,
            last_team_id=last_team_id,
        )

    @app.post("/sessions/{session_id}/steps/{item_id}/{action}")
    def tick_step(
        request: Request, session_id: int, item_id: int, action: str, token: str = Form(...)
    ):
        check_form(request, token)
        if action not in ("done", "undo"):
            raise HTTPException(404, UNKNOWN_ACTION)
        conn = db()
        try:
            session = checklist.mark(conn, session_id, item_id, done=action == "done")
        except sessions.SessionNotFound as exc:
            raise HTTPException(404, str(exc)) from None
        except (checklist.ChecklistError, planner.PlanError, projects.ProjectError) as exc:
            raise HTTPException(400, str(exc)) from None
        finally:
            conn.close()
        # The anchor uses the session's ID as stored, not the one in the address.
        return back(TODAY, anchor=f"session-{session.session_id}")

    def _open_projects(conn) -> list[tuple[str, projects.Project]]:
        names = dict(conn.execute(CHARACTER_NAMES).fetchall())
        return [(names.get(p.character_id, "?"), p) for p in projects.open_projects(conn)]

    @app.post("/projects/{project_id}/{status}")
    def close_project(request: Request, project_id: int, status: str, token: str = Form(...)):
        check_form(request, token)
        if status not in ("done", "dropped"):
            raise HTTPException(404, "Unknown status.")
        conn = db()
        try:
            projects.close(conn, project_id, status)
        except projects.ProjectError as exc:
            raise HTTPException(404, str(exc)) from None
        finally:
            conn.close()
        return back(TODAY, msg=f"project_{status}")

    @app.post("/today/plan")
    def new_plan(
        request: Request,
        token: str = Form(...),
        character: str = Form(...),
        minutes: float = Form(...),
        allow_lowsec: bool = Form(False),
        allow_risky: bool = Form(False),
        compare: bool = Form(False),
    ):
        check_form(request, token)
        start_market_scan()  # for the plans after this one, if the markets are old
        if make_plan is None:
            return today_page(request, error="Planning is not available here.")
        conn = db()
        try:
            character_id = goals.resolve_character(conn, character)
            everyone = [r["character_id"] for r in conn.execute(CHARACTERS_BY_NAME)]
        except GoalError as exc:
            return today_page(request, error=str(exc))
        finally:
            conn.close()
        try:
            if compare:  # the same plan for every character, to pick who plays
                made = [make_plan(cid, minutes, allow_lowsec, allow_risky) for cid in everyone]
                return back(TODAY, compare=",".join(str(int(plan_id)) for plan_id in made))
            plan_id = make_plan(character_id, minutes, allow_lowsec, allow_risky)
        except Exception as exc:  # noqa: BLE001  # shown to the person, never swallowed silently
            return today_page(request, error=f"Could not build a plan: {exc}")
        return back(f"/plans/{plan_id}")

    @app.get("/plans/{plan_id}", response_class=HTMLResponse)
    def plan_page(request: Request, plan_id: int):
        conn = db()
        try:
            try:
                stored = planner.load_plan(conn, plan_id)
            except planner.PlanError as exc:
                raise HTTPException(404, str(exc)) from None
            names = _names(conn)
            open_projects = _open_projects(conn)
            playing = sessions.open_sessions(conn)
            results = outcomes.recent(conn, 5)
            noticed = detection.suggestions(conn)
            ship_type_id = overview.summarize(conn, stored.character_id).ship_type_id
            team_id = together.team_of(conn, plan_id)
        finally:
            conn.close()
        return render(
            request,
            "today.html",
            character_names=names,
            plan=stored,
            results=results,
            error=None,
            can_plan=make_plan is not None,
            open_projects=open_projects,
            open_sessions=playing,
            session_views=[],  # a plan you opened is shown even while playing
            noticed=noticed,
            ship_type_id=ship_type_id,
            can_team_plan=make_team_plan is not None,
            team_id=team_id,
        )

    @app.post("/static-data/update")
    def update_static_data(request: Request, token: str = Form(...)):
        """The banner's Update now: the background refresh does it on its next tick."""
        check_form(request, token)
        if auto_refresh is None:
            raise HTTPException(404, NO_PAGE)  # no background refresh to do it
        conn = db()
        try:
            sde.request_update(conn)
        finally:
            conn.close()
        return back(TODAY, msg="sde_update_requested")

    @app.post("/today/together")
    def plan_together(
        request: Request,
        token: str = Form(...),
        minutes: float = Form(...),
        allow_lowsec: bool = Form(False),
        allow_risky: bool = Form(False),
        characters: Annotated[list[str] | None, Form()] = None,
    ):
        check_form(request, token)
        if make_team_plan is None:
            return today_page(request, error="Planning is not available here.")
        conn = db()
        try:
            ids = [goals.resolve_character(conn, name) for name in characters or []]
            problems = together.check(together.members(conn, ids))
        except GoalError as exc:
            return today_page(request, error=str(exc))
        finally:
            conn.close()
        if problems:  # checked here too, so a refusal doesn't wait for market reads
            return today_page(request, error=_described(problems))
        start_market_scan()
        job = start_team_plan(ids, characters or [], minutes, allow_lowsec, allow_risky)
        return back(f"/teams/planning/{job}")

    def _described(reasons) -> str:
        return " ".join(explain.describe(r.code, r.detail) for r in reasons)

    @app.get("/teams/planning/{job}", response_class=HTMLResponse)
    def team_planning(request: Request, job: str):
        found = team_jobs.get(job)
        if found is None:
            raise HTTPException(404, "No such planning (the dashboard may have restarted).")
        if found["state"] == "done":
            return back(f"/teams/{found['team_id']}")
        if found["state"] == "failed":
            return today_page(request, error=found["error"])
        return render(
            request,
            "team_planning.html",
            names=found["names"],
            minutes=found["minutes"],
            seconds=int(time.monotonic() - found["started"]),
            reload=TEAM_PLANNING_RELOAD_SECONDS,
        )

    @app.get("/teams/{team_id}", response_class=HTMLResponse)
    def team_page(request: Request, team_id: int):
        conn = db()
        try:
            try:
                team = together.load(conn, team_id)
            except planner.PlanError as exc:
                raise HTTPException(404, str(exc)) from None
            busy = {s.character_id for s in sessions.open_sessions(conn)}
            names = {r["character_id"]: r["name"] for r in conn.execute(CHARACTERS_BY_NAME)}
        finally:
            conn.close()
        return render(
            request,
            "team.html",
            team=team,
            busy=busy,
            names_by_id=names,
            shape_names=explain.SHAPE_NAMES,
            role_names=explain.ROLE_NAMES,
        )

    def _after_session(request: Request, result: session_flow.Result, msg: str):
        if result.problems:
            done = MESSAGES[msg]
            return today_page(
                request, error=f"{done} Snapshot problems: " + "; ".join(result.problems)
            )
        return back(TODAY, msg=msg)

    @app.post("/sessions/start")
    def start_session(request: Request, token: str = Form(...), plan_id: int = Form(...)):
        check_form(request, token)
        try:
            result = session_flow.begin(db_path, snapshot, plan_id=plan_id)
        except sessions.SessionNotFound as exc:
            raise HTTPException(404, str(exc)) from None
        except sessions.SessionError as exc:
            return today_page(request, error=str(exc))
        conn = db()
        try:
            team_id = together.team_of(conn, plan_id)
        finally:
            conn.close()
        if team_id is not None and not result.problems:  # back to start the others
            return back(f"/teams/{team_id}", msg="session_started")
        return _after_session(request, result, "session_started")

    @app.post("/sessions/{session_id}/{how}")
    def end_session(
        request: Request,
        session_id: int,
        how: str,
        token: str = Form(...),
        minutes: str = Form(""),
    ):
        check_form(request, token)
        if how not in ("finish", "cancel"):
            raise HTTPException(404, UNKNOWN_ACTION)
        try:
            played = float(minutes) if minutes.strip() else None
        except ValueError:
            return today_page(request, error="Minutes played must be a number.")
        ending = "finished" if how == "finish" else "cancelled"
        try:
            result = session_flow.end(
                db_path, snapshot, session_id, how=ending, minutes_played=played
            )
        except sessions.SessionNotFound as exc:
            raise HTTPException(404, str(exc)) from None
        except sessions.SessionError as exc:
            return today_page(request, error=str(exc))
        return _after_session(request, result, f"session_{result.session.how}")

    @app.post("/detected/{action}")
    def detected_session(
        request: Request,
        action: str,
        token: str = Form(...),
        character_id: int = Form(...),
        login: str = Form(...),
    ):
        check_form(request, token)
        if action not in ("record", "dismiss"):
            raise HTTPException(404, UNKNOWN_ACTION)
        try:
            if action == "record":
                session_flow.record_detected(db_path, character_id, login)
            else:
                session_flow.dismiss_detected(db_path, character_id, login)
        except sessions.SessionNotFound as exc:
            return today_page(request, error=str(exc))
        except sessions.SessionError as exc:
            return today_page(request, error=str(exc))
        return back(TODAY, msg="detected_recorded" if action == "record" else "detected_dismissed")

    @app.post("/plan-items/{item_id}/respond")
    def respond(request: Request, item_id: int, token: str = Form(...), response: str = Form(...)):
        check_form(request, token)
        conn = db()
        try:
            row = conn.execute(
                "SELECT plan_id, kind FROM plan_items WHERE item_id = ?", (item_id,)
            ).fetchone()
            if row is None:
                raise HTTPException(404, "No such suggestion.")
            if response == "do" and row["kind"] != "project_start":
                raise HTTPException(400, COMMIT_WITH_THE_PLAN)
            try:
                planner.respond(conn, item_id, response)
            except planner.PlanError as exc:
                raise HTTPException(400, str(exc)) from None
            following = [s for s in sessions.open_sessions(conn) if s.plan_id == row["plan_id"]]
        finally:
            conn.close()
        if following:  # answered from the session view: go back to it
            return back(TODAY, anchor=f"session-{following[0].session_id}", msg=response)
        # Back to the step that was answered, not the top of the page.
        return back(f"/plans/{row['plan_id']}", anchor=f"item-{item_id}", msg=response)

    @app.get("/preferences")
    def old_preferences_page():
        return RedirectResponse(SETTINGS, status_code=308)  # renamed: old links still work

    @app.get(SETTINGS, response_class=HTMLResponse)
    def settings_page(request: Request):
        conn = db()
        try:
            active = preferences.active_mutes(conn)
            weekly = projects.weekly_minutes(conn)
            play_time = pace.weekly_play(conn)
            folder = Path(backups_dir) if backups_dir else backups.default_folder(db_path)
            kept = backups.list_backups(folder)
            buyback = corporation.buyback(conn)
            bridges = sovereignty.bridges_text(conn)
        finally:
            conn.close()
        return render(
            request,
            "settings.html",
            active=active,
            categories=preferences.KNOWN,
            weekly_hours=weekly / 60,
            play_time=play_time,
            backups_folder=folder,
            backups_kept=kept,
            buyback=buyback,
            bridges=bridges,
            bridge_count=len(bridges.splitlines()),
            planets_on=planets_on,
            alert_channels=alert_channels() if alert_channels else set(),
        )

    @app.post("/settings/buyback")
    def change_buyback(
        request: Request,
        token: str = Form(...),
        percent: str = Form(""),
        system: str = Form(""),
    ):
        check_form(request, token)
        conn = db()
        try:
            if not percent.strip() and not system.strip():
                corporation.clear_buyback(conn)
                return back(SETTINGS, anchor="buyback", msg="buyback_cleared")
            corporation.set_buyback(conn, _amount(percent, 0.0), system)
        except corporation.BuybackError as exc:
            raise HTTPException(400, str(exc)) from None
        finally:
            conn.close()
        return back(SETTINGS, anchor="buyback", msg="buyback")

    @app.post("/settings/bridges")
    def change_bridges(request: Request, token: str = Form(...), bridges: str = Form("")):
        check_form(request, token)
        conn = db()
        try:
            sovereignty.save_bridges(conn, bridges)
        except sovereignty.BridgeError as exc:
            raise HTTPException(400, str(exc)) from None
        finally:
            conn.close()
        return back(SETTINGS, anchor="bridges", msg="bridges")

    @app.post("/settings/play-time")
    def change_play_time(request: Request, token: str = Form(...), hours: float = Form(...)):
        check_form(request, token)
        conn = db()
        try:
            projects.set_weekly_minutes(conn, hours * 60)
        except projects.ProjectError as exc:
            raise HTTPException(400, str(exc)) from None
        finally:
            conn.close()
        return back(SETTINGS, msg="play_time")

    @app.post("/settings/{action}")
    def change_preference(
        request: Request, action: str, token: str = Form(...), category: str = Form(...)
    ):
        check_form(request, token)
        if action not in ("mute", "unmute"):
            raise HTTPException(404, UNKNOWN_ACTION)
        conn = db()
        try:
            if action == "mute":
                preferences.mute(conn, category)
            else:
                preferences.unmute(conn, category)
        except preferences.PreferenceError as exc:
            raise HTTPException(400, str(exc)) from None
        finally:
            conn.close()
        return back(SETTINGS, msg=f"{action}d")

    @app.get("/history", response_class=HTMLResponse)
    def history_page(request: Request):
        conn = db()
        try:
            outcomes.update(conn)  # pick up anything that settled since
            past = history.sessions_history(conn)
            speeds = [
                (r["name"], corrections.mining_observations(conn, r["character_id"]))
                for r in conn.execute(CHARACTERS_BY_NAME)
            ]
            # Characters with the same text share one line ("not yet checked" thrice is noise).
            learned_by_text: dict[str, list[str]] = {}
            for r in conn.execute(CHARACTERS_BY_NAME):
                for correction in activities.learned(conn, r["character_id"]):
                    reason = correction.reason()
                    text = explain.describe(reason.code, reason.detail)
                    learned_by_text.setdefault(text, []).append(r["name"])
            learned = [(", ".join(names), text) for text, names in learned_by_text.items()]
            finish = pace.finish_rate(conn)
            risk_model = risk.model(conn)
            own_losses = _losses(conn)
        finally:
            conn.close()
        return render(
            request,
            "history.html",
            past=past,
            speeds=[(name, found) for name, found in speeds if found],
            learned=learned,
            finish=finish,
            risk_matrix=risk.MATRIX,
            band_labels=BAND_LABELS,
            risk_model=risk_model,
            own_losses=own_losses,
        )

    @app.get(CHARACTERS, response_class=HTMLResponse)
    def characters_page(request: Request, msg: str = ""):
        # After a refresh with problems: shown here (post/redirect/get), so a
        # reload doesn't send the refresh again and the page is always whole.
        problems = list(last_refresh["problems"]) if msg == "refresh_problems" else []
        conn = db()
        try:
            summaries = overview.all_characters(conn)
            account_names = accounts.labels(conn)
            clashes = accounts.clashes(conn)
            worth_lines = {c.character_id: _worth(conn, [c.character_id]) for c in summaries}
            corps, timers = _corps_and_planets(conn, [c.character_id for c in summaries])
        finally:
            conn.close()
        recent = login["at"] and (_minutes_since(login["at"]) or 0) < LOGIN_NEWS_MINUTES
        missing = {
            cid: scopes
            for cid, scopes in (missing_scopes() if missing_scopes else {}).items()
            if scopes
        }
        return render(
            request,
            "characters.html",
            characters=summaries,
            account_names=account_names,
            clashes=clashes,
            clone_states=accounts.CLONE_STATES,
            max_label=accounts.MAX_LABEL,
            worth_lines=worth_lines,
            problems=problems,
            missing=missing,
            corps=corps,
            timers=timers,
            planets_on=planets_on,
            login=login if login["state"] == "waiting" or recent else None,
            can_add=add_character is not None or web_login is not None,  # local or hosted
            # While EVE's login page is open, check back every few seconds.
            **({"reload": LOGIN_RELOAD_SECONDS} if login["state"] == "waiting" else {}),
        )

    @app.get("/images/{kind}/{item_id}.png")
    def image(kind: str, item_id: int):
        """A portrait (own characters only) or ship render, kept in var/images."""
        if kind not in images.KINDS:
            raise HTTPException(404, NO_PAGE)
        if kind == "portrait":
            conn = db()
            try:
                ours = conn.execute(
                    "SELECT 1 FROM characters WHERE character_id = ?", (item_id,)
                ).fetchone()
            finally:
                conn.close()
            if ours is None:
                raise HTTPException(404, NO_PAGE)
        path = images.cached(images_dir, kind, item_id, fetch_image)
        if path is None:
            return Response(
                images.PLACEHOLDER_SVG,
                media_type="image/svg+xml",
                headers={"Cache-Control": "private, max-age=3600"},
            )
        return FileResponse(
            path, media_type="image/png", headers={"Cache-Control": "private, max-age=86400"}
        )

    @app.post("/characters/{character_id}/account")
    def set_account(
        request: Request,
        character_id: int,
        token: str = Form(...),
        label: str = Form(...),
        state: str = Form(...),
    ):
        check_form(request, token)
        conn = db()
        try:
            accounts.set_up(conn, character_id, label, state)
        except accounts.AccountError as exc:
            raise HTTPException(400, str(exc)) from None  # the form itself prevents these
        finally:
            conn.close()
        return back(CHARACTERS, msg="account_set")

    @app.post("/characters/add")
    def add_character_now(request: Request, token: str = Form(...)):
        check_form(request, token)
        if web_login is not None:  # hosted: EVE sends the browser back to /callback
            for state in [s for s, p in pending_logins.items() if time.time() - p.started > 600]:
                pending_logins.pop(state, None)
            pending = web_login.start()
            pending_logins[pending.state] = pending
            return RedirectResponse(pending.url, status_code=303)
        if add_character is None:
            raise HTTPException(
                400, "Adding characters isn't available here; run eve-assistant login instead."
            )
        if not login_running.acquire(blocking=False):
            return back(CHARACTERS)  # already waiting for one

        def run() -> None:
            try:
                login.update(state="waiting", message=None, at=store.utc_now())
                name = add_character()
                login.update(state="done", message=f"Added {name}.", at=store.utc_now())
            except Exception as exc:  # noqa: BLE001  # shown on the page, never swallowed
                problem = f"The login didn't finish: {exc}"
                login.update(state="failed", message=problem, at=store.utc_now())
            finally:
                login_running.release()

        login.update(state="waiting", message=None, at=store.utc_now())
        threading.Thread(target=run, daemon=True).start()
        return back(CHARACTERS)

    @app.get("/callback")
    def login_callback(code: str = "", state: str = ""):
        """Where EVE's login page sends the browser back to, in hosted mode."""
        pending = pending_logins.pop(state, None) if web_login is not None else None
        if pending is None or not code:
            raise HTTPException(
                400,
                "This login wasn't started here or has expired. Press Add a character again.",
            )
        try:
            name = web_login.finish(pending, code)
        except Exception as exc:  # noqa: BLE001  # shown on the page, never swallowed
            login.update(state="failed", message=f"The login didn't finish: {exc}",
                         at=store.utc_now())  # fmt: skip
            return back(CHARACTERS)
        login.update(state="done", message=f"Added {name}.", at=store.utc_now())
        return back(CHARACTERS)

    async def _limited_body(request: Request) -> bytes | None:
        """The request body, or None once it passes INGEST_MAX_BYTES."""
        if int(request.headers.get("content-length") or 0) > INGEST_MAX_BYTES:
            return None
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > INGEST_MAX_BYTES:
                return None
        return body

    def _over_limit() -> bool:
        """Whether /ingest/ has taken INGEST_PER_HOUR requests in the last hour;
        if not, this one is counted."""
        now = time.monotonic()
        while ingest_times and now - ingest_times[0] > 3600:
            ingest_times.popleft()
        if len(ingest_times) >= INGEST_PER_HOUR:
            log.warning("Log sender: over %s requests an hour, refused", INGEST_PER_HOUR)
            return True
        ingest_times.append(now)
        return False

    @app.get("/ingest/hello")
    def ingest_hello(nonce: str = ""):
        """The log sender asks this first: the answer proves the server knows the
        token (an HMAC of the sender's fresh challenge), so the sender never
        sends the token to a device that doesn't."""
        if not ingest_token:
            return PlainTextResponse("Not found", status_code=404)
        if not 16 <= len(nonce) <= 128 or any(c not in "0123456789abcdef" for c in nonce):
            return JSONResponse({"error": "a hex challenge is needed"}, status_code=400)
        if _over_limit():
            return JSONResponse({"error": "too many requests"}, status_code=429)
        return JSONResponse({"proof": hosted_rules.proof(ingest_token, nonce)})

    @app.post("/ingest/gamelogs")
    async def ingest_gamelogs(request: Request):
        """Mining lines from the gaming PC's log sender (hosted mode): a token,
        at most INGEST_MAX_BYTES a request and INGEST_PER_HOUR requests an hour."""
        if not ingest_token:
            return PlainTextResponse("Not found", status_code=404)
        given = request.headers.get("authorization", "")
        if not secrets.compare_digest(given.encode(), f"Bearer {ingest_token}".encode()):
            return JSONResponse({"error": "wrong or missing token"}, status_code=401)
        body = await _limited_body(request)
        if body is None:
            log.warning("Log sender: a request over %s bytes, refused", INGEST_MAX_BYTES)
            return JSONResponse({"error": "too large"}, status_code=413)
        if _over_limit():
            return JSONResponse({"error": "too many requests"}, status_code=429)
        try:
            sent = json.loads(body)
            conn = db()
            try:
                stored = gamelogs.store_sent(conn, sent["file"], sent["listener"], sent["lines"])
            finally:
                conn.close()
        except (ValueError, KeyError, TypeError) as exc:
            log.warning("Log sender: an unreadable request, refused (%s)", exc)
            return JSONResponse({"error": "unreadable request"}, status_code=400)
        return JSONResponse({"stored": stored})

    @app.post("/refresh", response_class=HTMLResponse)
    def refresh_now(request: Request, token: str = Form(...)):
        check_form(request, token)
        problems = refresh() if refresh else ["Refreshing is not available here."]
        if not problems:
            return back(CHARACTERS, msg="refreshed")
        last_refresh["problems"] = list(problems)
        return back(CHARACTERS, msg="refresh_problems")

    @app.get(GOALS, response_class=HTMLResponse)
    def goals_page(request: Request, error: str | None = None):
        conn = db()
        try:
            statuses, problems = goals.evaluate_all(conn)
            worth_lines = {}
            for goal in (s.goal for s in statuses):
                if goal.kind in ("isk", "omega"):
                    whose = [goal.character_id] if goal.character_id else worth.all_characters(conn)
                    worth_lines[goal.goal_id] = _worth(conn, whose)
            names = _names(conn)
            suggestions = skills.names_in(
                conn,
                skills.SHIP_CATEGORY,
                skills.SKILL_CATEGORY,
                skills.IMPLANT_CATEGORY,
                skills.MODULE_CATEGORY,
                skills.DRONE_CATEGORY,
            )
            active = active_character(request, conn)
            omega_view = omega.compare(conn, active[0]) if active else None
            saved_fits = [f.name for f in fittings.saved(conn, active[0])] if active else []
            hours_a_week = pace.weekly_play(conn).minutes / 60
        finally:
            conn.close()
        return render(
            request,
            "goals.html",
            statuses=statuses,
            problems=problems,
            character_names=names,
            suggestions=suggestions,
            omega_view=omega_view,
            saved_fits=saved_fits,
            hours_a_week=hours_a_week,
            worth_lines=worth_lines,
            error=error,
        )

    @app.get("/goals/{goal_id}/drop", response_class=HTMLResponse)
    def confirm_drop_goal(request: Request, goal_id: int):
        conn = db()
        try:
            goal = next((g for g in goals.active_goals(conn) if g.goal_id == goal_id), None)
        finally:
            conn.close()
        if goal is None:
            raise HTTPException(404, f"No open goal {goal_id}.")
        return render(
            request,
            "confirm.html",
            title="Drop this goal?",
            what=goal.label,
            detail="It leaves the Goals page. Its history is kept.",
            action=f"/goals/{goal_id}/status",
            field=("status", "dropped"),
            button="Drop it",
            cancel=GOALS,
        )

    @app.get("/projects/{project_id}/drop", response_class=HTMLResponse)
    def confirm_drop_project(request: Request, project_id: int):
        conn = db()
        try:
            project = next(
                (p for p in projects.open_projects(conn) if p.project_id == project_id), None
            )
        finally:
            conn.close()
        if project is None:
            raise HTTPException(404, f"No open project {project_id}.")
        return render(
            request,
            "confirm.html",
            title="Drop this project?",
            what=project.title,
            detail="It stops being planned. Minutes already logged are kept.",
            action=f"/projects/{project_id}/dropped",
            field=None,
            button="Drop it",
            cancel=TODAY,
        )

    @app.post(GOALS)
    def add_goal(
        request: Request,
        token: str = Form(...),
        kind: str = Form(...),
        target: str = Form(""),
        level: int | None = Form(None),
        character: str = Form(""),
        fit: str = Form(""),
        after: str = Form(""),
    ):
        check_form(request, token)
        conn = db()
        try:
            character_id = goals.resolve_character(conn, character) if character else None
            after_id = int(after) if after.strip().isdigit() else None
            goals.check_after(conn, after_id, character_id)
            if kind == "item":
                if fit.strip():
                    raise GoalError("A fit only goes with a ship goal.")
                goals.add_goal(
                    conn, "fly_ship", character_id, type_id=skills.find_type(conn, target),
                    after_goal_id=after_id,
                )  # fmt: skip
            elif kind == "ship":
                chosen = goals.fit_from_text(conn, fit, character_id)
                if not target.strip() and chosen is None:
                    raise GoalError("Name the ship, or paste its fit.")
                hull = skills.find_type(conn, target) if target.strip() else None
                goals.add_goal(conn, "fly_ship", character_id, type_id=hull, fit=chosen,
                               after_goal_id=after_id)  # fmt: skip
            elif kind == "skill":
                goals.add_goal(
                    conn,
                    "skill",
                    character_id,
                    type_id=skills.find_type(conn, target),
                    level=level,
                    after_goal_id=after_id,
                )
            elif kind == "isk":
                amount = float(target.replace(",", "").replace("_", "").strip() or 0)
                goals.add_goal(conn, "isk", character_id, isk=amount, after_goal_id=after_id)
            elif kind == "omega":
                months = int(target.strip() or 1) if target.strip().isdigit() else level or 1
                goals.add_goal(conn, "omega", character_id, level=months, after_goal_id=after_id)
            else:
                raise GoalError("Choose a goal type.")
        except (GoalError, SkillsError, fittings.FitError, ValueError) as exc:
            message = str(exc) if not isinstance(exc, ValueError) else "Enter a number of ISK."
            return goals_page(request, error=message)
        finally:
            conn.close()
        return back(GOALS, msg="goal_added")

    @app.post("/goals/{goal_id}/status")
    def close_goal(request: Request, goal_id: int, token: str = Form(...), status: str = Form(...)):
        check_form(request, token)
        if status not in ("done", "dropped"):
            raise HTTPException(400, "Unknown status.")
        conn = db()
        try:
            goals.set_status(conn, goal_id, status)
        except GoalError as exc:
            raise HTTPException(404, str(exc)) from None
        finally:
            conn.close()
        return back(GOALS, msg=f"goal_{status}")

    @app.get("/skills", response_class=HTMLResponse)
    def skills_page(request: Request, character: str = "", item: str = ""):
        conn = db()
        try:
            names = _names(conn)
            suggestions = skills.names_in(conn, skills.SHIP_CATEGORY)
            if not character:
                active = active_character(request, conn)
                character = active[1] if active else ""
            requirement, error, clone_state = None, None, None
            train, train_goal, train_capped = [], None, False
            refining, refining_levels = None, []
            if character:
                try:
                    character_id = goals.resolve_character(conn, character)
                    train = train_next.suggestions(conn, character_id)
                    refining = train_next.refining_outlook(conn, character_id)
                    refining_levels = train_next.refining_suggestions(conn, character_id)
                    train_goal = train_next.goal_for(conn, character_id)
                    train_capped = not train and train_next.at_alpha_limits(conn, character_id)
                except GoalError:
                    pass
            if character and item:
                try:
                    character_id = goals.resolve_character(conn, character)
                    loaded = skills.load_character_skills(conn, character_id)
                    clone_state = loaded.clone_state
                    requirement = skills.plan(conn, loaded, skills.find_type(conn, item))
                except (GoalError, SkillsError) as exc:
                    error = str(exc)
        finally:
            conn.close()
        return render(
            request,
            "skills.html",
            character_names=names,
            suggestions=suggestions,
            character=character,
            item=item,
            requirement=requirement,
            clone_state=clone_state,
            train=train,
            train_goal=train_goal,
            train_capped=train_capped,
            refining=refining,
            refining_levels=refining_levels,
            error=error,
        )

    @app.get(TRADE, response_class=HTMLResponse)
    def trade_page(
        request: Request,
        source: str = "any",
        dest: str = "any",
        isk: str = "",
        cargo: str = "",
        mode: str = "instant",
        lowsec: bool = False,
        min_profit: str = "",
        sort: str = "profit",
    ):
        hub_names = [h.name for h in market.HUBS]
        source = source if source in hub_names else "any"
        dest = dest if dest in hub_names else "any"
        mode = mode if mode in TRADE_MODES else TRADE_MODES[0]
        conn = db()
        try:
            active = active_character(request, conn)
            scans = [(h.name, market_data.scanned_at(conn, h)) for h in market.HUBS]
            if market_data.stale(conn, _now()):
                start_market_scan()
            board = None
            if active and any(at for _, at in scans):
                board = trade.board(
                    conn,
                    active[0],
                    trade.Wanted(
                        source=None if source == "any" else source,
                        dest=None if dest == "any" else dest,
                        isk=_amount(isk, -1.0),
                        cargo_m3=_amount(cargo, -1.0),
                        mode=mode,
                        highsec_only=not lowsec,
                        min_profit=_amount(min_profit, 0.0),
                        by_hour=sort == "hour",
                    ),
                )
        finally:
            conn.close()
        return render(
            request,
            "trade.html",
            hubs=hub_names,
            scans=scans,
            scanning=market_status["running"],
            scan_problems=market_status["problems"],
            can_scan=scan_markets is not None,
            board=board,
            filters={
                "source": source,
                "dest": dest,
                "isk": isk,
                "cargo": cargo,
                "mode": mode,
                "lowsec": lowsec,
                "min_profit": min_profit,
                "sort": "hour" if sort == "hour" else "profit",
            },
        )

    @app.post(f"{TRADE}/refresh")
    def refresh_markets(request: Request, token: str = Form("")):
        check_form(request, token)
        start_market_scan(force=True)
        return RedirectResponse(f"{TRADE}?msg=market_refreshing", status_code=303)

    return app
