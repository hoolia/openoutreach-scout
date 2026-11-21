"""The LinkedIn part of the marketing playbook.

Read from a YAML file at the start of every session, so a changed ConfigMap takes effect
without a restart. Only the ``linkedin`` key is read; other keys belong to other jobs::

    linkedin:
      rules:
        - id: icp-platform-nl            # becomes signal rule_id
          people_keywords: ["platform engineer kubernetes"]
          network: [second, third]       # optional: first, second, third
          max_profiles_per_search: 5     # optional, default 5, at most 10
        - id: engagers-own-posts
          watch_posts:                   # own and competitor posts
            - https://www.linkedin.com/feed/update/urn:li:activity:123/

An invalid file raises :class:`PlaybookError`; the scout then keeps the last valid
version it loaded in this process and alerts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import yaml

NETWORK = {"first": "F", "second": "S", "third": "O"}
MAX_PROFILES_PER_SEARCH = 10
POST_PATHS = ("/feed/update/", "/posts/")


class PlaybookError(ValueError):
    pass


def _is_post_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parts = urlsplit(value)
    host = (parts.hostname or "").lower()
    return (
        parts.scheme == "https"
        and (host == "linkedin.com" or host.endswith(".linkedin.com"))
        and parts.path.startswith(POST_PATHS)
    )


def _list(raw: dict, key: str) -> list:
    value = raw.get(key) or []
    if not isinstance(value, list):
        raise PlaybookError(f"rule {raw['id']}: {key} must be a list")
    return value


@dataclass(frozen=True)
class Rule:
    id: str
    people_keywords: tuple[str, ...] = ()
    network: tuple[str, ...] = ()
    max_profiles_per_search: int = 5
    watch_posts: tuple[str, ...] = ()


@dataclass(frozen=True)
class Playbook:
    rules: tuple[Rule, ...] = field(default_factory=tuple)


def parse(data: object) -> Playbook:
    if not isinstance(data, dict):
        raise PlaybookError("playbook must be a mapping")
    section = data.get("linkedin") or {}
    if not isinstance(section, dict):
        raise PlaybookError("'linkedin' must be a mapping")
    rules = []
    seen = set()
    for i, raw in enumerate(section.get("rules") or []):
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"]:
            raise PlaybookError(f"rule {i}: needs a string id")
        if raw["id"] in seen:
            raise PlaybookError(f"rule {raw['id']}: duplicate id")
        seen.add(raw["id"])
        kws = _list(raw, "people_keywords")
        posts = _list(raw, "watch_posts")
        net = _list(raw, "network")
        if not all(isinstance(k, str) and k.strip() for k in kws):
            raise PlaybookError(f"rule {raw['id']}: people_keywords must be strings")
        if not all(_is_post_url(p) for p in posts):
            raise PlaybookError(f"rule {raw['id']}: watch_posts must be https LinkedIn post URLs")
        bad = [n for n in net if n not in NETWORK]
        if bad:
            raise PlaybookError(f"rule {raw['id']}: unknown network {bad}")
        n = raw.get("max_profiles_per_search", 5)
        if not isinstance(n, int) or not 1 <= n <= MAX_PROFILES_PER_SEARCH:
            raise PlaybookError(f"rule {raw['id']}: max_profiles_per_search must be 1..{MAX_PROFILES_PER_SEARCH}")
        if not kws and not posts:
            raise PlaybookError(f"rule {raw['id']}: needs people_keywords or watch_posts")
        rules.append(Rule(raw["id"], tuple(k.strip() for k in kws), tuple(NETWORK[x] for x in net), n, tuple(posts)))
    return Playbook(tuple(rules))


def load(path: str | Path) -> Playbook:
    try:
        return parse(yaml.safe_load(Path(path).read_text()))
    except (OSError, yaml.YAMLError) as exc:
        raise PlaybookError(f"cannot read playbook {path}: {exc}") from exc
