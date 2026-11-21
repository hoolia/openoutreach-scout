"""Signal mapping to signals.signal and the ClickHouse insert."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
import respx

from oo_scout.signals import (
    ClickHouseSink,
    JsonlSink,
    SinkError,
    engager_signal,
    profile_signal,
    profile_url,
    signal_id,
)

NOW = datetime(2026, 11, 24, 9, 0, 0, 123000, tzinfo=UTC)
COLUMNS = {"id", "actor_key", "source", "url", "signal_type", "actor_person", "actor_company", "excerpt",
           "observed_at", "rule_id", "score", "status", "odoo_id", "version"}

PROFILE = {
    "url": "https://www.linkedin.com/in/jane-doe/",
    "public_identifier": "jane-doe",
    "full_name": "Jane Doe",
    "headline": "Platform engineer at Example",
    "location_name": "Utrecht",
    "summary": "x" * 5000,
    "positions": [
        {"title": "Platform engineer", "company_name": "Example BV", "date_range": {"start": {"year": 2023}, "end": None}},
        {"title": "SRE", "company_name": "Old Corp", "date_range": {"start": {"year": 2019}, "end": {"year": 2023}}},
    ],
}


def test_profile_signal_maps_every_column():
    s = profile_signal(PROFILE, "icp-platform-nl", now=NOW)
    row = s.row()
    assert set(row) == COLUMNS
    assert row["source"] == "linkedin"
    assert row["url"] == row["actor_key"] == "https://www.linkedin.com/in/jane-doe/"
    assert row["signal_type"] == "trigger_event"
    assert row["actor_person"] == "Jane Doe"
    assert row["actor_company"] == "Example BV"
    assert row["rule_id"] == "icp-platform-nl"
    assert row["status"] == "new" and row["score"] is None and row["odoo_id"] is None
    assert row["observed_at"] == "2026-11-24 09:00:00"
    assert row["version"] == int(NOW.timestamp() * 1000)
    assert row["excerpt"].startswith("Platform engineer at Example | Utrecht | xxx")
    long = profile_signal({**PROFILE, "headline": "h" * 3000}, "r", now=NOW)
    assert len(long.excerpt) == 1000


def test_engager_signal_url_is_post_actor_is_profile():
    post = "https://www.linkedin.com/feed/update/urn:li:activity:7123/?utm_source=share"
    s = engager_signal(post, "https://www.linkedin.com/in/john-smith?miniProfileUrn=x", "engagers-own", now=NOW)
    assert s.url == "https://www.linkedin.com/feed/update/urn:li:activity:7123/"
    assert s.actor_key == "https://www.linkedin.com/in/john-smith/"
    assert s.signal_type == "engagement" and s.source == "linkedin"


def test_two_engagers_of_one_post_are_two_signals():
    post = "https://www.linkedin.com/feed/update/urn:li:activity:7123/"
    a = engager_signal(post, "https://www.linkedin.com/in/a/", "r", now=NOW)
    b = engager_signal(post, "https://www.linkedin.com/in/b/", "r", now=NOW)
    assert a.id != b.id


def test_id_is_stable_per_source_url_actor():
    a = profile_signal(PROFILE, "r1", now=NOW)
    b = profile_signal({**PROFILE, "headline": "changed"}, "r2", now=datetime(2027, 1, 1, tzinfo=UTC))
    assert a.id == b.id == signal_id("linkedin", a.url, a.actor_key)


@pytest.mark.parametrize("raw,clean", [
    ("https://www.linkedin.com/in/jane-doe", "https://www.linkedin.com/in/jane-doe/"),
    ("https://nl.linkedin.com/in/jane-doe/details/experience/?x=1", "https://www.linkedin.com/in/jane-doe/"),
    ("/in/j%C3%A9r%C3%B4me/", "https://www.linkedin.com/in/j%C3%A9r%C3%B4me/"),
    ("https://www.linkedin.com/company/example/", ""),
    ("", ""),
])
def test_profile_url(raw, clean):
    assert profile_url(raw) == clean


def test_engager_needs_a_profile_url():
    with pytest.raises(ValueError):
        engager_signal("https://www.linkedin.com/feed/update/x/", "https://www.linkedin.com/company/x/", "r")


@respx.mock(assert_all_mocked=True)
def test_clickhouse_insert_jsoneachrow(respx_mock):
    route = respx_mock.post("http://clickhouse:8123/").mock(return_value=httpx.Response(200, text=""))
    sink = ClickHouseSink("http://clickhouse:8123", "scout", "secret")
    signals = [profile_signal(PROFILE, "r", now=NOW),
               engager_signal("https://www.linkedin.com/feed/update/x/", "/in/b/", "r", now=NOW)]
    assert sink.write(signals) == 2
    req = route.calls.last.request
    assert req.url.params["query"] == "INSERT INTO signals.signal FORMAT JSONEachRow"
    assert req.headers["X-ClickHouse-User"] == "scout" and req.headers["X-ClickHouse-Key"] == "secret"
    rows = [json.loads(line) for line in req.content.decode().splitlines()]
    assert [r["signal_type"] for r in rows] == ["trigger_event", "engagement"]
    assert all(set(r) == COLUMNS for r in rows)


@respx.mock(assert_all_mocked=True)
def test_clickhouse_error_does_not_leak_password(respx_mock):
    respx_mock.post("http://clickhouse:8123/").mock(return_value=httpx.Response(516, text="Authentication failed"))
    sink = ClickHouseSink("http://clickhouse:8123", "scout", "s3cr3t")
    with pytest.raises(SinkError) as exc:
        sink.write([profile_signal(PROFILE, "r", now=NOW)])
    assert "s3cr3t" not in str(exc.value)


def test_jsonl_sink(tmp_path):
    sink = JsonlSink(tmp_path / "s.jsonl")
    sink.write([profile_signal(PROFILE, "r", now=NOW)])
    assert json.loads((tmp_path / "s.jsonl").read_text())["source"] == "linkedin"
