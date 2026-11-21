"""The LinkedIn send contract (README, Send contract), off unless READ_ONLY=false and SEND_ENABLED=true.

For each Chatwoot conversation labelled ``send-linkedin``:

1. Take the newest *private* note written by a human staff user (sender type ``User``,
   not on the excluded list) whose text starts with ``LinkedIn:``. Without one, nothing
   is sent.
2. Read the Odoo lead named by the conversation's custom attribute (default
   ``odoo_lead_id``). If ``x_linkedin_sent_note_id`` already holds this note's id, or
   the local ledger has it, the note was sent: remove the label and stop.
3. Open the lead's ``x_linkedin_profile``. A first-degree connection gets the text as a
   message. Anyone else gets a connection request without a note (upstream has no note
   path); the label stays and the message follows on a later pass once they accept.
4. After a message: record the note id in the ledger, then on the lead
   (``x_linkedin_sent_note_id``, ``x_linkedin_message_sent``), then remove the label.

Every step goes through the pacer: message 15/day, connect 10/day and 80/week, and the
page view of each profile counts against the 50/day.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from . import guard
from .pacing import CONNECT, MESSAGE, PAGE_VIEW, Pacer
from .signals import profile_url

logger = logging.getLogger(__name__)

LABEL = "send-linkedin"
PREFIX = "LinkedIn:"
MAX_MESSAGE = 8000
PRIVATE_NOTE_TYPE = 1  # Chatwoot message_type outgoing; private notes are outgoing + private


@dataclass(frozen=True)
class Note:
    id: int
    text: str


def pick_note(messages: list[dict[str, Any]], excluded_sender_ids: frozenset[int]) -> Note | None:
    """The newest human-authored private note starting with ``LinkedIn:``, or None."""
    for m in sorted(messages, key=lambda m: m.get("id", 0), reverse=True):
        if m.get("private") is not True or m.get("message_type") not in (PRIVATE_NOTE_TYPE, "outgoing"):
            continue
        sender = m.get("sender") or {}
        sender_type = str(m.get("sender_type") or sender.get("type") or "").lower()
        if sender_type != "user":
            continue  # AgentBot, Contact or unknown
        sender_id = m.get("sender_id", sender.get("id"))
        if sender_id is None or int(sender_id) in excluded_sender_ids:
            continue
        content = (m.get("content") or "").strip()
        if not content.startswith(PREFIX):
            continue
        text = content[len(PREFIX):].strip()
        if not text or len(text) > MAX_MESSAGE:
            continue
        return Note(int(m["id"]), text)
    return None


@dataclass
class Outcome:
    conversation_id: int
    result: str
    note_id: int | None = None


class SendContract:
    def __init__(self, settings: Any, chatwoot: Any, odoo: Any, pacer: Pacer, session: Any,
                 upstream: Any = None) -> None:
        # raises ReadOnlyViolation unless READ_ONLY=false and SEND_ENABLED=true
        self.senders = guard.allow_send(settings)
        self.settings = settings
        self.chatwoot = chatwoot
        self.odoo = odoo
        self.pacer = pacer
        self.session = session
        self.up = upstream

    def _scrape(self, url: str) -> dict | None:
        if self.up is not None:
            return self.up.scrape_profile(self.session, {"url": url})[0]
        from linkedin_cli.actions.profile import scrape_profile
        from linkedin_cli.url_utils import url_to_public_id

        return scrape_profile(self.session, {"url": url, "public_identifier": url_to_public_id(url)})[0]

    def _visit(self, profile: dict) -> None:
        if self.up is not None:
            return self.up.visit_profile(self.session, profile)
        from linkedin_cli.actions.search import visit_profile

        visit_profile(self.session, profile)

    def run_once(self) -> list[Outcome]:
        out = []
        for conv in self.chatwoot.conversations_with_label(LABEL):
            out.append(self.handle(conv))
            logger.info("send-linkedin conversation %s: %s", out[-1].conversation_id, out[-1].result)
        return out

    def handle(self, conv: dict[str, Any]) -> Outcome:
        cid = int(conv["id"])
        note = pick_note(self.chatwoot.messages(cid), self.settings.chatwoot_excluded_sender_ids)
        if note is None:
            return Outcome(cid, "no_approved_note")
        lead_ref = (conv.get("custom_attributes") or {}).get(self.settings.chatwoot_lead_attribute)
        try:
            lead_id = int(lead_ref)
        except (TypeError, ValueError):
            return Outcome(cid, "no_lead", note.id)
        lead = self.odoo.read_lead(lead_id)
        if not lead:
            return Outcome(cid, "no_lead", note.id)
        ledger = self.pacer.ledger
        odoo_marked = str(lead.get("x_linkedin_sent_note_id") or "") == str(note.id)
        if odoo_marked or ledger.note_sent(note.id):
            if not ledger.note_sent(note.id):
                ledger.mark_note_sent(note.id)
            if not odoo_marked:
                self.odoo.mark_sent(lead_id, note.id)
            self.chatwoot.remove_label(cid, LABEL)
            return Outcome(cid, "already_sent", note.id)
        target = profile_url(lead.get("x_linkedin_profile") or "")
        if not target:
            return Outcome(cid, "no_profile", note.id)

        self.pacer.acquire(PAGE_VIEW)
        profile = self._scrape(target)
        self.session.check()
        if not profile:
            return Outcome(cid, "profile_unavailable", note.id)

        if profile.get("connection_degree") == 1:
            self.pacer.acquire(MESSAGE)
            sent = self.senders["send_raw_message"](self.session, profile, note.text)
            if not sent:
                self.session.check()
                return Outcome(cid, "send_failed", note.id)
            ledger.mark_note_sent(note.id)  # first: the local record survives an Odoo outage
            self.odoo.mark_sent(lead_id, note.id)
            self.chatwoot.remove_label(cid, LABEL)
            self.session.check()  # after the records: a warning page must not cause a resend
            return Outcome(cid, "message_sent", note.id)

        pending = ledger.data.setdefault("pending_connects", {})
        if target in pending:
            return Outcome(cid, "awaiting_acceptance", note.id)
        self.pacer.acquire(PAGE_VIEW)
        self._visit(profile)
        self.pacer.acquire(CONNECT)
        state = self.senders["send_connection_request"](self.session, profile)
        pending[target] = {"note_id": note.id, "state": str(state)}
        ledger.save()
        self.session.check()
        return Outcome(cid, "connect_sent", note.id)
