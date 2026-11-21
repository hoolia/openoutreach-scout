"""Pacing: caps, working window, sessions, delays and halts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from oo_scout.config import Pacing
from oo_scout.pacing import CONNECT, MESSAGE, PAGE_VIEW, Halted, Ledger, Pacer, PacingRefused, SessionOver


def test_page_view_51_is_refused(pacer, clock):
    for i in range(50):
        pacer.acquire(PAGE_VIEW)
        clock.now += timedelta(seconds=30)
    with pytest.raises(PacingRefused) as exc:
        pacer.acquire(PAGE_VIEW)
    assert exc.value.reason == "cap_reached"
    assert len(pacer.ledger.events(PAGE_VIEW)) == 50


def test_daily_cap_resets_next_working_day(pacer, clock):
    for _ in range(50):
        pacer.acquire(PAGE_VIEW)
    clock.now += timedelta(days=1)  # Wednesday 10:00
    pacer.acquire(PAGE_VIEW)


def test_cap_survives_restart(pacer, clock, tmp_path):
    for _ in range(50):
        pacer.acquire(PAGE_VIEW)
    fresh = Pacer(Pacing(), Ledger(pacer.ledger.path), clock=clock, sleep=lambda _s: None)
    with pytest.raises(PacingRefused):
        fresh.acquire(PAGE_VIEW)


def test_connect_11_per_day_is_refused(pacer):
    for _ in range(10):
        pacer.acquire(CONNECT)
    with pytest.raises(PacingRefused, match="connects_per_day"):
        pacer.acquire(CONNECT)


def test_connect_81_per_rolling_week_is_refused(pacer, clock):
    # 80 connects on the six previous days, none today: the daily cap is free, the weekly is not
    for day in range(1, 7):
        for _ in range(14 if day <= 2 else 13):
            pacer.ledger.data["events"].append({"kind": CONNECT, "at": (clock.now - timedelta(days=day)).isoformat()})
    assert len(pacer.ledger.events(CONNECT)) == 80
    with pytest.raises(PacingRefused, match="connects_per_week"):
        pacer.acquire(CONNECT)
    # eight days later the oldest have rolled out
    clock.now += timedelta(days=7)
    pacer.acquire(CONNECT)


def test_message_16_is_refused(pacer):
    for _ in range(15):
        pacer.acquire(MESSAGE)
    with pytest.raises(PacingRefused, match="messages_per_day"):
        pacer.acquire(MESSAGE)


@pytest.mark.parametrize("when,label", [
    (datetime(2026, 11, 24, 7, 29, tzinfo=UTC), "Tue 08:29 CET"),
    (datetime(2026, 11, 24, 17, 0, tzinfo=UTC), "Tue 18:00 CET"),
    (datetime(2026, 11, 28, 10, 0, tzinfo=UTC), "Sat 11:00 CET"),
    (datetime(2026, 11, 29, 10, 0, tzinfo=UTC), "Sun 11:00 CET"),
    (datetime(2026, 7, 7, 6, 29, tzinfo=UTC), "Tue 08:29 CEST"),
])
def test_out_of_hours_refuses(pacer, clock, when, label):
    clock.now = when
    with pytest.raises(PacingRefused) as exc:
        pacer.acquire(PAGE_VIEW)
    assert exc.value.reason == "out_of_hours"
    assert pacer.ledger.events(PAGE_VIEW) == []


@pytest.mark.parametrize("when", [
    datetime(2026, 11, 24, 7, 30, tzinfo=UTC),   # 08:30 CET
    datetime(2026, 11, 24, 16, 59, tzinfo=UTC),  # 17:59 CET
    datetime(2026, 7, 7, 6, 30, tzinfo=UTC),     # 08:30 CEST
])
def test_inside_window_allows(pacer, clock, when):
    clock.now = when
    pacer.acquire(PAGE_VIEW)


def test_next_window_start_skips_weekend(pacer, clock):
    clock.now = datetime(2026, 11, 27, 17, 30, tzinfo=UTC)  # Friday 18:30 CET
    assert pacer.next_window_start() == datetime(2026, 11, 30, 7, 30, tzinfo=UTC)  # Monday 08:30


def test_session_ends_after_45_minutes(pacer, clock):
    pacer.start_session()
    clock.now += timedelta(minutes=44, seconds=59)
    pacer.acquire(PAGE_VIEW)
    clock.now += timedelta(seconds=1)
    with pytest.raises(SessionOver):
        pacer.acquire(PAGE_VIEW)


def test_delays_are_random_within_bounds(ledger, clock):
    slept = []
    pacer = Pacer(Pacing(), ledger, clock=clock, sleep=slept.append)
    for _ in range(500):
        pacer.delay()
    assert min(slept) >= 20 and max(slept) <= 120
    assert len(set(round(s, 3) for s in slept)) > 400


def test_halt_needs_resume_and_a_rest_day(pacer, clock):
    rest_until = pacer.halt("checkpoint", "https://www.linkedin.com/checkpoint/challenge/x")
    assert rest_until - clock.now == timedelta(hours=24)
    with pytest.raises(Halted, match="halted"):
        pacer.acquire(PAGE_VIEW)
    pacer.ledger.clear_halt()
    with pytest.raises(Halted, match="resting"):
        pacer.acquire(PAGE_VIEW)
    clock.now = rest_until  # Wednesday 10:00
    pacer.acquire(PAGE_VIEW)


def test_halt_without_resume_stays_after_rest(pacer, clock):
    pacer.halt("captcha", "https://www.linkedin.com/x?captcha=1")
    clock.now += timedelta(days=7)
    with pytest.raises(Halted):
        pacer.acquire(PAGE_VIEW)
