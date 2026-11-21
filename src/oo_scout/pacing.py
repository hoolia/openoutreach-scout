"""Human pacing: working window, daily and weekly caps, sessions, delays, halts.

Every LinkedIn action asks :meth:`Pacer.acquire` first. ``acquire`` records the action in
the ledger *before* the caller performs it, so a crash mid-action still counts against
the cap. The ledger is a JSON file on the persistent volume, so caps survive restarts.
"""

from __future__ import annotations

import json
import os
import random
import time as _time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import TIMEZONE, Pacing

PAGE_VIEW = "page_view"
CONNECT = "connect"
MESSAGE = "message"
KINDS = (PAGE_VIEW, CONNECT, MESSAGE)

# events older than this are pruned; the longest window counted is 7 days
_KEEP = timedelta(days=8)


class PacingRefused(RuntimeError):
    """The action is not allowed now; ``reason`` says why."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        super().__init__(f"{reason}: {detail}" if detail else reason)


class Halted(PacingRefused):
    """The scout is stopped after a checkpoint, captcha or warning page."""


class SessionOver(PacingRefused):
    """The current session reached its maximum length."""


class Ledger:
    """Action log, halt record and sent-note ids, kept in one JSON file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.data: dict[str, Any] = {"events": [], "halt": None, "sent_notes": [], "seen": []}
        if self.path.exists():
            self.data.update(json.loads(self.path.read_text()))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, sort_keys=True))
        os.replace(tmp, self.path)

    def events(self, kind: str) -> list[datetime]:
        return [datetime.fromisoformat(e["at"]) for e in self.data["events"] if e["kind"] == kind]

    def record(self, kind: str, at: datetime) -> None:
        cutoff = at - _KEEP
        self.data["events"] = [
            e for e in self.data["events"] if datetime.fromisoformat(e["at"]) >= cutoff
        ] + [{"kind": kind, "at": at.isoformat()}]
        self.save()

    # -- halts ------------------------------------------------------------------
    @property
    def halt(self) -> dict | None:
        return self.data.get("halt")

    def set_halt(self, reason: str, url: str, at: datetime, rest_until: datetime) -> None:
        self.data["halt"] = {
            "reason": reason,
            "url": url,
            "at": at.isoformat(),
            "rest_until": rest_until.isoformat(),
            "cleared": False,
        }
        self.save()

    def clear_halt(self) -> None:
        if self.data.get("halt"):
            self.data["halt"]["cleared"] = True
            self.save()

    # -- sent notes (send contract, second guard beside Odoo) -------------------
    def note_sent(self, note_id: int) -> bool:
        return int(note_id) in self.data.get("sent_notes", [])

    def mark_note_sent(self, note_id: int) -> None:
        notes = self.data.setdefault("sent_notes", [])
        if int(note_id) not in notes:
            notes.append(int(note_id))
            self.save()

    # -- profiles already turned into signals ----------------------------------
    def seen(self, key: str) -> bool:
        return key in self.data.get("seen", [])

    def mark_seen(self, key: str) -> None:
        seen = self.data.setdefault("seen", [])
        if key not in seen:
            seen.append(key)
            # keep the newest 20000 keys; ClickHouse collapses any repeat anyway
            del seen[:-20000]
            self.save()


class Pacer:
    def __init__(
        self,
        pacing: Pacing,
        ledger: Ledger,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = _time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.pacing = pacing
        self.ledger = ledger
        self.clock = clock
        self.sleep = sleep
        self.rng = rng or random.SystemRandom()
        self.session_started: datetime | None = None

    # -- window -----------------------------------------------------------------
    def local(self, now: datetime | None = None) -> datetime:
        return (now or self.clock()).astimezone(TIMEZONE)

    def in_window(self, now: datetime | None = None) -> bool:
        if self.pacing.test_run:
            return True
        loc = self.local(now)
        return (
            loc.weekday() in self.pacing.working_days
            and self.pacing.window_start <= loc.time() < self.pacing.window_end
        )

    def next_window_start(self, now: datetime | None = None) -> datetime:
        """Start of the next working window (now when inside one), in UTC."""
        now = now or self.clock()
        if self.in_window(now):
            return now
        loc = self.local(now)
        for days in range(0, 8):
            day = (loc + timedelta(days=days)).date()
            start = datetime.combine(day, self.pacing.window_start, TIMEZONE)
            if day.weekday() in self.pacing.working_days and start > loc:
                return start.astimezone(UTC)
        raise RuntimeError("no working day in the next week")  # unreachable: days non-empty

    # -- halts ------------------------------------------------------------------
    def check_halt(self, now: datetime | None = None) -> None:
        halt = self.ledger.halt
        if not halt:
            return
        now = now or self.clock()
        rest_until = datetime.fromisoformat(halt["rest_until"])
        if not halt.get("cleared"):
            raise Halted("halted", f"{halt['reason']} at {halt['url']}; run 'oo-scout resume' after review")
        if now < rest_until:
            raise Halted("resting", f"rest day until {rest_until.isoformat()}")

    def halt(self, reason: str, url: str) -> datetime:
        now = self.clock()
        rest_until = now + timedelta(hours=self.pacing.rest_hours)
        self.ledger.set_halt(reason, url, now, rest_until)
        return rest_until

    # -- sessions ---------------------------------------------------------------
    def start_session(self) -> None:
        self.check(None)
        self.session_started = self.clock()

    def end_session(self) -> None:
        self.session_started = None

    def session_pause_s(self) -> float:
        return 60.0 * self.rng.uniform(self.pacing.pause_min_min, self.pacing.pause_max_min)

    # -- actions ----------------------------------------------------------------
    def _count(self, kind: str, since: datetime) -> int:
        return sum(1 for at in self.ledger.events(kind) if at >= since)

    def check(self, kind: str | None) -> None:
        """Raise PacingRefused if ``kind`` may not run now; ``None`` checks only the window."""
        now = self.clock()
        self.check_halt(now)
        if not self.in_window(now):
            raise PacingRefused("out_of_hours", self.local(now).strftime("%a %H:%M %Z"))
        if kind is None:
            return
        if kind not in KINDS:
            raise ValueError(f"unknown action kind {kind!r}")
        if self.session_started is not None:
            if now - self.session_started >= timedelta(minutes=self.pacing.session_max_min):
                raise SessionOver("session_over", f"{self.pacing.session_max_min} minutes reached")
        loc = self.local(now)
        day_start = datetime.combine(loc.date(), datetime.min.time(), TIMEZONE).astimezone(UTC)
        week_start = now - timedelta(days=7)
        p = self.pacing
        limits = {
            PAGE_VIEW: [("page_views_per_day", p.page_views_per_day, day_start)],
            CONNECT: [
                ("connects_per_day", p.connects_per_day, day_start),
                ("connects_per_week", p.connects_per_week, week_start),
            ],
            MESSAGE: [("messages_per_day", p.messages_per_day, day_start)],
        }[kind]
        for name, limit, since in limits:
            used = self._count(kind, since)
            if used >= limit:
                raise PacingRefused("cap_reached", f"{name}={limit}, used {used}")

    def acquire(self, kind: str) -> None:
        """Check ``kind`` and record it as done now. Raises PacingRefused."""
        self.check(kind)
        self.ledger.record(kind, self.clock())

    def delay(self, sleep: Callable[[float], None] | None = None) -> float:
        seconds = self.rng.uniform(self.pacing.delay_min_s, self.pacing.delay_max_s)
        (sleep or self.sleep)(seconds)
        return seconds
