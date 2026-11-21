"""One scout session and the long-running loop around it.

A session: open the feed, run each playbook rule (people searches, then watched posts),
turn profiles and engagers into signal records. Every page view asks the pacer first.
A checkpoint, captcha, login wall or warning page halts the scout, alerts, and starts a
rest day; only ``oo-scout resume`` after that rest lets it run again.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import guard
from .browser import FEED_URL, WarningPage
from .pacing import PAGE_VIEW, Halted, Pacer, PacingRefused, SessionOver
from .playbook import Playbook, PlaybookError, Rule
from .signals import Signal, engager_signal, profile_signal, profile_url

logger = logging.getLogger(__name__)


@dataclass
class Stats:
    page_views: int = 0
    signals: int = 0
    skipped: int = 0
    stopped: str = ""
    errors: list[str] = field(default_factory=list)


class Upstream:
    """The upstream read verbs, behind one seam so tests can replace them."""

    def search_people(self, session: Any, keyword: str, network: Any = None) -> dict:
        from linkedin_cli.actions.search import search_people

        return search_people(session, keyword, network=network)

    def scrape_profile(self, session: Any, profile: dict) -> tuple[dict | None, Any]:
        from linkedin_cli.actions.profile import scrape_profile

        return scrape_profile(session, profile)

    def extract_in_urls(self, page: Any) -> list[str]:
        """Profile links in the page's ``main`` column: not the top bar or the side rail."""
        from linkedin_cli.browser.nav import extract_in_urls

        main = page.locator("main")
        if main.count() == 1:
            return extract_in_urls(_Scoped(page, main))
        return extract_in_urls(page)


class _Scoped:
    def __init__(self, page: Any, root: Any) -> None:
        self.page, self.root = page, root

    @property
    def url(self) -> str:
        return self.page.url

    def locator(self, selector: str) -> Any:
        return self.root.locator(selector)


def _skippable() -> tuple[type[BaseException], ...]:
    from linkedin_cli.exceptions import ProfileInaccessibleError, SkipProfile

    return (SkipProfile, ProfileInaccessibleError)


class Scout:
    def __init__(self, session: Any, pacer: Pacer, sink: Any, alert: Callable[[str], None],
                 upstream: Upstream | None = None) -> None:
        self.session = session
        self.pacer = pacer
        self.sink = sink
        self.alert = alert
        self.up = upstream or Upstream()
        self.self_url = ""

    def _view(self, stats: Stats) -> None:
        self.pacer.acquire(PAGE_VIEW)
        stats.page_views += 1

    def _emit(self, signals: list[Signal], stats: Stats) -> None:
        if signals:
            stats.signals += self.sink.write(signals)

    def run_rule(self, rule: Rule, stats: Stats) -> None:
        ledger = self.pacer.ledger
        for keyword in rule.people_keywords:
            self._view(stats)
            result = self.up.search_people(self.session, keyword, network=list(rule.network) or None)
            self.session.check()
            for found in result.get("profiles", [])[: rule.max_profiles_per_search]:
                url = profile_url(found.get("url", ""))
                key = f"{rule.id}|{url}"
                if not url or url == self.self_url or ledger.seen(key):
                    stats.skipped += 1
                    continue
                self._view(stats)
                try:
                    profile, _raw = self.up.scrape_profile(self.session, found)
                except _skippable() as exc:
                    logger.info("skip %s: %s", url, exc)
                    stats.skipped += 1
                    continue
                self.session.check()
                if profile:
                    self._emit([profile_signal(profile, rule.id)], stats)
                    ledger.mark_seen(key)
                self.session.pause()
        for post in rule.watch_posts:
            self._view(stats)
            self.session.goto(post)
            engagers, actors = [], set()
            for u in self.up.extract_in_urls(self.session.page):
                actor = profile_url(u)
                if actor and actor != self.self_url and actor not in actors \
                        and not ledger.seen(f"{rule.id}|{post}|{actor}"):
                    actors.add(actor)
                    engagers.append(engager_signal(post, actor, rule.id))
            self._emit(engagers, stats)
            for s in engagers:
                ledger.mark_seen(f"{rule.id}|{post}|{s.actor_key}")
            self.session.pause()

    def run_session(self, playbook: Playbook, extra: Callable[[], None] | None = None) -> Stats:
        """Run one session; returns its stats. Halts on a warning page."""
        from linkedin_cli.exceptions import AuthenticationError, CheckpointChallengeError

        stats = Stats()
        self.pacer.check(PAGE_VIEW)  # before the browser opens
        self.pacer.start_session()
        try:
            self._view(stats)
            self.session.goto(FEED_URL)
            try:
                self.self_url = profile_url(self.session.self_profile.get("url", ""))
            except AuthenticationError:
                raise WarningPage("logged_out", self.session.page.url) from None
            for rule in playbook.rules:
                self.run_rule(rule, stats)
            if extra:
                extra()
            stats.stopped = "done"
        except (SessionOver, Halted) as exc:
            stats.stopped = exc.reason
        except PacingRefused as exc:
            stats.stopped = exc.reason
        except WarningPage as exc:
            self._halt(exc.reason, exc.url, stats)
        except CheckpointChallengeError as exc:
            self._halt("checkpoint", exc.url, stats)
        except AuthenticationError as exc:
            self._halt("logged_out", getattr(self.session.page, "url", ""), stats, str(exc))
        except guard.ReadOnlyViolation as exc:
            self._halt("read_only_violation", getattr(self.session.page, "url", ""), stats, str(exc))
        finally:
            self.pacer.end_session()
            self.session.close()
        logger.info("session ended: %s", stats)
        return stats

    def _halt(self, reason: str, url: str, stats: Stats, detail: str = "") -> None:
        rest_until = self.pacer.halt(reason, url)
        stats.stopped = reason
        self.alert(
            f"stopped on {reason} ({url}) {detail}".strip()
            + f". Rest until {rest_until:%Y-%m-%d %H:%M} UTC; then check the account and run 'oo-scout resume'."
        )


def run_forever(make_scout: Callable[[], Scout], pacer: Pacer, load_playbook: Callable[[], Playbook],
                alert: Callable[[str], None], extra: Callable[[Scout], Callable[[], None] | None] | None = None,
                sleep: Callable[[float], None] = time.sleep, max_sessions: int | None = None) -> None:
    last_good: Playbook | None = None
    sessions = 0
    while max_sessions is None or sessions < max_sessions:
        try:
            pacer.check(None)
        except Halted as exc:
            logger.warning("not running: %s", exc)
            sleep(900)
            continue
        except PacingRefused:
            wait = max(60.0, (pacer.next_window_start() - pacer.clock()).total_seconds())
            wait += pacer.rng.uniform(0, 20 * 60)  # do not start on the minute
            logger.info("out of hours, sleeping %.0f s", wait)
            sleep(wait)
            continue
        try:
            last_good = load_playbook()
        except PlaybookError as exc:
            alert(f"playbook invalid, {'keeping last valid version' if last_good else 'nothing to run'}: {exc}")
        if last_good is None:
            sleep(3600)
            continue
        scout = make_scout()
        try:
            scout.run_session(last_good, extra(scout) if extra else None)
        except PacingRefused as exc:
            logger.info("session not started: %s", exc)
        except Exception as exc:  # noqa: BLE001 - keep the loop alive, never retry fast
            logger.exception("session failed")
            alert(f"session failed: {type(exc).__name__}: {exc}")
        sessions += 1
        if max_sessions is None or sessions < max_sessions:
            sleep(pacer.session_pause_s())
