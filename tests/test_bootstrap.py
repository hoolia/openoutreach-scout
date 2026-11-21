"""The bootstrap wait must keep Playwright's event loop running.

The read-only network guard is a route on every request. Sync Playwright runs route
handlers only while Python is inside a Playwright call, so a wait built on time.sleep
leaves every request after the first page load pending: the login form never submits.
"""

from __future__ import annotations

import time

from oo_scout.browser import wait_for_feed


class FakePage:
    def __init__(self, urls: list[str]) -> None:
        self._urls = urls
        self.waits: list[int] = []

    @property
    def url(self) -> str:
        return self._urls[min(len(self.waits), len(self._urls) - 1)]

    def wait_for_timeout(self, ms: int) -> None:
        self.waits.append(ms)


def test_wait_for_feed_waits_inside_playwright(monkeypatch):
    def no_sleep(_s):
        raise AssertionError("time.sleep blocks Playwright route dispatch")

    monkeypatch.setattr(time, "sleep", no_sleep)
    page = FakePage(["https://www.linkedin.com/login", "https://www.linkedin.com/checkpoint/lg/login-submit",
                     "https://www.linkedin.com/feed/"])
    assert wait_for_feed(page, timeout_s=60, poll_ms=5000) is True
    assert page.waits == [5000, 5000]


def test_wait_for_feed_times_out(monkeypatch):
    clock = iter(range(0, 1000, 5))
    monkeypatch.setattr("oo_scout.browser._monotonic", lambda: next(clock))
    page = FakePage(["https://www.linkedin.com/login"])
    assert wait_for_feed(page, timeout_s=20, poll_ms=5000) is False
    assert page.waits


def test_session_pause_waits_inside_playwright(monkeypatch, pacer):
    from oo_scout.browser import ScoutSession

    def no_sleep(_s):
        raise AssertionError("time.sleep blocks Playwright route dispatch")

    pacer.sleep = no_sleep
    page = FakePage(["https://www.linkedin.com/feed/"])
    page.is_closed = lambda: False
    session = ScoutSession("/nonexistent", pacer)
    session.page = page
    session.pause()
    lo, hi = pacer.pacing.delay_min_s * 1000, pacer.pacing.delay_max_s * 1000
    assert len(page.waits) == 1 and lo <= page.waits[0] <= hi
