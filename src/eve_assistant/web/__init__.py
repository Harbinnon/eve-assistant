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
import secrets
import sqlite3
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from eve_assistant import (
    backups,
    corrections,
    explain,
    goals,
    history,
    outcomes,
    overview,
    pace,
    planner,
    preferences,
    projects,
    risk,
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
    "do": "Recorded: you're doing it.",
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
}


def _isk(value) -> str:
    if value is None:
        return "?"
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(value) >= limit:
            return f"{value / limit:,.1f}{suffix}"
    return f"{value:,.0f}"


def _when(value) -> str:
    """ISO UTC timestamp -> '2026-10-16 02:50 UTC'."""
    if not value:
        return "never"
    return f"{value[:10]} {value[11:16]} UTC"


def _security_class(value) -> str:
    if value is None:
        return "sec-unknown"
    rounded = round(value, 1)
    if rounded >= 0.5:
        return "sec-high"
    if rounded > 0.0:
        return "sec-low"
    return "sec-null"


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
    templates.env.filters["when"] = _when
    templates.env.globals["describe"] = skills.describe
    templates.env.globals["explain"] = explain.describe
    templates.env.globals["risk_summary"] = explain.risk_summary
    templates.env.globals["why_not"] = explain.WHY_NOT
    form_token = secrets.token_urlsafe(32)

    def db() -> sqlite3.Connection:
        return store.connect(db_path)

    def render(request: Request, name: str, **context) -> HTMLResponse:
        message = MESSAGES.get(request.query_params.get("msg", ""))
        return templates.TemplateResponse(
            request,
            name,
            {
                "form_token": form_token,
                "message": message,
                "auto": auto_status,
                "page": name.split(".")[0],
                **context,
            },  # fmt: skip
        )

    def check_form(request: Request, token: str) -> None:
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "Cross-site form posts are not allowed.")
        if not secrets.compare_digest(token, form_token):
            raise HTTPException(403, "This form has expired. Reload the page and try again.")

    def back(path: str, **params) -> RedirectResponse:
        query = f"?{urlencode(params)}" if params else ""
        return RedirectResponse(f"{path}{query}", status_code=303)

    @app.get("/")
    def home():
        return back(TODAY)

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
            results = outcomes.recent(conn, 5)
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
        )

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
            raise HTTPException(404, "Unknown action.")
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

    @app.post("/plan-items/{item_id}/respond")
    def respond(request: Request, item_id: int, token: str = Form(...), response: str = Form(...)):
        check_form(request, token)
        conn = db()
        try:
            row = conn.execute(
                "SELECT plan_id FROM plan_items WHERE item_id = ?", (item_id,)
            ).fetchone()
            if row is None:
                raise HTTPException(404, "No such suggestion.")
            try:
                planner.respond(conn, item_id, response)
            except planner.PlanError as exc:
                raise HTTPException(400, str(exc)) from None
        finally:
            conn.close()
        return back(f"/plans/{row['plan_id']}", msg=response)

    @app.get("/preferences", response_class=HTMLResponse)
    def preferences_page(request: Request):
        conn = db()
        try:
            active = preferences.active_mutes(conn)
            weekly = projects.weekly_minutes(conn)
            learned = [
                (r["name"], corrections.correction(conn, r["character_id"], "mining_rate"))
                for r in conn.execute("SELECT character_id, name FROM characters ORDER BY name")
            ]
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
        return back("/preferences", msg="play_time")

    @app.post("/preferences/{action}")
    def change_preference(
        request: Request, action: str, token: str = Form(...), category: str = Form(...)
    ):
        check_form(request, token)
        if action not in ("mute", "unmute"):
            raise HTTPException(404, "Unknown action.")
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
        return back("/preferences", msg=f"{action}d")

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

    @app.get("/goals", response_class=HTMLResponse)
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

    @app.post("/goals")
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
        return back("/goals", msg="goal_added")

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
        return back("/goals", msg=f"goal_{status}")

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
