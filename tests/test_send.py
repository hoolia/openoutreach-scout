"""The send contract: newest human LinkedIn: note, Odoo pre-send check, pacing, label removal."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from oo_scout import guard
from oo_scout.browser import WarningPage
from oo_scout.pacing import MESSAGE, PacingRefused
from oo_scout.send import LABEL, SendContract, pick_note

HUMAN, BOT_USER = 7, 99


def note(id, content, *, private=True, sender_type="User", sender_id=HUMAN, message_type=1):
    return {"id": id, "content": content, "private": private, "message_type": message_type,
            "sender_type": sender_type, "sender_id": sender_id, "sender": {"id": sender_id}}


EXCL = frozenset({BOT_USER})


def test_pick_newest_human_private_linkedin_note():
    msgs = [
        note(1, "LinkedIn: old draft"),
        note(2, "LinkedIn: richard's draft", sender_id=BOT_USER),
        note(3, "LinkedIn: from the agent bot", sender_type="AgentBot", sender_id=1),
        note(4, "LinkedIn: public reply", private=False),
        note(5, "LinkedIn: incoming", message_type=0, sender_type="Contact"),
        note(6, "Not a LinkedIn note"),
        note(8, "LinkedIn:   "),
    ]
    assert pick_note(msgs, EXCL).id == 1
    msgs.append(note(9, "LinkedIn: Hi Jane, approved text"))
    picked = pick_note(msgs, EXCL)
    assert (picked.id, picked.text) == (9, "Hi Jane, approved text")


def test_no_note_without_human_author():
    assert pick_note([note(2, "LinkedIn: x", sender_id=BOT_USER)], EXCL) is None
    assert pick_note([note(2, "LinkedIn: x", sender_id=None)], EXCL) is None


class FakeChatwoot:
    def __init__(self, convs, msgs):
        self.convs, self.msgs, self.removed = convs, msgs, []

    def conversations_with_label(self, label):
        assert label == LABEL
        return self.convs

    def messages(self, cid):
        return self.msgs[cid]

    def remove_label(self, cid, label):
        self.removed.append((cid, label))


class FakeOdoo:
    def __init__(self, leads):
        self.leads, self.writes = leads, []

    def read_lead(self, lead_id):
        return self.leads.get(lead_id)

    def mark_sent(self, lead_id, note_id):
        self.writes.append((lead_id, note_id))
        self.leads[lead_id]["x_linkedin_sent_note_id"] = str(note_id)


class FakeUpstream:
    def __init__(self, degree):
        self.degree = degree
        self.visits = []

    def scrape_profile(self, session, profile):
        return {"url": profile["url"], "public_identifier": "jane", "urn": "urn:li:fsd_profile:1",
                "connection_degree": self.degree}, {}

    def visit_profile(self, session, profile):
        self.visits.append(profile["url"])


class FakeSession:
    def __init__(self):
        self.warn = False

    def check(self):
        if self.warn:
            raise WarningPage("checkpoint", "https://www.linkedin.com/checkpoint/")


def settings(may_send=True):
    return SimpleNamespace(may_send=may_send, chatwoot_lead_attribute="odoo_lead_id",
                           chatwoot_excluded_sender_ids=EXCL)


def make(pacer, degree=1, sent_note_id=False, may_send=True):
    cw = FakeChatwoot([{"id": 11, "labels": [LABEL], "custom_attributes": {"odoo_lead_id": 42}}],
                      {11: [note(5, "LinkedIn: Hi Jane")]})
    odoo = FakeOdoo({42: {"id": 42, "x_linkedin_profile": "https://www.linkedin.com/in/jane",
                          "x_linkedin_sent_note_id": sent_note_id}})
    up = FakeUpstream(degree)
    contract = SendContract(settings(may_send), cw, odoo, pacer, FakeSession(), upstream=up)
    return contract, cw, odoo, up


@pytest.fixture
def senders(guarded, monkeypatch):
    calls = []

    def fake_message(session, profile, text):
        calls.append(("message", profile["public_identifier"], text))
        return True

    def fake_connect(session, profile):
        calls.append(("connect", profile["public_identifier"]))
        return "Pending"

    monkeypatch.setitem(guard._originals, ("linkedin_cli.actions.message", "send_raw_message"), fake_message)
    monkeypatch.setitem(guard._originals, ("linkedin_cli.actions.connect", "send_connection_request"), fake_connect)
    return calls


def test_contract_refused_when_sending_disabled(guarded, pacer):
    with pytest.raises(guard.ReadOnlyViolation):
        make(pacer, may_send=False)


def test_message_to_connection_then_odoo_then_label(senders, pacer):
    contract, cw, odoo, _up = make(pacer, degree=1)
    [out] = contract.run_once()
    assert out.result == "message_sent"
    assert senders == [("message", "jane", "Hi Jane")]
    assert odoo.writes == [(42, 5)]
    assert cw.removed == [(11, LABEL)]
    assert pacer.ledger.note_sent(5)


def test_already_sent_note_is_skipped(senders, pacer):
    contract, cw, odoo, _up = make(pacer, sent_note_id="5")
    [out] = contract.run_once()
    assert out.result == "already_sent"
    assert senders == [] and odoo.writes == []
    assert cw.removed == [(11, LABEL)]


def test_local_ledger_prevents_double_send_after_odoo_failure(senders, pacer):
    contract, cw, odoo, _up = make(pacer)
    pacer.ledger.mark_note_sent(5)  # sent before, Odoo write lost
    [out] = contract.run_once()
    assert out.result == "already_sent" and senders == []
    assert odoo.writes == [(42, 5)]  # the lost Odoo write is retried


def test_not_connected_sends_connect_once_and_keeps_label(senders, pacer):
    contract, cw, odoo, up = make(pacer, degree=2)
    assert contract.run_once()[0].result == "connect_sent"
    assert contract.run_once()[0].result == "awaiting_acceptance"
    assert senders == [("connect", "jane")]
    assert cw.removed == [] and odoo.writes == []


def test_message_cap_stops_sending(senders, pacer):
    contract, *_ = make(pacer)
    for _ in range(15):
        pacer.acquire(MESSAGE)
    with pytest.raises(PacingRefused):
        contract.run_once()
    assert senders == []


def test_no_lead_attribute_sends_nothing(senders, pacer):
    contract, cw, odoo, _ = make(pacer)
    cw.convs[0]["custom_attributes"] = {}
    assert contract.run_once()[0].result == "no_lead"
    assert senders == []


def test_warning_page_after_message_does_not_cause_a_resend(senders, pacer):
    contract, cw, odoo, _up = make(pacer, degree=1)
    real = guard._originals[("linkedin_cli.actions.message", "send_raw_message")]

    def send_then_warn(session, profile, text):
        out = real(session, profile, text)
        session.warn = True
        return out

    contract.senders["send_raw_message"] = send_then_warn
    with pytest.raises(WarningPage):
        contract.run_once()
    assert pacer.ledger.note_sent(5) and odoo.writes == [(42, 5)] and cw.removed == [(11, LABEL)]
    contract.session.warn = False
    assert contract.run_once()[0].result == "already_sent"
    assert senders == [("message", "jane", "Hi Jane")]


def test_warning_page_after_connect_keeps_it_pending(senders, pacer):
    contract, cw, odoo, _up = make(pacer, degree=2)
    real = guard._originals[("linkedin_cli.actions.connect", "send_connection_request")]

    def connect_then_warn(session, profile):
        out = real(session, profile)
        session.warn = True
        return out

    contract.senders["send_connection_request"] = connect_then_warn
    with pytest.raises(WarningPage):
        contract.run_once()
    contract.session.warn = False
    assert contract.run_once()[0].result == "awaiting_acceptance"
    assert senders == [("connect", "jane")]
