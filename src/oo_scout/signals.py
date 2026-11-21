"""Signal records (README, Signal table) and where they go.

``signals.signal`` is a ReplacingMergeTree ordered by ``(source, url, actor_key)``; a
repeat insert of the same signal collapses, so the scout does not need to dedupe
against ClickHouse. The scout never sets ``score``, ``odoo_id`` or a status other than
``new``: the qualifier owns those.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import httpx

logger = logging.getLogger(__name__)

SOURCE = "linkedin"
SIGNAL_TYPES = ("stated_need", "trigger_event", "competitor_pain", "engagement", "visit")
EXCERPT_MAX = 1000
# fixed namespace: the id is stable per (source, url, actor_key) across scouts and runs
NAMESPACE = uuid.UUID("6b1f3c2e-5d0a-4a8e-9a57-0c9e8d2f4b11")


class SinkError(RuntimeError):
    """The signal store refused or failed an insert."""


def profile_url(value: str) -> str:
    """Canonical profile URL ``https://www.linkedin.com/in/<id>/``, or '' if not a profile."""
    if not value:
        return ""
    parts = urlsplit(value.strip())
    segs = [s for s in parts.path.split("/") if s]
    if len(segs) < 2 or segs[0] != "in":
        return ""
    return f"https://www.linkedin.com/in/{quote(unquote(segs[1]), safe='')}/"


def post_url(value: str) -> str:
    """A LinkedIn post URL without query or fragment."""
    parts = urlsplit(value.strip())
    return f"https://www.linkedin.com{parts.path}"


def signal_id(source: str, url: str, actor_key: str) -> str:
    return str(uuid.uuid5(NAMESPACE, f"{source}\n{url}\n{actor_key}"))


def _ts(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


@dataclass(frozen=True)
class Signal:
    id: str
    actor_key: str
    source: str
    url: str
    signal_type: str
    actor_person: str | None
    actor_company: str | None
    excerpt: str
    observed_at: str
    rule_id: str
    score: float | None
    status: str
    odoo_id: int | None
    version: int

    def row(self) -> dict[str, Any]:
        return asdict(self)


def _make(url: str, actor_key: str, signal_type: str, rule_id: str, observed: datetime,
          person: str | None, company: str | None, excerpt: str, now: datetime) -> Signal:
    if signal_type not in SIGNAL_TYPES:
        raise ValueError(f"unknown signal_type {signal_type!r}")
    return Signal(
        id=signal_id(SOURCE, url, actor_key),
        actor_key=actor_key,
        source=SOURCE,
        url=url,
        signal_type=signal_type,
        actor_person=person or None,
        actor_company=company or None,
        excerpt=(excerpt or "")[:EXCERPT_MAX],
        observed_at=_ts(observed),
        rule_id=rule_id,
        score=None,
        status="new",
        odoo_id=None,
        version=int(now.timestamp() * 1000),
    )


def _current_company(profile: dict) -> str | None:
    for pos in profile.get("positions") or []:
        end = ((pos.get("date_range") or {}).get("end")) or None
        if not end or not end.get("year"):
            return pos.get("company_name") or None
    return None


def profile_signal(profile: dict, rule_id: str, now: datetime | None = None) -> Signal:
    """A profile found through an ICP people search (playbook rule ``rule_id``).

    url and actor_key are both the profile URL; the evidence the human opens is the
    profile. ``signal_type`` is ``trigger_event`` because the signal table has no ICP-match type.
    """
    now = now or datetime.now(UTC)
    url = profile_url(profile.get("url") or f"/in/{profile.get('public_identifier', '')}/")
    if not url:
        raise ValueError("profile has no LinkedIn /in/ URL")
    excerpt = " | ".join(
        x for x in (profile.get("headline"), profile.get("location_name"), (profile.get("summary") or "")[:600]) if x
    )
    return _make(url, url, "trigger_event", rule_id, now, profile.get("full_name"),
                 _current_company(profile), excerpt, now)


def engager_signal(post: str, engager: str, rule_id: str, *, person: str | None = None,
                   excerpt: str = "", now: datetime | None = None) -> Signal:
    """Someone who reacted to or commented on a watched post: url is the post, actor_key the profile."""
    now = now or datetime.now(UTC)
    actor = profile_url(engager)
    if not actor:
        raise ValueError(f"not a LinkedIn profile URL: {engager!r}")
    return _make(post_url(post), actor, "engagement", rule_id, now, person, None, excerpt, now)


class ClickHouseSink:
    """Insert rows over the ClickHouse HTTP interface as JSONEachRow."""

    def __init__(self, url: str, user: str, password: str, table: str = "signals.signal",
                 transport: httpx.BaseTransport | None = None, timeout: float = 30.0) -> None:
        if not url.startswith(("http://", "https://")):
            raise ValueError("CLICKHOUSE_URL must start with http:// or https://")
        self.table = table
        self._http = httpx.Client(
            base_url=url,
            headers={"X-ClickHouse-User": user, "X-ClickHouse-Key": password},
            timeout=timeout,
            transport=transport,
        )

    def write(self, signals: list[Signal]) -> int:
        if not signals:
            return 0
        body = "\n".join(json.dumps(s.row(), ensure_ascii=False) for s in signals) + "\n"
        query = f"INSERT INTO {self.table} FORMAT JSONEachRow"
        try:
            r = self._http.post("/", params={"query": query}, content=body.encode())
        except httpx.HTTPError as exc:
            raise SinkError(f"ClickHouse request failed: {type(exc).__name__}") from exc
        if r.status_code != 200:
            raise SinkError(f"ClickHouse returned HTTP {r.status_code}: {r.text[:300]}")
        return len(signals)


class JsonlSink:
    """Append rows to a local file; used when CLICKHOUSE_URL is empty (dry runs)."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def write(self, signals: list[Signal]) -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            for s in signals:
                f.write(json.dumps(s.row(), ensure_ascii=False) + "\n")
        return len(signals)
