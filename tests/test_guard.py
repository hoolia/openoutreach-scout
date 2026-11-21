"""READ_ONLY blocks every upstream send path, in Python and on the network."""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import re
from types import SimpleNamespace

import pytest

import linkedin_cli
from oo_scout import guard
from oo_scout.config import Settings


@pytest.mark.parametrize("module_name,attr", guard.SEND_PATHS)
def test_every_send_path_raises(guarded, module_name, attr):
    fn = getattr(importlib.import_module(module_name), attr)
    with pytest.raises(guard.ReadOnlyViolation):
        fn(object(), {"public_identifier": "x", "urn": "urn:li:fsd_profile:1"}, "hello")


def test_voyager_post_raises(guarded):
    from linkedin_cli.api.client import PlaywrightLinkedinAPI

    api = PlaywrightLinkedinAPI.__new__(PlaywrightLinkedinAPI)  # no browser needed
    with pytest.raises(guard.ReadOnlyViolation):
        api.post("https://www.linkedin.com/voyager/api/x?action=createMessage", data="{}")


def test_upstream_entry_points_blocked_end_to_end(guarded):
    """Calling the public senders as the daemon would still ends in the guard."""
    from linkedin_cli.actions import connect, message

    with pytest.raises(guard.ReadOnlyViolation):
        connect.send_connection_request(object(), {"public_identifier": "x"})
    with pytest.raises(guard.ReadOnlyViolation):
        message.send_raw_message(object(), {"public_identifier": "x", "urn": "u"}, "hi")


_SEND_NAME = re.compile(r"(send|connect|invite|message|post|react|comment|follow|endorse)", re.I)
# read-only functions whose names match the pattern above, reviewed by hand
_REVIEWED_READS = {
    "linkedin_cli.actions.conversations.find_conversation_urn",
    "linkedin_cli.actions.conversations.find_conversation_urn_via_navigation",
    "linkedin_cli.actions.conversations.get_conversation",
    "linkedin_cli.actions.conversations.parse_message_element",
    "linkedin_cli.actions.conversations.parse_messages",
    "linkedin_cli.actions.status._has_connect_in_more",  # opens and closes the More menu only
    "linkedin_cli.api.voyager._extract_connection_info",
    "linkedin_cli.api.messaging.conversations.fetch_conversations",
    "linkedin_cli.api.messaging.conversations.fetch_messages",
    "linkedin_cli.api.messaging.utils.check_response",
    "linkedin_cli.api.voyager.parse_connection_degree",
    "linkedin_cli.actions.status.get_connection_status",
    "linkedin_cli.launcher.open_bound_session",
    "linkedin_cli.browser.login.submit_login_form",
}


def _upstream_functions():
    for info in pkgutil.walk_packages(linkedin_cli.__path__, "linkedin_cli."):
        if info.name.endswith(".cli"):
            continue  # the upstream CLI is not shipped as an entry point and not imported
        module = importlib.import_module(info.name)
        for name, obj in vars(module).items():
            if inspect.isfunction(obj) and obj.__module__ == info.name:
                yield f"{info.name}.{name}"


def test_no_unreviewed_send_looking_function_upstream():
    """Fails when the pinned upstream gains a send-looking function the guard does not cover."""
    covered = {f"{m}.{a}" for m, a in guard.SEND_PATHS}
    suspicious = {f for f in _upstream_functions() if _SEND_NAME.search(f.rsplit(".", 1)[-1])}
    unreviewed = suspicious - covered - _REVIEWED_READS
    assert not unreviewed, f"review and add to guard.SEND_PATHS or _REVIEWED_READS: {sorted(unreviewed)}"


@pytest.mark.parametrize("method,url", [
    ("POST", "https://www.linkedin.com/voyager/api/voyagerMessagingDashMessengerMessages?action=createMessage"),
    ("POST", "https://www.linkedin.com/voyager/api/voyagerRelationshipsDashMemberRelationships?action=verifyQuotaAndCreateV2"),
    ("POST", "https://www.linkedin.com/voyager/api/growth/normInvitations"),
    ("POST", "https://www.linkedin.com/voyager/api/voyagerSocialDashReactions?threadUrn=x"),
    ("POST", "https://www.linkedin.com/voyager/api/voyagerSocialDashNormComments"),
    ("PUT", "https://www.linkedin.com/voyager/api/feed/follows/x"),
    ("DELETE", "https://www.linkedin.com/voyager/api/relationships/invitations/1"),
    ("POST", "https://www.linkedin.com/voyager/api/anything?action=doSomething"),
])
def test_network_guard_aborts_writes(method, url):
    calls = []
    route = SimpleNamespace(abort=lambda code: calls.append(("abort", code)),
                            continue_=lambda: calls.append(("continue",)))
    g = guard.NetworkGuard()
    g(route, SimpleNamespace(method=method, url=url))
    assert calls == [("abort", "blockedbyclient")]
    assert g.blocked == [(method, url)]


@pytest.mark.parametrize("method,url", [
    ("GET", "https://www.linkedin.com/voyager/api/identity/dash/profiles?q=memberIdentity"),
    ("GET", "https://www.linkedin.com/messaging/thread/new/"),
    ("POST", "https://www.linkedin.com/li/track"),
    ("POST", "https://example.org/messaging?action=createMessage"),
])
def test_network_guard_lets_reads_through(method, url):
    calls = []
    route = SimpleNamespace(abort=lambda code: calls.append(("abort", code)),
                            continue_=lambda: calls.append(("continue",)))
    guard.NetworkGuard()(route, SimpleNamespace(method=method, url=url))
    assert calls == [("continue",)]


@pytest.mark.parametrize("env", [
    {},
    {"SEND_ENABLED": "true"},
    {"READ_ONLY": "false"},
    {"READ_ONLY": "true", "SEND_ENABLED": "true"},
])
def test_allow_send_refuses_unless_both_switches(guarded, env):
    with pytest.raises(guard.ReadOnlyViolation):
        guard.allow_send(Settings.from_env(env))


def test_allow_send_unpatches_only_during_the_call(guarded, monkeypatch):
    from linkedin_cli.actions import connect

    seen = {}

    def fake_original(session, profile):
        # inside the call the helpers are the originals again
        seen["helper_blocked"] = getattr(connect._connect_direct, "oo_scout_blocked", False)
        return "Pending"

    monkeypatch.setitem(guard._originals, ("linkedin_cli.actions.connect", "send_connection_request"), fake_original)
    settings = SimpleNamespace(may_send=True)
    senders = guard.allow_send(settings)
    assert senders["send_connection_request"](object(), {}) == "Pending"
    assert seen["helper_blocked"] is False
    # and the guard is back afterwards
    assert getattr(connect.send_connection_request, "oo_scout_blocked", False)
    assert getattr(connect._connect_direct, "oo_scout_blocked", False)
