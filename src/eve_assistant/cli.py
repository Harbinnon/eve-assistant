"""Command line: eve-assistant <command>."""

import argparse
import errno
import sys
from pathlib import Path

from eve_assistant import (
    __version__,
    accounts,
    activities,
    alerts,
    auth,
    backups,
    couriers,
    detection,
    explain,
    fittings,
    gamelogs,
    goals,
    images,
    launcher,
    logsender,
    market,
    mining,
    ore,
    outcomes,
    pace,
    planner,
    preferences,
    projects,
    refresh,
    sde,
    session_flow,
    sessions,
    skills,
    snapshots,
    store,
    together,
)
from eve_assistant.accounts import AccountError
from eve_assistant.config import ConfigError, load_config
from eve_assistant.esi import scopes, user_agent
from eve_assistant.esi.client import EsiClient, EsiError
from eve_assistant.esi.sso import SSOError
from eve_assistant.goals import GoalError
from eve_assistant.sde import SdeError
from eve_assistant.secrets_store import SecretStoreError, default_store
from eve_assistant.skills import SkillsError


def _cmd_login(_args) -> int:
    config = load_config()
    secrets = default_store()
    print("Opening EVE's login page in your browser. Pick the character to add.")
    print("Waiting up to 5 minutes...")
    with auth.http_client(config) as client:
        character = auth.login_character(config, secrets, client)
    print(f"Logged in: {character.name} ({character.character_id}).")
    missing = character.missing(scopes(config.planets))
    if missing:
        print("Warning: these permissions were not granted, so some features won't work:")
        for scope in missing:
            print(f"  {scope}")
        print("Check the scopes on your ESI application (docs/setup-esi.md), then log in again.")
    if config.contact is None:
        print("Tip: set [esi] contact in config/local.toml so CCP can reach you about API use.")
    return 0


def _cmd_characters(args) -> int:
    secrets = default_store()
    characters = auth.list_characters(secrets)
    if not characters:
        print("No characters logged in. Run: eve-assistant login")
        return 0
    config = load_config() if args.check else None
    failures = 0
    for character in characters:
        line = f"{character.name} ({character.character_id})"
        if config is not None:
            try:
                with auth.http_client(config) as client:
                    auth.access_token(config, secrets, client, character.character_id)
                line += "  token OK"
            except SSOError as exc:
                failures += 1
                line += f"  token FAILED: {exc}"
        if character.missing_scopes:
            line += f"  ({len(character.missing_scopes)} permissions missing)"
        print(line)
    return 1 if failures else 0


def _cmd_logout(args) -> int:
    secrets = default_store()
    if auth.logout(secrets, args.character_id):
        print(f"Removed the stored token for {args.character_id} from this PC.")
        print(f"To revoke the app's access entirely, also visit {auth.REVOKE_PAGE}")
        return 0
    print(f"No stored token for {args.character_id}.")
    return 1


def _cmd_sde_update(args) -> int:
    config = load_config()
    with store.connect() as db, auth.http_client(config) as client:
        current = sde.loaded_build(db)
        print(f"Loaded static data: {current.build_number if current else 'none'}.")
        print("Checking for a newer build (the first download is about 100 MB)...")
        build, loaded = sde.update(db, client, force=args.force)
    print(
        f"Static data build {build.build_number} ({build.release_date}) "
        + ("loaded." if loaded else "is already current.")
    )
    return 0


def _cmd_snapshot(_args) -> int:
    report = refresh.refresh_all(load_config(), default_store())
    for line in report.lines:
        print(line)
    if report.priced:
        print(f"Market prices: {report.priced:,} items.")
    for problem in report.problems:
        print(f"  {problem}")
    return 1 if report.problems else 0


CONFIG_PROBLEM = 2  # exit code: a restart won't fix it, so the server stops retrying
NOT_YET = 1  # exit code: worth retrying (a crash exits 1 too)
LISTEN_WAIT = 60.0  # seconds to wait at boot for the server's own address to appear
INGEST_VARIABLE = "EVE_ASSISTANT_INGEST_TOKEN"  # the log sender's token, on the server


def _app(config, secrets, **extra):
    """The dashboard, wired to this config and secret store."""
    from eve_assistant.web import AccountStatus, create_app

    def do_refresh() -> list[str]:
        return refresh.refresh_all(config, secrets).problems

    def do_plan(character_id: int, minutes: float, allow_lowsec: bool, allow_risky: bool) -> int:
        return refresh.plan_now(config, secrets, character_id, minutes, allow_lowsec, allow_risky)

    def do_team_plan(ids: list[int], minutes: float, allow_lowsec: bool, allow_risky: bool) -> int:
        return refresh.team_plan_now(config, secrets, ids, minutes, allow_lowsec, allow_risky)

    def do_snapshot(character_id: int) -> list[str]:
        return refresh.snapshot_character(config, secrets, character_id)

    def do_auto_refresh():
        return refresh.auto_refresh_once(config, secrets)

    def do_add_character() -> str:
        with auth.http_client(config) as client:
            character = auth.login_character(config, secrets, client, store_lock=refresh.ESI_LOCK)
        refresh.snapshot_character(config, secrets, character.character_id)
        return character.name

    settings = {
        "refresh": do_refresh,
        "make_plan": do_plan,
        "make_team_plan": do_team_plan,
        "snapshot": do_snapshot,
        "auto_refresh": do_auto_refresh if config.auto_refresh else None,
        "backups_dir": config.backups_dir,
        "add_character": do_add_character,
        "fetch_image": lambda address: images.download(address, user_agent(config.contact)),
        "account": AccountStatus(
            missing_scopes=lambda: {
                c.character_id: c.missing(scopes(config.planets))
                for c in auth.list_characters(secrets)
            },
            planets_on=config.planets,
            alert_channels=lambda: set(alerts.addresses(secrets)),
        ),
        "scan_markets": lambda force=False: refresh.scan_markets(config, force=force),
    }
    return create_app(**{**settings, **extra})


def _cmd_serve(args) -> int:
    import uvicorn

    if args.hosted:
        return _serve_hosted()
    config = load_config()
    if config.mode != "local":
        print("This config is for hosted mode: run eve-assistant serve --hosted.")
        return CONFIG_PROBLEM
    if args.open:
        if launcher.dashboard_running(args.port):
            print(f"The dashboard is already running: {launcher.url(args.port)}")
            launcher.open_in_browser(args.port)
            return 0
        if launcher.listening(args.port):
            print(f"Something else is using port {args.port}. Close it or pass --port.")
            return 2
    app = _app(config, default_store())
    print(f"Dashboard: http://127.0.0.1:{args.port}  (Ctrl+C or close this window to stop)")
    if args.open:
        launcher.open_when_listening(args.port)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


def _hosted_setup():
    """The hosted copy's config and secret store, or exit CONFIG_PROBLEM with a reason."""
    config = load_config()
    if config.hosted is None:
        raise ConfigError(
            'serve --hosted needs mode = "hosted" under [app] and a [hosted] section.'
        )
    secrets = default_store()
    secrets.load()  # a wrong key stops here, not on the first refresh
    return config, secrets


def _wait_to_listen(host: str, port: int, wait: float = LISTEN_WAIT, sleep=None) -> OSError | None:
    """Whether host:port can be listened on: None if so, else the error. At boot
    the server's own address can be missing for a few seconds while the network
    starts (EADDRNOTAVAIL); that is waited out, up to `wait` seconds. Anything
    else (the port in use, a bad address) is returned at once."""
    import socket
    import time

    sleep = sleep or time.sleep
    deadline = time.monotonic() + wait
    said = False
    while True:
        try:
            socket.create_server((host, port)).close()
            return None
        except OSError as exc:
            if exc.errno != errno.EADDRNOTAVAIL or time.monotonic() >= deadline:
                return exc
            if not said:
                print(f"Waiting for {host} to come up (the network is still starting).",
                      file=sys.stderr, flush=True)  # fmt: skip
                said = True
            sleep(2)


def _serve_hosted() -> int:
    """The hosted copy (roadmap 7.2): behind the home lab's proxy and sign-in.
    Exits CONFIG_PROBLEM, with one line saying why, for anything a restart
    won't fix; a crash exits 1."""
    import os
    import types

    import uvicorn

    try:
        config, secrets = _hosted_setup()
    except (ConfigError, SecretStoreError) as exc:
        print(f"Configuration problem: {exc}", file=sys.stderr)
        return CONFIG_PROBLEM
    hosted = config.hosted
    problem = _wait_to_listen(hosted.listen, hosted.port)
    if problem is not None:
        if problem.errno == errno.EADDRNOTAVAIL:
            waited = f"{LISTEN_WAIT:.0f} s"
            print(f"{hosted.listen} still isn't one of this server's addresses after {waited}; "
                  "exiting to be started again.", file=sys.stderr)  # fmt: skip
            return NOT_YET
        print(f"Configuration problem: can't listen on {hosted.listen}:{hosted.port} "
              f"({problem.strerror}).", file=sys.stderr)  # fmt: skip
        return CONFIG_PROBLEM

    def finish(pending, code: str) -> str:
        with auth.http_client(config) as client:
            character = auth.finish_login(config, secrets, client, pending, code,
                                          store_lock=refresh.ESI_LOCK)  # fmt: skip
        refresh.snapshot_character(config, secrets, character.character_id)
        return character.name

    from eve_assistant.web import Hosting

    login = types.SimpleNamespace(start=lambda: auth.start_login(config), finish=finish)
    app = _app(
        config,
        secrets,
        add_character=None,
        hosting=Hosting(hosted, login, os.environ.get(INGEST_VARIABLE) or None),
    )
    print(f"Hosted dashboard on {hosted.listen}:{hosted.port} for {hosted.public_origin}",
          flush=True)  # fmt: skip
    uvicorn.run(app, host=hosted.listen, port=hosted.port, proxy_headers=False,
                log_level="warning")  # fmt: skip
    return 0


def _send_logs_setup(secrets, ask) -> int:
    import getpass

    ask = ask or getpass.getpass
    answer = ask("The hosted copy's log token (Enter keeps it, - removes it): ").strip()
    if answer:
        logsender.save_token(secrets, None if answer == "-" else answer)
        print("Token removed." if answer == "-" else "Token saved.")
    return 0


def _send_logs_problem(config, secrets) -> str | None:
    """What stops send-logs from running, if anything."""
    if not config.send_logs_url:
        return ("Set [send_logs] url in config/local.toml to the hosted copy's address on "
                "your home network, e.g. http://192.168.1.21:8700.")  # fmt: skip
    if config.gamelogs_dir is None:
        return "Set [logs] gamelogs_dir in config/local.toml to EVE's Gamelogs folder."
    if not logsender.token(secrets):
        return "No token yet. Run: eve-assistant send-logs --setup"
    return None


def _send_logs_owners(config, db_path: Path | None = None) -> set[str]:
    """Whose logs to send: the characters listed under [send_logs] characters,
    plus any logged in on a dashboard on this PC. Never makes a database, so a
    PC that only sends logs needs no dashboard."""
    owners = set(config.send_logs_characters)
    path = db_path or store.DEFAULT_DB_PATH
    if path.exists():
        with store.connect(path) as db:
            owners |= {r[0] for r in db.execute("SELECT name FROM characters")}
    return owners


def _cmd_send_logs(args, ask=None) -> int:
    """Send new mining lines from this PC's game logs to the hosted copy."""
    import time

    secrets = default_store()
    if args.setup:
        return _send_logs_setup(secrets, ask)
    config = load_config()
    problem = _send_logs_problem(config, secrets)
    if problem:
        print(problem)
        return 1
    owners = _send_logs_owners(config)
    if not owners:
        print('List your characters in config/local.toml: [send_logs] characters = ["Your '
              'Main", "Your Alt"].')  # fmt: skip
        return 1
    while True:
        with auth.http_client(config) as http:  # carries the descriptive User-Agent
            report = logsender.send(http, config.send_logs_url, logsender.token(secrets),
                                    config.gamelogs_dir, owners)  # fmt: skip
        logsender.note(report)  # the only trace of a run from the scheduled task
        print(report.summary)
        for line in [*report.problems, *report.warnings]:
            print(f"  {line}")
        if not args.watch:
            # Away from home isn't a failure: the next run catches up.
            return 1 if report.problems and not report.unreachable else 0
        time.sleep(args.watch * 60)


def _cmd_shortcut(args) -> int:
    path = launcher.create_shortcut(args.folder, args.port)
    print(f"Made {path}. Double-click it to start the dashboard and open it.")
    print("Its window starts minimised; close that window to stop the dashboard.")
    return 0


def _print_requirement(req: skills.Requirement, is_alpha: bool) -> None:
    if req.usable_now:
        print("  Can use it now.")
        return
    if req.usable_with_omega_now:
        print("  Skills are trained; Omega would unlock it straight away.")
    for step in req.steps:
        flag = "" if step.allowed_for_alpha else "  (Omega only)"
        print(
            f"  {step.skill_name} {step.level}: {step.skillpoints:,} SP, "
            f"{skills.format_minutes(step.minutes_omega)} as Omega{flag}"
        )
    if req.steps:
        print(f"  Total as Omega: {skills.format_minutes(req.minutes_as_omega)}")
        if is_alpha:
            if req.minutes_as_alpha is None:
                print("  As Alpha: not possible.")
            else:
                print(
                    f"  Total as Alpha: about {skills.format_minutes(req.minutes_as_alpha)} "
                    "(Alpha speed assumed half; not yet verified)"
                )
    for reason in req.reasons:
        print(f"  - {skills.describe(reason)}")


def _cmd_skills(args) -> int:
    with store.connect() as db:
        character_id = goals.resolve_character(db, args.character)
        character = skills.load_character_skills(db, character_id)
        type_id = skills.find_type(db, args.item)
        req = skills.plan(db, character, type_id)
        print(f"{req.type_name} for character {character_id} [{character.clone_state}]:")
        _print_requirement(req, character.is_alpha)
    return 0


def _cmd_goal_add(args) -> int:
    with store.connect() as db:
        character_id = goals.resolve_character(db, args.character) if args.character else None
        goals.check_after(db, args.after, character_id)
        after = {"after_goal_id": args.after}
        if args.kind == "item":
            goal_id = goals.add_goal(
                db, "fly_ship", character_id, type_id=skills.find_type(db, args.target), **after
            )
        elif args.kind == "ship":
            fit_text = args.fit or ""
            if fit_text and Path(fit_text).is_file():
                fit_text = Path(fit_text).read_text(encoding="utf-8")
            chosen = goals.fit_from_text(db, fit_text, character_id)
            goal_id = goals.add_goal(
                db, "fly_ship", character_id, type_id=skills.find_type(db, args.target),
                fit=chosen, **after,
            )  # fmt: skip
        elif args.kind == "skill":
            goal_id = goals.add_goal(
                db,
                "skill",
                character_id,
                type_id=skills.find_type(db, args.target),
                level=args.level,
                **after,
            )
        else:
            amount = float(args.target.replace(",", "").replace("_", ""))
            goal_id = goals.add_goal(db, "isk", character_id, isk=amount, **after)
    print(f"Added goal {goal_id}. See it with: eve-assistant goals")
    return 0


def _cmd_goals(_args) -> int:
    with store.connect() as db:
        statuses, problems = goals.evaluate_all(db)
        if not statuses and not problems:
            print("No goals yet. Example: eve-assistant goal add ship Retriever --character <name>")
            return 0
        for line in problems:
            print(line)
        for status in statuses:
            goal = status.goal
            who = f" ({status.character_name})" if status.character_name else ""
            print(f"[{goal.goal_id}] {goal.label}{who}")
            if status.after is not None:
                print(f"  After [{status.after.goal_id}] {status.after.label}: "
                      f"{status.chain_isk:,.0f} ISK with the goals before")  # fmt: skip
            if status.requirement is not None:
                _print_requirement(status.requirement, is_alpha=bool(status.is_alpha))
            if status.fit_cost is not None:
                cost = status.fit_cost
                yours = f"; already yours: {', '.join(cost.owned)}" if cost.owned else ""
                print(f'  Fit "{cost.fit.name}" (your name for it): {cost.total:,.0f} ISK to buy '
                      f"(CCP averages){yours}")  # fmt: skip
            elif status.price is not None:
                print(f"  Rough price: {status.price:,.0f} ISK (CCP average, not a hub quote)")
            if status.isk_needed is not None:
                print(f"  ISK still needed: {status.isk_needed:,.0f} (wallet {status.wallet:,.0f})")
            for reason in status.reasons:
                if reason.code == "no_price":
                    print("  - No market price yet; run `eve-assistant snapshot`.")
                elif reason.code == "clone_state_unknown":
                    print("  - Account clone state unknown; assumed Alpha (`account state`).")
    return 0


def _cmd_goal_status(args) -> int:
    with store.connect() as db:
        goals.set_status(db, args.goal_id, args.status)
    print(f"Goal {args.goal_id} marked {args.status}.")
    return 0


def _resolve_station(db, character_id: int, station: str | None) -> int:
    if station:
        for hub in market.HUBS:
            if hub.name.lower() == station.lower():
                return hub.station_id
        if station.isdigit():
            return int(station)
        raise ore.OreError(f"Unknown station '{station}'. Use a hub name or a station ID.")
    location = snapshots.latest(db, character_id, "location") or {}
    if location.get("station_id"):
        return location["station_id"]
    if location.get("structure_id"):
        return location["structure_id"]  # ore.analyse checks the character can access it
    raise ore.OreError("The character isn't docked. Pass --station, e.g. --station Jita.")


def _cmd_ore(args) -> int:
    config = load_config()
    secrets = default_store()
    with store.connect() as db, auth.http_client(config) as client:
        character_id = goals.resolve_character(db, args.character)
        type_id = skills.find_type(db, args.ore)
        station_id = _resolve_station(db, character_id, args.station)
        esi = EsiClient(client, db, token_for=auth.AccessTokens(config, secrets, client).get)
        result = ore.analyse(
            db, esi, character_id, type_id, args.quantity, station_id,
            highsec_only=not args.allow_lowsec,
        )  # fmt: skip
    print(
        f"{result.quantity:,} {result.type_name} ({result.volume_m3:,.0f} m3"
        + (
            f", {result.compressed_volume_m3:,.0f} m3 compressed"
            if result.compressed_volume_m3
            else ""
        )  # fmt: skip
        + f"). Refining here: {result.refine_yield:.1%} yield, {result.refine_tax:.1%} tax."
    )
    for option in result.options:
        flag = "" if option.instant else "  (waits for buyers)"
        print(
            f"  {option.isk:>14,.0f} ISK  {option.active_minutes:>5.0f} min  {option.title}{flag}"
        )
        for reason in option.reasons:
            if reason.code == "not_enough_buyers":
                print(f"{'':24}not enough buyers for {reason.detail['unsold']:,} units")
    print("  Travel time assumes about 1 minute per jump until the tool learns your pace.")
    return 0


def _cmd_mining(args) -> int:
    with store.connect() as db:
        character_id = goals.resolve_character(db, args.character)
        if args.ship:
            character = skills.load_character_skills(db, character_id)
            ship_id = skills.find_type(db, args.ship)
            modules = [skills.find_type(db, m) for m in args.module or []]
            estimate = mining.estimate(db, character, ship_id, modules)
        else:
            estimate = mining.estimate_current(db, character_id)

        print(f"{estimate.ship_name}: about {estimate.m3_per_minute:,.0f} m3 of ore per minute")
        for module in estimate.modules:
            print(
                f"  {module.name}: {module.m3_per_cycle:,.1f} m3 every "
                f"{module.cycle_seconds:.1f} s ({module.m3_per_minute:,.0f} m3/min)"
            )
        scaled = ", ".join(f"{k} x{v:.2f}" for k, v in estimate.multipliers.items())
        print(f"  Scaled by: {scaled}")
        if estimate.minutes_to_fill_hold:
            print(
                f"  Ore hold {estimate.hold_m3:,.0f} m3: full after about "
                f"{estimate.minutes_to_fill_hold:,.0f} minutes"
            )
        for reason in estimate.reasons:
            if reason.code == "no_mining_modules":
                print("  - No mining modules fitted. Try --ship and --module to compare fits.")
            elif reason.code == "crystals_not_modelled":
                print("  - Mining crystals are not counted yet.")
            elif reason.code == "not_modelled":
                print(f"  - Not counted: {', '.join(reason.detail['items'])}.")
            elif reason.code == "modules_not_usable":
                names = ", ".join(reason.detail["modules"])
                print(f"  - Left out, skills missing or Alpha-locked: {names}.")
            elif reason.code == "ship_not_usable":
                print(f"  - This character cannot fly a {reason.detail['ship']} yet.")

        if args.ore and estimate.m3_per_minute:
            config = load_config()
            ore_id = skills.find_type(db, args.ore)
            volume = db.execute(
                "SELECT volume FROM sde_types WHERE type_id = ?", (ore_id,)
            ).fetchone()[0]
            per_hour = int(estimate.m3_per_minute * 60 / volume)
            station_id = _resolve_station(db, character_id, args.station)
            with auth.http_client(config) as client:
                esi = EsiClient(client, db)
                result = ore.analyse(db, esi, character_id, ore_id, per_hour, station_id)
            print(f"One hour of mining {result.type_name}: about {per_hour:,} units. Best uses:")
            for option in [o for o in result.options if o.instant][:3]:
                print(
                    f"  {option.isk:>12,.0f} ISK  +{option.active_minutes:.0f} min  {option.title}"
                )
    print("This is an estimate from the static data; it improves once real sessions are recorded.")
    return 0


def _cmd_couriers(args) -> int:
    config = load_config()
    with store.connect() as db, auth.http_client(config) as client:
        character_id = goals.resolve_character(db, args.character)
        region_ids = None
        if args.region:
            region_ids = []
            for name in args.region:
                row = db.execute(
                    "SELECT region_id FROM sde_regions WHERE name = ? COLLATE NOCASE", (name,)
                ).fetchone()
                if row is None:
                    raise couriers.CourierError(f"No region called '{name}'.")
                region_ids.append(row["region_id"])
        result = couriers.find(
            db,
            EsiClient(client, db),
            character_id,
            region_ids=region_ids,
            highsec_only=not args.allow_lowsec,
            max_minutes=args.minutes,
            limit=args.limit,
            what_if_ship_id=skills.find_type(db, args.ship) if args.ship else None,
        )
    print(f"Courier contracts in {', '.join(result.regions)}, best ISK per minute first:")
    if not result.options:
        print("  None that fit right now.")
    for o in result.options:
        security = f"{o.lowest_security:.1f}" if o.lowest_security is not None else "?"
        print(
            f"  {o.reward:>12,.0f} ISK  {o.minutes:>4.0f} min  {o.isk_per_minute:>9,.0f}/min  "
            f"{o.start_system} -> {o.end_system} ({o.pickup_jumps}+{o.delivery_jumps} jumps, "
            f"lowest sec {security})"
        )
        whose = "a" if any(r.code == "what_if_ship" for r in o.reasons) else "your"
        print(
            f"{'':16}{o.volume_m3:,.0f} m3 in {whose} {o.ship.name}, collateral "
            f"{o.collateral:,.0f}, {o.days_to_complete} days to deliver"
        )
        for reason in o.reasons:
            if reason.code == "collateral_large_share":
                print(f"{'':16}warning: collateral is {reason.detail['share']:.0%} of the wallet")
    if result.excluded:
        skipped = ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in result.excluded.items())
        print(f"  Skipped: {skipped}.")
    print("  Travel time assumes about 1 minute per jump. Lawless highsec is not detected:")
    print("  check the route in game before accepting anything valuable.")
    return 0


CHARACTER_HELP = "character name or ID"
LOWSEC_HELP = "allow lowsec routes"

WHY_NOT = {
    "worse": "less ISK after expected losses",
    "too_long": "does not fit in the time",
    "red_risk": "red on the risk matrix (use --risky to include)",
    "muted": "muted",
    "new_limit": "only one new activity per plan",
    "teammate": "another character you're playing with does it",
    "hauling": "hauling for the other character makes more between you",
    "boosting": "boosting the other character makes more between you",
}


def _cmd_plan_together(args) -> int:
    config = load_config()
    secrets = default_store()
    with store.connect() as db, auth.http_client(config) as client:
        ids = [goals.resolve_character(db, name) for name in args.characters]
        esi = EsiClient(client, db, token_for=auth.AccessTokens(config, secrets, client).get)
        try:
            team = together.build(
                db, esi, ids, args.minutes, allow_lowsec=args.allow_lowsec,
                allow_risky=args.risky, ore_names=args.ore,
            )  # fmt: skip
        except together.TogetherError as exc:
            for reason in exc.reasons:
                print(explain.describe(reason.code, reason.detail), file=sys.stderr)
            return 1
        team = together.record(
            db, team, {"allow_lowsec": args.allow_lowsec, "risky": args.risky, "ore": args.ore}
        )
    shape = explain.SHAPE_NAMES[team.chosen.kind]
    print(f"Plan together {team.team_id}: {args.minutes:.0f} minutes, {shape}.")
    for member, plan, role in zip(team.members, team.chosen.plans, team.chosen.roles, strict=True):
        does = explain.ROLE_NAMES.get(role)
        print(f"  {member.name}{f' ({does})' if does else ''}, plan {plan.plan_id}:")
        if not plan.items:
            print("    Nothing fits right now.")
        for number, item in enumerate(plan.items, 1):
            print(f"    {number}. {item.title}")
            print(f"       {item.isk:,.0f} ISK, {item.minutes:.0f} min, risk {item.risk.rating}")
    for note in team.notes:
        print(f"  {explain.describe(note.code, note.detail)}")
    return 0


def _cmd_plan(args) -> int:
    config = load_config()
    secrets = default_store()
    with store.connect() as db, auth.http_client(config) as client:
        character_id = goals.resolve_character(db, args.character)
        esi = EsiClient(client, db, token_for=auth.AccessTokens(config, secrets, client).get)
        plan = planner.build_plan(
            db,
            esi,
            character_id,
            args.minutes,
            allow_lowsec=args.allow_lowsec,
            allow_risky=args.risky,
            ore_names=args.ore,
        )
        plan = planner.record(
            db, plan, {"allow_lowsec": args.allow_lowsec, "risky": args.risky, "ore": args.ore}
        )
        item_ids = [
            r["item_id"]
            for r in db.execute(
                "SELECT item_id FROM plan_items WHERE plan_id = ? AND position IS NOT NULL "
                "ORDER BY position",
                (plan.plan_id,),
            )
        ]
    print(f"Plan {plan.plan_id}: {args.minutes:.0f} minutes.")
    print(f"  Net worth about {plan.net_worth:,.0f} ISK.")
    if not plan.items:
        print("  Nothing fits right now.")
    for number, (item, item_id) in enumerate(zip(plan.items, item_ids, strict=True), 1):
        new = "  [new to you]" if item.new_to_you else ""
        print(f"  {number}. {item.title}{new}")
        print(
            f"     {item.isk:,.0f} ISK, {item.minutes:.0f} min, risk {item.risk.rating}"
            f" (expected loss {item.risk.expected_loss:,.0f})   [item {item_id}]"
        )
    print(f"  Total: {plan.total_isk:,.0f} ISK in {plan.total_minutes:.0f} min.")
    if plan.alternatives:
        print("  Also considered:")
        for alt in plan.alternatives[: args.alternatives]:
            print(
                f"    {alt.item.title}: {alt.item.isk:,.0f} ISK, {alt.item.minutes:.0f} min "
                f"({WHY_NOT.get(alt.why_not, alt.why_not)})"
            )
    for note in plan.notes:
        if note.code == "ores_assumed":
            ores = ", ".join(note.detail["ores"])
            print(f"  - No mining here on record; assumed common ores: {ores}.")
        elif note.code == "ores_from_mining_ledger":
            print("  - Ores chosen from what you mined in this system recently.")
        elif note.code == "loss_chances_are_guesses":
            print("  - Loss chances are starting guesses until kill data and history refine them.")
        elif note.code == "no_couriers_fit":
            print("  - No courier contracts fit your ships, wallet and time.")
        elif note.code == "selling_at_nearby_station":
            print("  - Not docked at an NPC station; selling prices are for the nearest one here.")
    print("  Record what you did: eve-assistant respond <item> do|not_now|mute")
    return 0


def _cmd_respond(args) -> int:
    with store.connect() as db:
        planner.respond(db, args.item_id, args.response)
    print("Recorded.")
    if args.response == "mute":
        print("The category is muted; undo with `eve-assistant unmute <category>`.")
    return 0


def _cmd_mutes(args) -> int:
    with store.connect() as db:
        if args.action == "list":
            active = preferences.active_mutes(db)
            print("Muted: " + (", ".join(sorted(active)) if active else "nothing"))
            print("Categories: " + ", ".join(preferences.KNOWN))
            return 0
        if args.category is None:
            raise preferences.PreferenceError("Name a category, e.g. kind:courier.")
        changed = (preferences.mute if args.action == "mute" else preferences.unmute)(
            db, args.category
        )
    print(f"{args.category}: {'done' if changed else 'no change'}.")
    return 0


def _cmd_projects(_args) -> int:
    with store.connect() as db:
        names = dict(db.execute("SELECT character_id, name FROM characters").fetchall())
        open_projects = projects.open_projects(db)
        play_time = pace.weekly_play(db)
    print(explain.describe(play_time.reason().code, play_time.reason().detail))
    print("Change your setting with: eve-assistant play-time <hours>")
    if not open_projects:
        print("No open projects.")
    for p in open_projects:
        deadline = f", due {p.deadline[:16].replace('T', ' ')} UTC" if p.deadline else ""
        print(
            f"[{p.project_id}] {p.title} ({names.get(p.character_id, '?')}): "
            f"{p.minutes_done:.0f}/{p.total_minutes:.0f} min, {p.isk:,.0f} ISK at the end{deadline}"
        )
    return 0


def _cmd_project_close(args) -> int:
    with store.connect() as db:
        projects.close(db, args.project_id, args.status)
    print(f"Project {args.project_id} marked {args.status}.")
    return 0


def _snapshot_for_session(character_id: int) -> list[str]:
    try:
        config = load_config()
    except ConfigError as exc:
        return [f"No snapshot: {exc}"]
    return refresh.snapshot_character(config, default_store(), character_id)


def _print_problems(problems: list[str]) -> None:
    for line in problems:
        print(f"  Snapshot problem: {line}")


def _cmd_session_start(args) -> int:
    with store.connect(store.DEFAULT_DB_PATH) as db:
        character_id = goals.resolve_character(db, args.character)
        plan_id = args.plan
        if plan_id is None and not args.no_plan:
            plan_id = planner.latest_plan_id(db, character_id)
    result = session_flow.begin(
        store.DEFAULT_DB_PATH, _snapshot_for_session, character_id=character_id, plan_id=plan_id
    )
    session = result.session
    following = f", following plan {plan_id}" if plan_id else ""
    print(f"Session {session.session_id} started for {session.character_name}{following}.")
    _print_problems(result.problems)
    print(f'When you stop: eve-assistant session finish "{session.character_name}"')
    return 0


def _cmd_session_end(args) -> int:
    with store.connect(store.DEFAULT_DB_PATH) as db:
        character_id = goals.resolve_character(db, args.character)
    session_id = session_flow.running_session_id(store.DEFAULT_DB_PATH, character_id)
    result = session_flow.end(
        store.DEFAULT_DB_PATH,
        _snapshot_for_session,
        session_id,
        how=args.how,
        minutes_played=getattr(args, "minutes", None),
    )
    ended = result.session
    print(f"Session {ended.session_id} {ended.how}: {ended.minutes():.0f} minutes.")
    _print_problems(result.problems)
    return 0


def _cmd_detected(args) -> int:
    with store.connect(store.DEFAULT_DB_PATH) as db:
        found = detection.suggestions(db)
    if args.action == "list":
        if not found:
            print("No sessions to record. Sessions are noticed while the dashboard runs.")
            return 0
        for number, d in enumerate(found, 1):
            plan = f", plan {d.plan_id}" if d.plan_id else ""
            print(
                f"{number}: {d.character_name}, {d.login} to {d.logout}, {d.minutes:.0f} min{plan}"
            )
        print("Record one with: eve-assistant detected record <number>")
        return 0
    if args.number is None or not 1 <= args.number <= len(found):
        raise sessions.SessionError("Give the number of a session from: eve-assistant detected")
    chosen = found[args.number - 1]
    if args.action == "record":
        session = session_flow.record_detected(
            store.DEFAULT_DB_PATH, chosen.character_id, chosen.login
        )
        print(f"Recorded as session {session.session_id}.")
    else:
        session_flow.dismiss_detected(store.DEFAULT_DB_PATH, chosen.character_id, chosen.login)
        print("Won't be offered again.")
    return 0


def _cmd_sessions(_args) -> int:
    with store.connect() as db:
        found = sessions.recent(db)
    if not found:
        print("No sessions yet. Start one with: eve-assistant session start <character>")
        return 0
    for s in found:
        state = "running" if s.is_open else s.how
        plan = f", plan {s.plan_id}" if s.plan_id else ""
        print(
            f"{s.session_id}: {s.character_name}, {s.started_at}, "
            f"{s.minutes():.0f} min, {state}{plan}"
        )
    return 0


def _cmd_outcomes(_args) -> int:
    with store.connect() as db:
        outcomes.update(db)
        found = outcomes.recent(db)
    if not found:
        print("No finished sessions yet.")
        return 0
    for session, outcome in found:
        print(f"Session {session.session_id}: {session.character_name}, {session.started_at}")
        if outcome is None:
            print("  Not worked out yet.")
            continue
        planned = (
            f"planned {outcome.planned_isk:,.0f} ISK in {outcome.planned_minutes:.0f} min"
            if outcome.planned_isk is not None
            else "no plan"
        )
        print(f"  Played {session.minutes():.0f} min; {planned}.")
        print(f"  Wallet change: {outcome.net_isk:+,.0f} ISK.")
        if outcome.details["mined"] is not None:
            value = outcome.mined_value
            print(
                f"  Ore mined: {outcome.details['mined_m3']:,.0f} m3"
                + (f", worth about {value:,.0f} ISK at buyers' prices after tax" if value else "")
                + "."
            )
        if (logs := outcome.log_mining) is not None:
            active = logs["m3_per_active_minute"]
            print(
                f"  From game logs: {logs['m3']:,.0f} m3 in {logs['cycles']} cycles"
                + (f", {active:,.0f} m3/min while mining" if active else "")
                + "."
            )
        for note in outcome.notes():
            print(f"  {note}")
    return 0


def _backup_folder():
    try:
        return load_config().backups_dir
    except ConfigError:
        return None


def _cmd_backup(_args) -> int:
    made = backups.make(store.DEFAULT_DB_PATH, _backup_folder())
    print(f"Backup saved: {made.path} ({made.size / 1e6:.1f} MB).")
    return 0


def _cmd_backups(_args) -> int:
    folder = _backup_folder() or backups.default_folder(store.DEFAULT_DB_PATH)
    found = backups.list_backups(folder)
    if not found:
        print(f"No backups yet in {folder}. Make one with: eve-assistant backup")
        return 0
    print(f"Backups in {folder}, newest first:")
    for b in found:
        print(f"  {b.path.name}  {b.size / 1e6:.1f} MB")
    return 0


def _cmd_restore(args) -> int:
    if not args.confirm:
        print(
            "This replaces the database with the backup. Your current database is moved aside, "
            "not deleted. Close the dashboard first, then run again with --confirm."
        )
        return 2
    aside = backups.restore(args.file, store.DEFAULT_DB_PATH)
    print(f"Restored {args.file}. The previous database is kept as {aside}.")
    print("Reload the static data next: eve-assistant sde-update")
    return 0


def _cmd_learned(_args) -> int:
    with store.connect() as db:
        rows = db.execute("SELECT character_id, name FROM characters ORDER BY name").fetchall()
        for row in rows:
            for correction in activities.learned(db, row["character_id"]):
                reason = correction.reason()
                print(f"{row['name']}: {explain.describe(reason.code, reason.detail)}")
        for reason in (pace.weekly_play(db).reason(), pace.finish_rate(db).reason()):
            print(explain.describe(reason.code, reason.detail))
    return 0


def _cmd_gamelogs(_args) -> int:
    config = load_config()
    if config.gamelogs_dir is None:
        print("No game log folder set. Add [logs] gamelogs_dir to config/local.toml.")
        return 2
    with store.connect() as db:
        problems = refresh.read_game_logs(config, db)
        if problems:
            raise ConfigError(problems[0])
        report = gamelogs.import_folder(db, config.gamelogs_dir)
        total = db.execute("SELECT COUNT(*) FROM mining_cycles").fetchone()[0]
    print(
        f"Read {report.files_read} log files: {report.cycles_added} new mining lines "
        f"({total} in all). Skipped {report.skipped_files} files from other characters."
    )
    return 0


def _cmd_play_time(args) -> int:
    with store.connect() as db:
        projects.set_weekly_minutes(db, args.hours * 60)
    print(f"Play time set to {args.hours:g} hours a week.")
    return 0


def _cmd_accounts(_args) -> int:
    with store.connect() as db:
        views = accounts.overview(db)
    if not views:
        print("No characters yet. Run `eve-assistant snapshot` first.")
        return 0
    for view in views:
        print(f"{view.label} [{view.clone_state}]: {', '.join(view.characters)}")
    return 0


def _cmd_account_assign(args) -> int:
    with store.connect() as db:
        accounts.assign(db, args.character_id, args.label)
    print(f"Character {args.character_id} is now on account '{args.label}'.")
    return 0


def _cmd_account_state(args) -> int:
    with store.connect() as db:
        accounts.set_clone_state(db, args.label, args.state)
    print(f"Account '{args.label}' is now marked {args.state}.")
    return 0


def _cmd_alerts_setup(_args, ask=None) -> int:
    """Enter the Discord webhook and ntfy topic at a hidden prompt (never echoed)."""
    import getpass

    ask = ask or getpass.getpass
    secrets = default_store()
    saved = alerts.addresses(secrets)
    print("Alerts: in-game actions go to Discord, the tool's health to ntfy. Both are optional.")
    print("Press Enter to keep what's saved, or type - to remove it. Nothing you type is shown.")
    for channel, what in (("discord", "Discord webhook address"), ("ntfy", "ntfy topic address")):
        state = "saved" if channel in saved else "not set"
        answer = ask(f"{what} ({state}): ").strip()
        if not answer:
            continue
        try:
            alerts.save_address(secrets, channel, None if answer == "-" else answer)
        except alerts.AlertError as exc:
            print(f"Not saved: {exc}")
            return 1
        print(f"{what}: {'removed' if answer == '-' else 'saved'}.")
    return 0


def _cmd_alerts_test(_args) -> int:
    """Send a test alert to each channel that has an address."""
    config = load_config()
    secrets = default_store()
    saved = alerts.addresses(secrets)
    if not saved:
        print("No alert channels yet. Run: eve-assistant alerts-setup")
        return 1
    db = store.connect()
    try:
        tests = [
            alerts.Alert(f"test:{channel}:{store.utc_now()}", channel,
                         "Test alert from EVE Assistant: this channel works.")
            for channel in saved
        ]  # fmt: skip
        with auth.http_client(config) as http:
            sent, problems = alerts.send(db, secrets, http, tests)
    finally:
        db.close()
    for problem in problems:
        print(problem)
    print(f"Sent {sent} test alert{'s' if sent != 1 else ''}.")
    return 1 if problems else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="eve-assistant", description="EVE Assistant")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("login", help="log a character in through EVE's login page").set_defaults(
        func=_cmd_login
    )

    characters = commands.add_parser("characters", help="list logged-in characters")
    characters.add_argument(
        "--check", action="store_true", help="also check each character's token with EVE"
    )
    characters.set_defaults(func=_cmd_characters)

    commands.add_parser(
        "alerts-setup", help="enter the Discord webhook and ntfy topic for alerts"
    ).set_defaults(func=_cmd_alerts_setup)
    commands.add_parser("alerts-test", help="send a test alert to each channel").set_defaults(
        func=_cmd_alerts_test
    )

    logout = commands.add_parser("logout", help="forget a character's token on this PC")
    logout.add_argument("character_id", type=int)
    logout.set_defaults(func=_cmd_logout)

    sde_update = commands.add_parser("sde-update", help="download and load CCP's static data")
    sde_update.add_argument("--force", action="store_true", help="reload even if current")
    sde_update.set_defaults(func=_cmd_sde_update)

    commands.add_parser(
        "snapshot", help="record the current state of every logged-in character"
    ).set_defaults(func=_cmd_snapshot)

    commands.add_parser("accounts", help="show characters grouped by account").set_defaults(
        func=_cmd_accounts
    )
    account = commands.add_parser("account", help="set up accounts").add_subparsers(
        dest="account_command", required=True
    )
    assign = account.add_parser("assign", help="put a character on an account")
    assign.add_argument("character_id", type=int)
    assign.add_argument("label", help="any name for the account, e.g. main")
    assign.set_defaults(func=_cmd_account_assign)
    state = account.add_parser("state", help="mark an account Alpha or Omega")
    state.add_argument("label")
    state.add_argument("state", choices=accounts.CLONE_STATES)
    state.set_defaults(func=_cmd_account_state)

    skills_cmd = commands.add_parser("skills", help="what a character needs to use an item")
    skills_cmd.add_argument("character", help=CHARACTER_HELP)
    skills_cmd.add_argument("item", help='item name, e.g. "Retriever"')
    skills_cmd.set_defaults(func=_cmd_skills)

    ore_cmd = commands.add_parser("ore", help="compare selling, refining and hauling ore")
    ore_cmd.add_argument("character", help=CHARACTER_HELP)
    ore_cmd.add_argument("ore", help='ore name, e.g. "Veldspar"')
    ore_cmd.add_argument("quantity", type=int)
    ore_cmd.add_argument("--station", help="hub name or station ID (default: where docked)")
    ore_cmd.add_argument("--allow-lowsec", action="store_true", help=LOWSEC_HELP)
    ore_cmd.set_defaults(func=_cmd_ore)

    mining_cmd = commands.add_parser("mining", help="estimate ore mining yield")
    mining_cmd.add_argument("character", help=CHARACTER_HELP)
    mining_cmd.add_argument(
        "--ship", help='compare a ship instead of the current one, e.g. "Venture"'
    )
    mining_cmd.add_argument(
        "--module", action="append", help='a fitted module (repeat), e.g. --module "Miner I"'
    )
    mining_cmd.add_argument("--ore", help="also value an hour of mining this ore")
    mining_cmd.add_argument("--station", help="where the ore would be sold (default: docked)")
    mining_cmd.set_defaults(func=_cmd_mining)

    couriers_cmd = commands.add_parser("couriers", help="find courier contracts worth taking")
    couriers_cmd.add_argument("character", help=CHARACTER_HELP)
    couriers_cmd.add_argument("--region", action="append", help="region name (repeat)")
    couriers_cmd.add_argument(
        "--minutes", type=float, help="only jobs that fit in this many minutes"
    )
    couriers_cmd.add_argument("--allow-lowsec", action="store_true", help=LOWSEC_HELP)
    couriers_cmd.add_argument("--limit", type=int, default=10)
    couriers_cmd.add_argument("--ship", help='plan as if you had this ship, e.g. "Tayra"')
    couriers_cmd.set_defaults(func=_cmd_couriers)

    plan_cmd = commands.add_parser("plan", help="plan what to do with the time you have")
    plan_cmd.add_argument("character", help=CHARACTER_HELP)
    plan_cmd.add_argument("minutes", type=float, help="minutes you have to play")
    plan_cmd.add_argument("--allow-lowsec", action="store_true", help=LOWSEC_HELP)
    plan_cmd.add_argument("--risky", action="store_true", help="include red-rated options")
    plan_cmd.add_argument("--ore", action="append", help="ore to consider mining (repeat)")
    plan_cmd.add_argument("--alternatives", type=int, default=5, help="how many to list")
    plan_cmd.set_defaults(func=_cmd_plan)

    together_cmd = commands.add_parser(
        "plan-together", help="plan characters on different Omega accounts played at once"
    )
    together_cmd.add_argument("minutes", type=float, help="minutes you have to play")
    together_cmd.add_argument("characters", nargs="+", help="two or more characters")
    together_cmd.add_argument("--allow-lowsec", action="store_true", help=LOWSEC_HELP)
    together_cmd.add_argument("--risky", action="store_true", help="include red-rated options")
    together_cmd.add_argument("--ore", action="append", help="ore to consider mining (repeat)")
    together_cmd.set_defaults(func=_cmd_plan_together)

    respond = commands.add_parser("respond", help="record what you did with a plan item")
    respond.add_argument("item_id", type=int)
    respond.add_argument("response", choices=("do", "not_now", "mute"))
    respond.set_defaults(func=_cmd_respond)

    for action in ("mute", "unmute", "list"):
        name = "mutes" if action == "list" else action
        mutes_cmd = commands.add_parser(name, help=f"{action} planner categories")
        mutes_cmd.add_argument("category", nargs="?", help="e.g. kind:courier or route:lowsec")
        mutes_cmd.set_defaults(func=_cmd_mutes, action=action)

    commands.add_parser("projects", help="list open projects").set_defaults(func=_cmd_projects)
    project_cmd = commands.add_parser("project", help="close a project").add_subparsers(
        dest="project_command", required=True
    )
    for status in ("done", "dropped"):
        close_cmd = project_cmd.add_parser(status, help=f"mark a project {status}")
        close_cmd.add_argument("project_id", type=int)
        close_cmd.set_defaults(func=_cmd_project_close, status=status)
    play_time = commands.add_parser("play-time", help="set your usual play time per week")
    play_time.add_argument("hours", type=float)
    play_time.set_defaults(func=_cmd_play_time)

    commands.add_parser("sessions", help="list recent play sessions").set_defaults(
        func=_cmd_sessions
    )
    commands.add_parser(
        "outcomes", help="what happened in recent sessions, against the plan"
    ).set_defaults(func=_cmd_outcomes)
    commands.add_parser(
        "gamelogs", help="read mining cycles from EVE's game logs now"
    ).set_defaults(func=_cmd_gamelogs)
    commands.add_parser(
        "learned", help="what the planner has learned from your sessions"
    ).set_defaults(func=_cmd_learned)
    commands.add_parser("backup", help="back up the database now").set_defaults(func=_cmd_backup)
    commands.add_parser("backups", help="list database backups").set_defaults(func=_cmd_backups)
    restore_cmd = commands.add_parser("restore", help="put a backup in place of the database")
    restore_cmd.add_argument("file", help="the backup file")
    restore_cmd.add_argument("--confirm", action="store_true", help="really do it")
    restore_cmd.set_defaults(func=_cmd_restore)
    detected_cmd = commands.add_parser(
        "detected", help="sessions noticed from EVE's login times, to record or dismiss"
    )
    detected_cmd.add_argument(
        "action", nargs="?", default="list", choices=("list", "record", "dismiss")
    )
    detected_cmd.add_argument("number", nargs="?", type=int)
    detected_cmd.set_defaults(func=_cmd_detected)
    session_cmd = commands.add_parser("session", help="start or end a play session").add_subparsers(
        dest="session_command", required=True
    )
    start_cmd = session_cmd.add_parser("start", help="start playing (takes a snapshot)")
    start_cmd.add_argument("character", help=CHARACTER_HELP)
    start_cmd.add_argument("--plan", type=int, help="plan to follow (default: the latest)")
    start_cmd.add_argument("--no-plan", action="store_true", help="play without a plan")
    start_cmd.set_defaults(func=_cmd_session_start)
    finish_cmd = session_cmd.add_parser("finish", help="stop playing (takes a snapshot)")
    finish_cmd.add_argument("character", help=CHARACTER_HELP)
    finish_cmd.add_argument(
        "--minutes", type=float, help="minutes actually played, if you stopped earlier"
    )
    finish_cmd.set_defaults(func=_cmd_session_end, how="finished")
    cancel_cmd = session_cmd.add_parser("cancel", help="discard a session you didn't play")
    cancel_cmd.add_argument("character", help=CHARACTER_HELP)
    cancel_cmd.set_defaults(func=_cmd_session_end, how="cancelled")

    serve = commands.add_parser("serve", help="run the dashboard on this PC")
    serve.add_argument("--port", type=int, default=launcher.DEFAULT_PORT)
    serve.add_argument(
        "--open", action="store_true", help="open it in the browser (only that, if it's running)"
    )
    serve.add_argument(
        "--hosted", action="store_true", help="run as the hosted copy, behind a proxy's sign-in"
    )
    serve.set_defaults(func=_cmd_serve)

    send_logs = commands.add_parser(
        "send-logs", help="send this PC's new mining lines to the hosted copy"
    )
    send_logs.add_argument("--setup", action="store_true", help="enter the hosted copy's token")
    send_logs.add_argument(
        "--watch", type=float, metavar="MINUTES", help="keep sending every MINUTES until stopped"
    )
    send_logs.set_defaults(func=_cmd_send_logs)
    shortcut = commands.add_parser(
        "shortcut", help="put a shortcut that starts the dashboard on the desktop (Windows)"
    )
    shortcut.add_argument("--folder", type=Path, help="another folder to put it in")
    shortcut.add_argument("--port", type=int, default=launcher.DEFAULT_PORT)
    shortcut.set_defaults(func=_cmd_shortcut)

    commands.add_parser("goals", help="show goals and how far away they are").set_defaults(
        func=_cmd_goals
    )
    goal = commands.add_parser("goal", help="add or close goals").add_subparsers(
        dest="goal_command", required=True
    )
    add = goal.add_parser("add", help="add a goal")
    add.add_argument("kind", choices=("ship", "item", "skill", "isk"))
    add.add_argument("target", help="ship, item or skill name, or an ISK amount")
    add.add_argument("level", nargs="?", type=int, help="skill level (skill goals)")
    add.add_argument("--character", help=CHARACTER_HELP)
    add.add_argument("--after", type=int, metavar="GOAL", help="follow this goal (its ID)")
    add.add_argument(
        "--fit", help="ship goals: a saved fit's name, or a file with a fit exported as EFT"
    )
    add.set_defaults(func=_cmd_goal_add)
    for status in ("done", "dropped"):
        close = goal.add_parser(status, help=f"mark a goal {status}")
        close.add_argument("goal_id", type=int)
        close.set_defaults(func=_cmd_goal_status, status=status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (
        ConfigError,
        SSOError,
        SecretStoreError,
        EsiError,
        SdeError,
        AccountError,
        SkillsError,
        GoalError,
        fittings.FitError,
        ore.OreError,
        mining.MiningError,
        couriers.CourierError,
        planner.PlanError,
        preferences.PreferenceError,
        projects.ProjectError,
        sessions.SessionError,
        backups.BackupError,
        launcher.LauncherError,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
