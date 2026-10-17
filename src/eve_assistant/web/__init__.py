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
import logging
import math
import secrets
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from eve_assistant import (
    backups,
    checklist,
    corrections,
    detection,
    explain,
    goals,
    history,
    numbers,
    outcomes,
    overview,
    pace,
    planner,
    preferences,
    projects,
    risk,
    sde,
    session_flow,
    sessions,
    skills,
    store,
)
from eve_assistant.goals import GoalError
from eve_assistant.skills import SkillsError

# make_plan(character_id, minutes, allow_lowsec, allow_risky) -> plan_id
PlanMaker = Callable[[int, float, bool, bool], int]
SnapshotTaker = Callable[[int], list[str]]
# Returns an object with .problems (list[str]) and .retry_after (seconds or None).
AutoRefresher = Callable[[], object]
AUTO_TICK_SECONDS = 60.0
log = logging.getLogger(__name__)
TODAY = "/today"
GOALS = "/goals"
PREFERENCES = "/preferences"
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

# While a session runs these pages reload themselves (a meta refresh: no
# JavaScript), so the session view's timer and wallet stay current.
RELOAD_SECONDS = 60
RELOADING_PAGES = ("today", "characters", "history")

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

# Colour themes, kept in a cookie set by the form in every page's footer.
THEMES = ("dark", "light", "system")
# The cookie gets one of these constants, never the posted text itself.
THEME_VALUES = {theme: theme for theme in THEMES}
DEFAULT_THEME = "dark"
THEME_COOKIE = "theme"
THEME_MAX_AGE = 365 * 24 * 3600
# Pages the theme form may return to; anything else goes to Today.
# Each maps to itself, so the redirect is always one of these constants.
RETURN_PAGES = {
    page: page for page in (TODAY, "/characters", GOALS, "/skills", "/history", PREFERENCES)
}

BAND_LABELS = {
    "none": "No player risk (Exordium)",
    "very_low": "Very low",
    "low": "Low (highsec)",
    "medium": "Medium (lowsec)",
    "high": "High (nullsec, unknown)",
}

MESSAGES = {
    "goal_added": "Goal added.",
    "goal_done": "Goal marked done.",
    "goal_dropped": "Goal dropped.",
    "refreshed": "Character data refreshed from EVE.",
    "do": "Project started: it's under Open projects.",
    "not_now": "Recorded: not now.",
    "mute": "Muted. You can undo that on the Preferences page.",
    "muted": "Muted.",
    "unmuted": "Unmuted.",
    "project_done": "Project marked done.",
    "project_dropped": "Project dropped.",
    "play_time": "Play time saved.",
    "session_started": "Session started. Press “I’m done” when you stop.",
    "session_finished": "Session recorded.",
    "session_cancelled": "Session cancelled.",
    "detected_recorded": "Session recorded. Its outcome is worked out from EVE's data.",
    "detected_dismissed": "Got it, that one won't be offered again.",
}


def _isk(value) -> str:
    return numbers.isk(value)


def _when(value) -> str:
    """ISO UTC timestamp -> '2026-10-16 02:50 UTC'."""
    if not value:
        return "never"
    return f"{value[:10]} {value[11:16]} UTC"


def _now() -> datetime:
    return datetime.now(UTC)


def _minutes_since(value) -> float | None:
    if not value:
        return None
    then = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    return max(0.0, (_now() - then).total_seconds() / 60)


def _ago(value) -> str:
    """ISO UTC timestamp -> '12 min ago', '6 hours ago', '3 days ago'."""
    minutes = _minutes_since(value)
    if minutes is None:
        return "never"
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes:.0f} min ago"
    hours = minutes / 60
    if hours < 48:
        return f"{hours:.0f} hour{'s' if round(hours) != 1 else ''} ago"
    return f"{hours / 24:.0f} days ago"


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


def create_app(
    db_path: Path | str = store.DEFAULT_DB_PATH,
    refresh: Callable[[], list[str]] | None = None,
    make_plan: PlanMaker | None = None,
    snapshot: SnapshotTaker | None = None,
    allowed_hosts: tuple[str, ...] = LOCAL_HOSTS,
    auto_refresh: AutoRefresher | None = None,
    auto_tick_seconds: float = AUTO_TICK_SECONDS,
    backups_dir: Path | str | None = None,
) -> FastAPI:
    """refresh: called by the Refresh button; returns problem lines, empty when all went well.
    make_plan: builds and records a plan (needs ESI), returning its ID.
    snapshot: snapshots one character (needs ESI), returning problem lines.
    auto_refresh: one background refresh tick (needs ESI), run every auto_tick_seconds."""
    auto_status: dict = {"enabled": auto_refresh is not None, "last_run": None, "problems": []}

    async def auto_loop() -> None:
        while True:
            delay = auto_tick_seconds
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
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts))

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["isk"] = _isk
    templates.env.filters["duration"] = skills.format_minutes
    templates.env.filters["security_class"] = _security_class
    templates.env.filters["security"] = _security
    templates.env.filters["when"] = _when
    templates.env.filters["ago"] = _ago
    templates.env.globals["minutes_since"] = _minutes_since
    templates.env.globals["plan_stale_minutes"] = PLAN_STALE_MINUTES
    templates.env.globals["describe"] = skills.describe
    templates.env.globals["explain"] = explain.describe
    templates.env.globals["risk_summary"] = explain.risk_summary
    templates.env.globals["why_not"] = explain.WHY_NOT
    templates.env.globals["why_not_here"] = planner.why_not
    templates.env.globals["per_hour"] = numbers.per_hour
    templates.env.globals["risk_labels"] = RISK_LABELS
    templates.env.globals["category_labels"] = preferences.KNOWN
    form_token = secrets.token_urlsafe(32)

    def db() -> sqlite3.Connection:
        return store.connect(db_path)

    def render(request: Request, name: str, **context) -> HTMLResponse:
        message = MESSAGES.get(request.query_params.get("msg", ""))
        conn = db()
        try:
            static_data = sde.update_status(conn)
            playing = bool(sessions.open_sessions(conn))
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
        if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
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
    def today_page(request: Request, error: str | None = None):
        conn = db()
        try:
            names = _names(conn)
            latest = planner.latest_plan_id(conn)
            recent = planner.load_plan(conn, latest) if latest else None
            open_projects = _open_projects(conn)
            playing = sessions.open_sessions(conn)
            session_views = checklist.views(conn)
            results = outcomes.recent(conn, 5)
            noticed = detection.suggestions(conn)
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
            checklist.mark(conn, session_id, item_id, done=action == "done")
        except sessions.SessionNotFound as exc:
            raise HTTPException(404, str(exc)) from None
        except (checklist.ChecklistError, planner.PlanError, projects.ProjectError) as exc:
            raise HTTPException(400, str(exc)) from None
        finally:
            conn.close()
        return RedirectResponse(f"{TODAY}#session-{session_id}", status_code=303)

    def _open_projects(conn) -> list[tuple[str, projects.Project]]:
        names = dict(conn.execute("SELECT character_id, name FROM characters").fetchall())
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
    ):
        check_form(request, token)
        if make_plan is None:
            return today_page(request, error="Planning is not available here.")
        conn = db()
        try:
            character_id = goals.resolve_character(conn, character)
        except GoalError as exc:
            return today_page(request, error=str(exc))
        finally:
            conn.close()
        try:
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

    @app.get(PREFERENCES, response_class=HTMLResponse)
    def preferences_page(request: Request):
        conn = db()
        try:
            active = preferences.active_mutes(conn)
            weekly = projects.weekly_minutes(conn)
            # Characters with the same text share one line ("not yet checked" thrice is noise).
            learned_by_text: dict[str, list[str]] = {}
            for r in conn.execute("SELECT character_id, name FROM characters ORDER BY name"):
                reason = corrections.correction(conn, r["character_id"], "mining_rate").reason()
                text = explain.describe(reason.code, reason.detail)
                learned_by_text.setdefault(text, []).append(r["name"])
            learned = [(", ".join(names), text) for text, names in learned_by_text.items()]
            play_time = pace.weekly_play(conn)
            finish = pace.finish_rate(conn)
            folder = Path(backups_dir) if backups_dir else backups.default_folder(db_path)
            kept = backups.list_backups(folder)
        finally:
            conn.close()
        return render(
            request,
            "preferences.html",
            active=active,
            categories=preferences.KNOWN,
            risk_matrix=risk.MATRIX,
            band_labels=BAND_LABELS,
            weekly_hours=weekly / 60,
            learned=learned,
            play_time=play_time,
            finish=finish,
            backups_folder=folder,
            backups_kept=kept,
        )

    @app.post("/preferences-play-time")
    def change_play_time(request: Request, token: str = Form(...), hours: float = Form(...)):
        check_form(request, token)
        conn = db()
        try:
            projects.set_weekly_minutes(conn, hours * 60)
        except projects.ProjectError as exc:
            raise HTTPException(400, str(exc)) from None
        finally:
            conn.close()
        return back(PREFERENCES, msg="play_time")

    @app.post("/preferences/{action}")
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
        return back(PREFERENCES, msg=f"{action}d")

    @app.get("/history", response_class=HTMLResponse)
    def history_page(request: Request):
        conn = db()
        try:
            outcomes.update(conn)  # pick up anything that settled since
            past = history.sessions_history(conn)
            speeds = [
                (r["name"], corrections.mining_observations(conn, r["character_id"]))
                for r in conn.execute("SELECT character_id, name FROM characters ORDER BY name")
            ]
        finally:
            conn.close()
        return render(
            request,
            "history.html",
            past=past,
            speeds=[(name, found) for name, found in speeds if found],
        )

    @app.get("/characters", response_class=HTMLResponse)
    def characters_page(request: Request):
        conn = db()
        try:
            summaries = overview.all_characters(conn)
        finally:
            conn.close()
        return render(request, "characters.html", characters=summaries, problems=[])

    @app.post("/refresh", response_class=HTMLResponse)
    def refresh_now(request: Request, token: str = Form(...)):
        check_form(request, token)
        problems = refresh() if refresh else ["Refreshing is not available here."]
        if not problems:
            return back("/characters", msg="refreshed")
        conn = db()
        try:
            summaries = overview.all_characters(conn)
        finally:
            conn.close()
        return render(request, "characters.html", characters=summaries, problems=problems)

    @app.get(GOALS, response_class=HTMLResponse)
    def goals_page(request: Request, error: str | None = None):
        conn = db()
        try:
            statuses, problems = [], []
            for goal in goals.active_goals(conn):
                try:
                    statuses.append(goals.evaluate(conn, goal))
                except SkillsError as exc:
                    problems.append(f"{goal.label}: {exc}")
            names = _names(conn)
        finally:
            conn.close()
        return render(
            request,
            "goals.html",
            statuses=statuses,
            problems=problems,
            character_names=names,
            error=error,
        )

    @app.post(GOALS)
    def add_goal(
        request: Request,
        token: str = Form(...),
        kind: str = Form(...),
        target: str = Form(""),
        level: int | None = Form(None),
        character: str = Form(""),
    ):
        check_form(request, token)
        conn = db()
        try:
            character_id = goals.resolve_character(conn, character) if character else None
            if kind == "ship":
                goals.add_goal(
                    conn, "fly_ship", character_id, type_id=skills.find_type(conn, target)
                )
            elif kind == "skill":
                goals.add_goal(
                    conn,
                    "skill",
                    character_id,
                    type_id=skills.find_type(conn, target),
                    level=level,
                )
            elif kind == "isk":
                amount = float(target.replace(",", "").replace("_", "").strip() or 0)
                goals.add_goal(conn, "isk", character_id, isk=amount)
            else:
                raise GoalError("Choose a goal type.")
        except (GoalError, SkillsError, ValueError) as exc:
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
            requirement, error, clone_state = None, None, None
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
            character=character,
            item=item,
            requirement=requirement,
            clone_state=clone_state,
            error=error,
        )

    return app
