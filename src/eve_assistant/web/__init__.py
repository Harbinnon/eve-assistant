"""The dashboard: server-rendered pages over the service layer.

Local mode only for now: it listens on 127.0.0.1 and has no login, so it
refuses to run in hosted mode until hosted mode (with a login) is built.
Forms carry a per-process token and cross-site posts are rejected, so a
web page in another tab cannot change anything here.
"""

import secrets
import sqlite3
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from eve_assistant import goals, overview, skills, store
from eve_assistant.goals import GoalError
from eve_assistant.skills import SkillsError

HERE = Path(__file__).parent

MESSAGES = {
    "goal_added": "Goal added.",
    "goal_done": "Goal marked done.",
    "goal_dropped": "Goal dropped.",
    "refreshed": "Character data refreshed from EVE.",
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
) -> FastAPI:
    """refresh: called by the Refresh button; returns problem lines, empty when all went well."""
    app = FastAPI(title="EVE Assistant", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["isk"] = _isk
    templates.env.filters["duration"] = skills.format_minutes
    templates.env.filters["security_class"] = _security_class
    templates.env.filters["when"] = _when
    templates.env.globals["describe"] = skills.describe
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
        return back("/characters")

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
            names = [r["name"] for r in conn.execute("SELECT name FROM characters ORDER BY name")]
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
            names = [r["name"] for r in conn.execute("SELECT name FROM characters ORDER BY name")]
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
