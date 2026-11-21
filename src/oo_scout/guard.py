"""Read-only guard over every upstream send path.

Two layers, both installed before any LinkedIn page opens:

1. **Function patches.** Every upstream ``linkedin_cli`` function that connects or sends
   a message, and the Voyager client's ``post`` (the only non-GET transport upstream
   has), is replaced by a function that raises :class:`ReadOnlyViolation`. The list is
   :data:`SEND_PATHS`; ``tests/test_guard.py`` fails when upstream gains a send-looking
   function that is not on it.
2. **Network block.** A Playwright route on the browser context aborts every non-GET
   request to LinkedIn that writes: messaging, invitations, relationships, reactions,
   comments, follows, endorsements, posts, and any Rest.li ``?action=`` call. This also
   catches a click on a Connect or Send button that bypasses the patched functions.

The guard is lifted only when READ_ONLY=false and SEND_ENABLED=true; then
:func:`allow_send` hands the original functions to the send contract alone, and the
network block stays off for that browser context only.
"""

from __future__ import annotations

import importlib
import logging
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)


class ReadOnlyViolation(RuntimeError):
    """A send, connect or other LinkedIn write was attempted in read-only mode."""


# (module, attribute) of every upstream function that connects or sends.
SEND_PATHS: tuple[tuple[str, str], ...] = (
    ("linkedin_cli.actions.connect", "send_connection_request"),
    ("linkedin_cli.actions.connect", "_connect_direct"),
    ("linkedin_cli.actions.connect", "_connect_via_more"),
    ("linkedin_cli.actions.connect", "_click_without_note"),
    ("linkedin_cli.actions.message", "send_raw_message"),
    ("linkedin_cli.actions.message", "_send_message"),
    ("linkedin_cli.actions.message", "_send_message_via_api"),
    ("linkedin_cli.api.messaging.send", "send_message"),
    ("linkedin_cli.api.messaging", "send_message"),
)
# the Voyager client's generic POST; every upstream API write goes through it
POST_PATH = ("linkedin_cli.api.client", "PlaywrightLinkedinAPI", "post")

# write endpoints and UI paths on linkedin.com, matched against path and query
_WRITE = re.compile(
    r"(messaging|messenger|createMessage|invitation|normInvitations|memberRelationships|"
    r"relationships|reactions|comments|follows|endorse|contentcreation|ugcPosts|shares|"
    r"voyagerSocialDash|growth/|connect)",
    re.IGNORECASE,
)
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

_originals: dict[tuple[str, ...], Any] = {}
_installed = False


def _blocked(name: str) -> Callable[..., Any]:
    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise ReadOnlyViolation(f"{name} is disabled: READ_ONLY mode")

    refuse.__name__ = f"blocked_{name.rsplit('.', 1)[-1]}"
    refuse.oo_scout_blocked = True  # type: ignore[attr-defined]
    return refuse


def install() -> None:
    """Patch every send path to raise. Idempotent."""
    global _installed
    for module_name, attr in SEND_PATHS:
        module = importlib.import_module(module_name)
        key = (module_name, attr)
        if key not in _originals:
            _originals[key] = getattr(module, attr)
        setattr(module, attr, _blocked(f"{module_name}.{attr}"))
    module_name, cls_name, attr = POST_PATH
    cls = getattr(importlib.import_module(module_name), cls_name)
    if POST_PATH not in _originals:
        _originals[POST_PATH] = getattr(cls, attr)
    setattr(cls, attr, _blocked(f"{module_name}.{cls_name}.{attr}"))
    _installed = True
    logger.info("read-only guard installed on %d send paths", len(SEND_PATHS) + 1)


def installed() -> bool:
    return _installed


def allow_send(settings: Any) -> dict[str, Callable[..., Any]]:
    """Return the original send functions, only when sending is enabled.

    The module attributes stay patched; the send contract calls the originals it gets
    here, so nothing else in the process can reach them.
    """
    if not getattr(settings, "may_send", False):
        raise ReadOnlyViolation("sending needs READ_ONLY=false and SEND_ENABLED=true")
    if not _originals:
        raise ReadOnlyViolation("guard not installed")

    def unguarded(key: tuple[str, str]) -> Callable[..., Any]:
        original = _originals[key]

        def call(*args: Any, **kwargs: Any) -> Any:
            # the upstream senders call each other and the client's post through module
            # globals, so all send paths are restored for this one call, then re-patched
            _restore()
            try:
                return original(*args, **kwargs)
            finally:
                install()

        call.__name__ = f"unguarded_{key[1]}"
        return call

    return {
        "send_connection_request": unguarded(("linkedin_cli.actions.connect", "send_connection_request")),
        "send_raw_message": unguarded(("linkedin_cli.actions.message", "send_raw_message")),
    }


def _restore() -> None:
    for key, original in _originals.items():
        if key == POST_PATH:
            module_name, cls_name, attr = POST_PATH
            setattr(getattr(importlib.import_module(module_name), cls_name), attr, original)
        else:
            setattr(importlib.import_module(key[0]), key[1], original)


def is_write_request(method: str, url: str) -> bool:
    """True for a non-GET request to LinkedIn that can change state."""
    if method.upper() in _READ_METHODS:
        return False
    parts = urlsplit(url)
    host = parts.hostname or ""
    if not (host == "linkedin.com" or host.endswith(".linkedin.com")):
        return False
    target = f"{parts.path}?{parts.query}"
    return bool(_WRITE.search(target)) or "action=" in parts.query


class NetworkGuard:
    """Playwright route handler that aborts LinkedIn writes and counts them."""

    def __init__(self, on_block: Callable[[str, str], None] | None = None) -> None:
        self.blocked: list[tuple[str, str]] = []
        self.on_block = on_block

    def __call__(self, route: Any, request: Any = None) -> None:
        request = request or route.request
        if is_write_request(request.method, request.url):
            self.blocked.append((request.method, request.url))
            logger.error("blocked LinkedIn write in read-only mode: %s %s", request.method, request.url)
            if self.on_block:
                self.on_block(request.method, request.url)
            route.abort("blockedbyclient")
            return
        route.continue_()

    def attach(self, context: Any) -> None:
        context.route("**/*", self)
