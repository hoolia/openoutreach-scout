"""SCOUT_TEST_RUN: an acceptance run outside the working window, with lower caps."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from oo_scout.config import TEST_RUN, ConfigError, Pacing, Settings
from oo_scout.pacing import PAGE_VIEW, Ledger, Pacer, PacingRefused, SessionOver

# Saturday 2026-11-21 05:00 Europe/Amsterdam: outside every working window
SATURDAY_NIGHT = datetime(2026, 11, 21, 4, 0, tzinfo=UTC)

TEST_ENV = {"SCOUT_TEST_RUN": "true", "SCOUT_MAX_PAGE_VIEWS_DAY": "5", "SCOUT_SESSION_MAX_MIN": "10"}


def _pacer(tmp_path, now, pacing):
    clock = type("C", (), {"now": now, "__call__": lambda self: self.now})()
    return Pacer(pacing, Ledger(tmp_path / "ledger.json"), clock=clock, sleep=lambda _s: None), clock


def test_test_run_is_off_by_default():
    assert Settings.from_env({}).pacing.test_run is False


def test_window_still_applies_without_test_run(tmp_path):
    pacer, _ = _pacer(tmp_path, SATURDAY_NIGHT, Pacing())
    with pytest.raises(PacingRefused) as exc:
        pacer.acquire(PAGE_VIEW)
    assert exc.value.reason == "out_of_hours"


def test_test_run_ignores_the_window_but_caps_page_views_at_5(tmp_path):
    pacer, clock = _pacer(tmp_path, SATURDAY_NIGHT, Settings.from_env(TEST_ENV).pacing)
    assert pacer.in_window()
    pacer.start_session()
    for _ in range(5):
        pacer.acquire(PAGE_VIEW)
        clock.now += timedelta(seconds=30)
    with pytest.raises(PacingRefused) as exc:
        pacer.acquire(PAGE_VIEW)
    assert exc.value.reason == "cap_reached"


def test_test_run_session_ends_after_10_minutes(tmp_path):
    pacer, clock = _pacer(tmp_path, SATURDAY_NIGHT, Settings.from_env(TEST_ENV).pacing)
    pacer.start_session()
    clock.now += timedelta(minutes=10)
    with pytest.raises(SessionOver):
        pacer.acquire(PAGE_VIEW)


def test_test_run_keeps_the_human_delays():
    p = Settings.from_env(TEST_ENV).pacing
    assert (p.delay_min_s, p.delay_max_s) == (20, 120)


@pytest.mark.parametrize("var,value", [
    ("SCOUT_MAX_PAGE_VIEWS_DAY", "6"),
    ("SCOUT_MAX_PAGE_VIEWS_DAY", ""),  # the 50 default is above the test cap
    ("SCOUT_SESSION_MAX_MIN", "11"),
    ("SCOUT_SESSION_MAX_MIN", ""),
    ("SCOUT_DELAY_MIN_S", "5"),
])
def test_test_run_refuses_caps_above_its_bounds(var, value):
    with pytest.raises(ConfigError):
        Settings.from_env({**TEST_ENV, var: value})


def test_test_run_refuses_sending():
    with pytest.raises(ConfigError, match="SCOUT_TEST_RUN"):
        Settings.from_env({**TEST_ENV, "READ_ONLY": "false", "SEND_ENABLED": "true"})


def test_test_run_bounds():
    assert (TEST_RUN.page_views_per_day, TEST_RUN.session_max_min) == (5, 10)


def test_run_forever_runs_one_session_in_a_test_run(tmp_path):
    from oo_scout.playbook import Playbook
    from oo_scout.scout import run_forever

    pacer, _ = _pacer(tmp_path, SATURDAY_NIGHT, Settings.from_env(TEST_ENV).pacing)
    sessions = []

    class FakeScout:
        def run_session(self, playbook, extra=None):
            sessions.append(playbook)

    slept = []
    run_forever(lambda: FakeScout(), pacer, lambda: Playbook(), lambda _m: None,
                sleep=slept.append, max_sessions=1)
    assert len(sessions) == 1
    assert slept == []  # no session pause after the last session
