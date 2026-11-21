"""Settings from the environment, checked against the hard pacing bounds.

The bounds are the upper limits of the scout's pacing design (README, Pacing). The
environment, and through it the playbook, may only narrow them: a value outside a bound
is a configuration error, never silently raised or clamped.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time
from zoneinfo import ZoneInfo


class ConfigError(ValueError):
    """A setting is missing, malformed or outside its hard bound."""


# the working window and the day caps are in this time zone; the browser reports it too
TIMEZONE = ZoneInfo(os.environ.get("SCOUT_TIMEZONE") or "Europe/Amsterdam")
# browser locale, as LinkedIn sees it
LOCALE = os.environ.get("SCOUT_LOCALE") or "nl-NL"
DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True)
class Bounds:
    working_days: frozenset[int] = frozenset(range(5))  # Monday to Friday
    window_start: time = time(8, 30)
    window_end: time = time(18, 0)
    page_views_per_day: int = 50
    connects_per_day: int = 10
    connects_per_week: int = 80
    messages_per_day: int = 15
    delay_min_s: float = 20.0
    delay_max_s: float = 120.0
    session_max_min: int = 45
    # pause between two sessions, in minutes
    pause_min_min: int = 30
    pause_max_min: int = 120
    # a rest day after a checkpoint, captcha or warning page: at least 24 hours
    rest_min_hours: int = 24


HARD = Bounds()


@dataclass(frozen=True)
class TestRunBounds:
    """Caps of an acceptance run (SCOUT_TEST_RUN), which may run outside the window."""

    page_views_per_day: int = 5
    session_max_min: int = 10


TEST_RUN = TestRunBounds()


def _truthy(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    v = value.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"not a boolean: {value!r}")


def _hhmm(value: str) -> time:
    try:
        hh, mm = value.strip().split(":")
        return time(int(hh), int(mm))
    except ValueError as exc:
        raise ConfigError(f"not a HH:MM time: {value!r}") from exc


@dataclass(frozen=True)
class Pacing:
    working_days: frozenset[int] = HARD.working_days
    window_start: time = HARD.window_start
    window_end: time = HARD.window_end
    page_views_per_day: int = HARD.page_views_per_day
    connects_per_day: int = HARD.connects_per_day
    connects_per_week: int = HARD.connects_per_week
    messages_per_day: int = HARD.messages_per_day
    delay_min_s: float = HARD.delay_min_s
    delay_max_s: float = HARD.delay_max_s
    session_max_min: int = HARD.session_max_min
    rest_hours: int = HARD.rest_min_hours
    pause_min_min: int = HARD.pause_min_min
    pause_max_min: int = HARD.pause_max_min
    # acceptance run: ignores the working window and days, needs the lower TEST_RUN caps
    test_run: bool = False

    def __post_init__(self) -> None:
        errors: list[str] = []
        if self.test_run:
            if self.page_views_per_day > TEST_RUN.page_views_per_day:
                errors.append(f"SCOUT_TEST_RUN needs SCOUT_MAX_PAGE_VIEWS_DAY of at most {TEST_RUN.page_views_per_day}")
            if self.session_max_min > TEST_RUN.session_max_min:
                errors.append(f"SCOUT_TEST_RUN needs SCOUT_SESSION_MAX_MIN of at most {TEST_RUN.session_max_min}")
        if not self.working_days or not self.working_days <= HARD.working_days:
            errors.append("SCOUT_WORKING_DAYS must be a non-empty subset of mon..fri")
        if self.window_start < HARD.window_start:
            errors.append(f"SCOUT_WINDOW_START must not be before {HARD.window_start:%H:%M}")
        if self.window_end > HARD.window_end:
            errors.append(f"SCOUT_WINDOW_END must not be after {HARD.window_end:%H:%M}")
        if self.window_start >= self.window_end:
            errors.append("SCOUT_WINDOW_START must be before SCOUT_WINDOW_END")
        for name, value, bound in (
            ("SCOUT_MAX_PAGE_VIEWS_DAY", self.page_views_per_day, HARD.page_views_per_day),
            ("SCOUT_MAX_CONNECTS_DAY", self.connects_per_day, HARD.connects_per_day),
            ("SCOUT_MAX_CONNECTS_WEEK", self.connects_per_week, HARD.connects_per_week),
            ("SCOUT_MAX_MESSAGES_DAY", self.messages_per_day, HARD.messages_per_day),
            ("SCOUT_SESSION_MAX_MIN", self.session_max_min, HARD.session_max_min),
        ):
            if not 0 <= value <= bound:
                errors.append(f"{name} must be between 0 and {bound}, got {value}")
        if self.delay_min_s < HARD.delay_min_s:
            errors.append(f"SCOUT_DELAY_MIN_S must be at least {HARD.delay_min_s:g}")
        if self.delay_max_s > HARD.delay_max_s:
            errors.append(f"SCOUT_DELAY_MAX_S must be at most {HARD.delay_max_s:g}")
        if self.delay_min_s > self.delay_max_s:
            errors.append("SCOUT_DELAY_MIN_S must not exceed SCOUT_DELAY_MAX_S")
        if self.rest_hours < HARD.rest_min_hours:
            errors.append(f"SCOUT_REST_HOURS must be at least {HARD.rest_min_hours}")
        if self.pause_min_min < HARD.pause_min_min:
            errors.append(f"SCOUT_PAUSE_MIN_MIN must be at least {HARD.pause_min_min}")
        if self.pause_max_min > HARD.pause_max_min:
            errors.append(f"SCOUT_PAUSE_MAX_MIN must be at most {HARD.pause_max_min}")
        if self.pause_min_min > self.pause_max_min:
            errors.append("SCOUT_PAUSE_MIN_MIN must not exceed SCOUT_PAUSE_MAX_MIN")
        if errors:
            raise ConfigError("; ".join(errors))

    @classmethod
    def from_env(cls, env: dict[str, str]) -> Pacing:
        kw: dict = {"test_run": _truthy(env.get("SCOUT_TEST_RUN"), False)}
        try:
            if env.get("SCOUT_WORKING_DAYS"):
                names = [d.strip().lower() for d in env["SCOUT_WORKING_DAYS"].split(",") if d.strip()]
                unknown = [d for d in names if d not in DAY_NAMES]
                if unknown:
                    raise ConfigError(f"unknown day names in SCOUT_WORKING_DAYS: {unknown}")
                kw["working_days"] = frozenset(DAY_NAMES.index(d) for d in names)
            if env.get("SCOUT_WINDOW_START"):
                kw["window_start"] = _hhmm(env["SCOUT_WINDOW_START"])
            if env.get("SCOUT_WINDOW_END"):
                kw["window_end"] = _hhmm(env["SCOUT_WINDOW_END"])
            for var, attr, conv in (
                ("SCOUT_MAX_PAGE_VIEWS_DAY", "page_views_per_day", int),
                ("SCOUT_MAX_CONNECTS_DAY", "connects_per_day", int),
                ("SCOUT_MAX_CONNECTS_WEEK", "connects_per_week", int),
                ("SCOUT_MAX_MESSAGES_DAY", "messages_per_day", int),
                ("SCOUT_DELAY_MIN_S", "delay_min_s", float),
                ("SCOUT_DELAY_MAX_S", "delay_max_s", float),
                ("SCOUT_SESSION_MAX_MIN", "session_max_min", int),
                ("SCOUT_REST_HOURS", "rest_hours", int),
                ("SCOUT_PAUSE_MIN_MIN", "pause_min_min", int),
                ("SCOUT_PAUSE_MAX_MIN", "pause_max_min", int),
            ):
                if env.get(var):
                    kw[attr] = conv(env[var])
        except ValueError as exc:
            if isinstance(exc, ConfigError):
                raise
            raise ConfigError(f"malformed pacing value: {exc}") from exc
        return cls(**kw)


@dataclass(frozen=True)
class Settings:
    pacing: Pacing = field(default_factory=Pacing)
    # READ_ONLY=true (default) disables every connect and message path, whatever
    # SEND_ENABLED says. Sending needs READ_ONLY=false and SEND_ENABLED=true.
    read_only: bool = True
    send_enabled: bool = False
    data_dir: str = "/data"
    playbook_path: str = "/etc/oo-scout/playbook.yaml"
    storage_state_path: str = "/var/run/secrets/linkedin/storage_state.json"
    clickhouse_url: str = ""
    clickhouse_user: str = ""
    clickhouse_password: str = ""
    clickhouse_table: str = "signals.signal"
    slack_webhook_url: str = ""
    chatwoot_url: str = ""
    chatwoot_account_id: int = 0
    chatwoot_api_token: str = ""
    chatwoot_lead_attribute: str = "odoo_lead_id"
    # Chatwoot user ids whose notes never count as human approval: the agent-role users
    # behind chatwoot-mcp and any bot. Required when sending is enabled.
    chatwoot_excluded_sender_ids: frozenset[int] = frozenset()
    odoo_url: str = ""
    odoo_db: str = ""
    odoo_user: str = ""
    odoo_password: str = ""

    @property
    def may_send(self) -> bool:
        return (not self.read_only) and self.send_enabled

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        env = dict(os.environ if env is None else env)
        try:
            account = int(env.get("CHATWOOT_ACCOUNT_ID") or 0)
            excluded = frozenset(
                int(x) for x in (env.get("CHATWOOT_EXCLUDED_SENDER_IDS") or "").split(",") if x.strip()
            )
        except ValueError as exc:
            raise ConfigError("CHATWOOT_ACCOUNT_ID and CHATWOOT_EXCLUDED_SENDER_IDS must be integers") from exc
        s = cls(
            pacing=Pacing.from_env(env),
            read_only=_truthy(env.get("READ_ONLY"), True),
            send_enabled=_truthy(env.get("SEND_ENABLED"), False),
            data_dir=env.get("SCOUT_DATA_DIR") or "/data",
            playbook_path=env.get("SCOUT_PLAYBOOK") or "/etc/oo-scout/playbook.yaml",
            storage_state_path=env.get("LINKEDIN_STORAGE_STATE")
            or "/var/run/secrets/linkedin/storage_state.json",
            clickhouse_url=(env.get("CLICKHOUSE_URL") or "").rstrip("/"),
            clickhouse_user=env.get("CLICKHOUSE_USER") or "",
            clickhouse_password=env.get("CLICKHOUSE_PASSWORD") or "",
            clickhouse_table=env.get("CLICKHOUSE_TABLE") or "signals.signal",
            slack_webhook_url=env.get("SLACK_WEBHOOK_URL") or "",
            chatwoot_url=(env.get("CHATWOOT_URL") or "").rstrip("/"),
            chatwoot_account_id=account,
            chatwoot_api_token=env.get("CHATWOOT_API_TOKEN") or "",
            chatwoot_lead_attribute=env.get("CHATWOOT_LEAD_ATTRIBUTE") or "odoo_lead_id",
            chatwoot_excluded_sender_ids=excluded,
            odoo_url=(env.get("ODOO_URL") or "").rstrip("/"),
            odoo_db=env.get("ODOO_DB") or "",
            odoo_user=env.get("ODOO_USER") or "",
            odoo_password=env.get("ODOO_PASSWORD") or "",
        )
        if s.may_send and s.pacing.test_run:
            raise ConfigError("SCOUT_TEST_RUN is read-only: it refuses READ_ONLY=false with SEND_ENABLED=true")
        if s.may_send:
            missing = [
                k
                for k, v in (
                    ("CHATWOOT_URL", s.chatwoot_url),
                    ("CHATWOOT_ACCOUNT_ID", s.chatwoot_account_id),
                    ("CHATWOOT_API_TOKEN", s.chatwoot_api_token),
                    ("ODOO_URL", s.odoo_url),
                    ("ODOO_DB", s.odoo_db),
                    ("ODOO_USER", s.odoo_user),
                    ("ODOO_PASSWORD", s.odoo_password),
                    ("CHATWOOT_EXCLUDED_SENDER_IDS", s.chatwoot_excluded_sender_ids),
                )
                if not v
            ]
            if missing:
                raise ConfigError(f"sending is enabled but these are missing: {', '.join(missing)}")
        return s
