"""The browser session the upstream ``linkedin_cli`` verbs run against.

One persistent Chromium profile on the data volume holds the LinkedIn login. Chromium
runs headed on the Xvfb display the entrypoint starts; LinkedIn treats headless browsers
as bots. The read-only network guard is attached before the first page opens.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import guard
from .config import LOCALE, TIMEZONE
from .pacing import Pacer

logger = logging.getLogger(__name__)

FEED_URL = "https://www.linkedin.com/feed/"
LOGIN_URL = "https://www.linkedin.com/login"

# Phrases of LinkedIn's interstitials that do not live under /checkpoint. Kept strict:
# a false hit halts the scout for a rest day, which is the safe side.
WARNING_PHRASES = (
    "let's do a quick security check",
    "quick security check",
    "your account has been restricted",
    "we've restricted your account",
    "your account is temporarily restricted",
    "you've reached the weekly invitation limit",
    "verify you're a human",
)


class WarningPage(RuntimeError):
    """A checkpoint, captcha, login wall or warning page: stop and rest."""

    def __init__(self, reason: str, url: str) -> None:
        self.reason = reason
        self.url = url
        super().__init__(f"{reason} at {url}")


def classify(url: str, text: str) -> str | None:
    """Return a halt reason for this page, or None when it is an ordinary page."""
    parts = urlsplit(url)
    path = parts.path.lower()
    if "captcha" in url.lower():
        return "captcha"
    if path.startswith("/checkpoint"):
        return "checkpoint"
    if path.startswith(("/login", "/uas/login", "/authwall", "/signup")):
        return "logged_out"
    lowered = text.lower()
    for phrase in WARNING_PHRASES:
        if phrase in lowered:
            return "warning_page"
    return None


def _monotonic() -> float:
    return time.monotonic()


def wait_for_feed(page: Any, timeout_s: float, poll_ms: int = 5000) -> bool:
    """Wait until a human login reaches the feed; False on timeout.

    Waits with ``page.wait_for_timeout``, never ``time.sleep``: the network guard is a
    route on every request, and sync Playwright dispatches route handlers only inside a
    Playwright call. A plain sleep leaves the login submit pending forever.
    """
    deadline = _monotonic() + timeout_s
    while _monotonic() < deadline:
        url = page.url
        if "/feed" in url and classify(url, "") is None:
            return True
        page.wait_for_timeout(poll_ms)
    return False


class ScoutSession:
    """Implements ``linkedin_cli.session.LinkedInSession`` with human pacing."""

    def __init__(self, profile_dir: str | Path, pacer: Pacer, *, read_only: bool = True,
                 storage_state_path: str | Path | None = None, on_block: Any = None) -> None:
        self.profile_dir = Path(profile_dir)
        self.pacer = pacer
        self.read_only = read_only
        self.storage_state_path = Path(storage_state_path) if storage_state_path else None
        self.on_block = on_block
        self.page: Any = None
        self.context: Any = None
        self.network_guard: guard.NetworkGuard | None = None
        self._pw: Any = None
        self._self_profile: dict | None = None
        # upstream login form fields; never set: the scout does not type credentials
        self.username = None
        self.password = None

    # -- LinkedInSession -------------------------------------------------------
    def ensure_browser(self) -> None:
        if self.page is not None and not self.page.is_closed():
            return
        from playwright.sync_api import sync_playwright
        from playwright_stealth import Stealth

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        self.context = self._pw.chromium.launch_persistent_context(
            str(self.profile_dir),
            headless=False,
            slow_mo=200,
            locale=LOCALE,
            timezone_id=str(TIMEZONE),
            viewport={"width": 1440, "height": 900},
        )
        self.context.set_default_timeout(30_000)
        self.context.set_default_navigation_timeout(30_000)
        Stealth().apply_stealth_sync(self.context)
        if self.read_only:
            self.network_guard = guard.NetworkGuard(on_block=self.on_block)
            self.network_guard.attach(self.context)
        self._import_storage_state()
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()

    @property
    def self_profile(self) -> dict:
        if self._self_profile is None:
            from linkedin_cli.setup.self_profile import discover_self_profile

            self._self_profile = discover_self_profile(self)
        return self._self_profile

    def wait(self, min_delay: float | None = None, max_delay: float | None = None) -> None:
        # upstream asks for 0.5-2 s pauses in places; the pacer's 20-120 s always applies
        self.pause()
        if self.page is not None:
            self.page.wait_for_load_state("domcontentloaded")

    def pause(self) -> None:
        """The pacer delay, waited inside Playwright so the network guard keeps dispatching."""
        page = self.page
        if page is not None and not page.is_closed():
            self.pacer.delay(lambda s: page.wait_for_timeout(s * 1000))
        else:
            self.pacer.delay()

    def close(self) -> None:
        try:
            if self.context is not None:
                self.context.close()
            if self._pw is not None:
                self._pw.stop()
        finally:
            self.page = self.context = self._pw = None
            self._self_profile = None

    # -- scout helpers ----------------------------------------------------------
    def check(self) -> None:
        """Raise WarningPage when the live page is a checkpoint, captcha, login or warning."""
        if self.page is None:
            return
        try:
            text = self.page.locator("body").inner_text(timeout=3000)[:20000]
        except Exception:  # noqa: BLE001 - a page without body text is not a warning
            text = ""
        reason = classify(self.page.url, text)
        if reason:
            raise WarningPage(reason, self.page.url)

    def goto(self, url: str) -> None:
        self.ensure_browser()
        self.page.goto(url, wait_until="domcontentloaded")
        self.wait()
        self.check()

    def has_login_cookie(self) -> bool:
        return any(c.get("name") == "li_at" for c in self.context.cookies("https://www.linkedin.com"))

    def _import_storage_state(self) -> None:
        """Seed the profile from a Playwright storage state Secret when it has no login yet."""
        path = self.storage_state_path
        if not path or not path.is_file() or self.has_login_cookie():
            return
        state = json.loads(path.read_text())
        cookies = [c for c in state.get("cookies", []) if "linkedin.com" in c.get("domain", "")]
        if cookies:
            self.context.add_cookies(cookies)
            logger.info("seeded browser profile with %d LinkedIn cookies from %s", len(cookies), path)
