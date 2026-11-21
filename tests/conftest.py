from __future__ import annotations

from datetime import UTC, datetime

import pytest

from oo_scout import guard
from oo_scout.config import Pacing
from oo_scout.pacing import Ledger, Pacer

# Tuesday 2026-11-24 10:00 Europe/Amsterdam (CET, UTC+1)
TUESDAY_10 = datetime(2026, 11, 24, 9, 0, tzinfo=UTC)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock(TUESDAY_10)


@pytest.fixture
def ledger(tmp_path) -> Ledger:
    return Ledger(tmp_path / "ledger.json")


@pytest.fixture
def pacer(ledger, clock) -> Pacer:
    return Pacer(Pacing(), ledger, clock=clock, sleep=lambda _s: None)


@pytest.fixture
def guarded():
    """Install the read-only guard and undo it after the test."""
    guard.install()
    yield
    guard._restore()
    guard._installed = False
