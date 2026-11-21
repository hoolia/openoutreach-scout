"""Env values may narrow the pacing bounds, never widen them."""

from __future__ import annotations

from datetime import time

import pytest

from oo_scout.config import HARD, ConfigError, Pacing, Settings


def test_defaults_are_the_hard_bounds_and_read_only():
    s = Settings.from_env({})
    assert s.read_only is True and s.send_enabled is False and s.may_send is False
    p = s.pacing
    assert (p.page_views_per_day, p.connects_per_day, p.connects_per_week, p.messages_per_day) == (50, 10, 80, 15)
    assert (p.delay_min_s, p.delay_max_s, p.session_max_min) == (20, 120, 45)
    assert (p.window_start, p.window_end) == (time(8, 30), time(18, 0))
    assert p.working_days == HARD.working_days


def test_narrower_values_are_accepted():
    p = Pacing.from_env({
        "SCOUT_MAX_PAGE_VIEWS_DAY": "30", "SCOUT_MAX_CONNECTS_DAY": "5", "SCOUT_MAX_CONNECTS_WEEK": "20",
        "SCOUT_MAX_MESSAGES_DAY": "3", "SCOUT_DELAY_MIN_S": "45", "SCOUT_DELAY_MAX_S": "90",
        "SCOUT_SESSION_MAX_MIN": "20", "SCOUT_WINDOW_START": "09:00", "SCOUT_WINDOW_END": "17:00",
        "SCOUT_WORKING_DAYS": "tue,thu", "SCOUT_REST_HOURS": "48",
    })
    assert p.page_views_per_day == 30 and p.working_days == frozenset({1, 3}) and p.rest_hours == 48


@pytest.mark.parametrize("var,value", [
    ("SCOUT_MAX_PAGE_VIEWS_DAY", "51"),
    ("SCOUT_MAX_CONNECTS_DAY", "11"),
    ("SCOUT_MAX_CONNECTS_WEEK", "81"),
    ("SCOUT_MAX_MESSAGES_DAY", "16"),
    ("SCOUT_DELAY_MIN_S", "19"),
    ("SCOUT_DELAY_MAX_S", "121"),
    ("SCOUT_SESSION_MAX_MIN", "46"),
    ("SCOUT_WINDOW_START", "08:29"),
    ("SCOUT_WINDOW_END", "18:01"),
    ("SCOUT_WORKING_DAYS", "mon,sat"),
    ("SCOUT_REST_HOURS", "12"),
    ("SCOUT_PAUSE_MIN_MIN", "29"),
    ("SCOUT_PAUSE_MAX_MIN", "121"),
    ("SCOUT_MAX_PAGE_VIEWS_DAY", "-1"),
    ("SCOUT_MAX_PAGE_VIEWS_DAY", "lots"),
])
def test_values_beyond_a_bound_are_errors(var, value):
    with pytest.raises(ConfigError):
        Settings.from_env({var: value})


def test_send_needs_both_switches():
    assert not Settings.from_env({"SEND_ENABLED": "true"}).may_send
    assert not Settings.from_env({"READ_ONLY": "false"}).may_send


def test_send_enabled_requires_credentials_and_excluded_senders():
    with pytest.raises(ConfigError, match="CHATWOOT_EXCLUDED_SENDER_IDS"):
        Settings.from_env({
            "READ_ONLY": "false", "SEND_ENABLED": "true",
            "CHATWOOT_URL": "http://cw", "CHATWOOT_ACCOUNT_ID": "1", "CHATWOOT_API_TOKEN": "t",
            "ODOO_URL": "http://odoo", "ODOO_DB": "db", "ODOO_USER": "u", "ODOO_PASSWORD": "p",
        })


def test_bad_boolean_is_an_error():
    with pytest.raises(ConfigError):
        Settings.from_env({"READ_ONLY": "maybe"})


@pytest.mark.parametrize(("env", "expected"), [
    ({}, "Europe/Amsterdam nl-NL"),
    ({"SCOUT_TIMEZONE": "Europe/London", "SCOUT_LOCALE": "en-GB"}, "Europe/London en-GB"),
])
def test_timezone_and_locale_come_from_the_environment(env, expected):
    # read once at import, so a fresh interpreter sees each environment
    import os
    import subprocess
    import sys

    base = {k: v for k, v in os.environ.items() if k not in ("SCOUT_TIMEZONE", "SCOUT_LOCALE")}
    out = subprocess.run(
        [sys.executable, "-c", "from oo_scout import config; print(config.TIMEZONE, config.LOCALE)"],
        env={**base, **env, "PYTHONPATH": os.pathsep.join(sys.path)},
        capture_output=True, text=True, check=True,
    )
    assert out.stdout.strip() == expected
