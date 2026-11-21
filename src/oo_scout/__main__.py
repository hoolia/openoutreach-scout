"""oo-scout: run, bootstrap the LinkedIn login, resume after a halt, show status."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
from datetime import datetime
from pathlib import Path

from . import __version__, guard
from .alert import Alerter
from .config import TIMEZONE, ConfigError, Settings
from .pacing import CONNECT, MESSAGE, PAGE_VIEW, Ledger, Pacer

logger = logging.getLogger("oo_scout")


def _ledger(settings: Settings) -> Ledger:
    return Ledger(Path(settings.data_dir) / "ledger.json")


def _profile_dir(settings: Settings) -> Path:
    return Path(settings.data_dir) / "browser-profile"


def cmd_check_config(settings: Settings) -> int:
    from .playbook import load

    pb = load(settings.playbook_path)
    print(json.dumps({
        "version": __version__,
        "read_only": settings.read_only,
        "send_enabled": settings.send_enabled,
        "may_send": settings.may_send,
        "pacing": {k: (sorted(v) if isinstance(v, frozenset) else str(v))
                   for k, v in settings.pacing.__dict__.items()},
        "rules": [r.id for r in pb.rules],
        "sink": "clickhouse" if settings.clickhouse_url else "jsonl",
    }, indent=2))
    return 0


def cmd_status(settings: Settings) -> int:
    ledger = _ledger(settings)
    pacer = Pacer(settings.pacing, ledger)
    day = datetime.combine(pacer.local().date(), datetime.min.time(), TIMEZONE)
    counts = {k: sum(1 for t in ledger.events(k) if t >= day) for k in (PAGE_VIEW, CONNECT, MESSAGE)}
    print(json.dumps({"in_window": pacer.in_window(), "today": counts, "halt": ledger.halt,
                      "sent_notes": len(ledger.data.get("sent_notes", []))}, indent=2))
    return 0


def cmd_resume(settings: Settings) -> int:
    ledger = _ledger(settings)
    if not ledger.halt:
        print("not halted")
        return 0
    ledger.clear_halt()
    print(f"halt cleared; the scout runs again after {ledger.halt['rest_until']}")
    return 0


def cmd_bootstrap(settings: Settings, timeout_min: int, stay: bool) -> int:
    """Open the login page for a human on the VNC display and wait for the feed.

    The scout never types credentials. The account owner logs in by hand, second factor included; the
    login then lives in the browser profile on the data volume.
    """
    from .browser import LOGIN_URL, ScoutSession, wait_for_feed

    guard.install()
    pacer = Pacer(settings.pacing, _ledger(settings), sleep=lambda _s: None)
    session = ScoutSession(_profile_dir(settings), pacer, read_only=True,
                           storage_state_path=settings.storage_state_path)
    session.ensure_browser()
    session.page.goto(LOGIN_URL)
    print("Log in to LinkedIn in the VNC window (localhost:5900). Waiting for the feed...", flush=True)
    try:
        if wait_for_feed(session.page, 60 * timeout_min):
            print("Logged in; the session is stored in the browser profile. "
                  "Set mode back to run.", flush=True)
        else:
            print("timed out waiting for the feed", file=sys.stderr)
            return 1
    finally:
        session.close()
    if stay:
        # a Deployment would restart a finished container and reopen the login page
        signal.pause()
    return 0


def cmd_run(settings: Settings, once: bool) -> int:
    from .browser import ScoutSession
    from .clients import Chatwoot, Odoo
    from .playbook import load
    from .scout import Scout, run_forever
    from .send import SendContract
    from .signals import ClickHouseSink, JsonlSink

    guard.install()
    alert = Alerter(settings.slack_webhook_url)
    ledger = _ledger(settings)
    pacer = Pacer(settings.pacing, ledger)
    if settings.clickhouse_url:
        sink = ClickHouseSink(settings.clickhouse_url, settings.clickhouse_user,
                              settings.clickhouse_password, settings.clickhouse_table)
    else:
        sink = JsonlSink(Path(settings.data_dir) / "signals.jsonl")
        logger.warning("CLICKHOUSE_URL is empty: signals go to %s", sink.path)

    def make_scout() -> Scout:
        session = ScoutSession(
            _profile_dir(settings), pacer, read_only=not settings.may_send,
            storage_state_path=settings.storage_state_path,
            on_block=lambda m, u: alert(f"blocked a LinkedIn write in read-only mode: {m} {u}"),
        )
        return Scout(session, pacer, sink, alert)

    def extra(scout: Scout):
        if not settings.may_send:
            return None
        contract = SendContract(
            settings,
            Chatwoot(settings.chatwoot_url, settings.chatwoot_account_id, settings.chatwoot_api_token),
            Odoo(settings.odoo_url, settings.odoo_db, settings.odoo_user, settings.odoo_password),
            pacer, scout.session,
        )
        return contract.run_once

    test_run = settings.pacing.test_run
    logger.info("oo-scout %s: read_only=%s send_enabled=%s test_run=%s", __version__,
                settings.read_only, settings.send_enabled, test_run)
    if test_run:
        logger.warning("test run: working window ignored; at most %d page views, one session of at most %d min",
                       settings.pacing.page_views_per_day, settings.pacing.session_max_min)
    # a test run is one session
    run_forever(make_scout, pacer, lambda: load(settings.playbook_path), alert, extra,
                max_sessions=1 if (once or test_run) else None)
    if test_run and not once:
        logger.info("test run finished; idling until the Deployment is suspended")
        # a Deployment would restart a finished container into a second session
        signal.pause()
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="oo-scout", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="scout in paced sessions inside the working window")
    r.add_argument("--once", action="store_true", help="one session, then exit")
    b = sub.add_parser("bootstrap", help="wait for a human LinkedIn login on the VNC display")
    b.add_argument("--timeout-min", type=int, default=30)
    b.add_argument("--exit", action="store_true", help="exit after the login instead of idling")
    sub.add_parser("resume", help="clear a halt; the rest day still applies")
    sub.add_parser("status", help="caps used today and halt state")
    sub.add_parser("check-config", help="validate settings and playbook")
    args = p.parse_args(argv)
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    if args.cmd == "run":
        return cmd_run(settings, args.once)
    if args.cmd == "bootstrap":
        return cmd_bootstrap(settings, args.timeout_min, not args.exit)
    if args.cmd == "resume":
        return cmd_resume(settings)
    if args.cmd == "status":
        return cmd_status(settings)
    from .playbook import PlaybookError

    try:
        return cmd_check_config(settings)
    except PlaybookError as exc:
        print(f"playbook error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
