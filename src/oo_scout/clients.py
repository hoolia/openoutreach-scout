"""Chatwoot and Odoo clients, limited to what the send contract needs.

Chatwoot: list conversations with a label, read messages, read and set labels. The
only Chatwoot write is ``remove_label``; there is no message or note method.
Odoo: read one ``crm.lead`` and write the two send fields on it.
"""

from __future__ import annotations

from typing import Any

import httpx


class ClientError(RuntimeError):
    pass


class Chatwoot:
    def __init__(self, url: str, account_id: int, token: str,
                 transport: httpx.BaseTransport | None = None, timeout: float = 30.0) -> None:
        self._http = httpx.Client(
            base_url=f"{url}/api/v1/accounts/{int(account_id)}",
            headers={"api_access_token": token, "Accept": "application/json"},
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
        )

    def _get(self, path: str, params: Any = None) -> Any:
        try:
            r = self._http.get(path, params=params)
        except httpx.HTTPError as exc:
            raise ClientError(f"Chatwoot request failed: {type(exc).__name__}") from exc
        if r.status_code != 200:
            raise ClientError(f"Chatwoot returned HTTP {r.status_code} for GET {path}")
        return r.json()

    def conversations_with_label(self, label: str, max_pages: int = 10) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            body = self._get("/conversations", params=[("assignee_type", "all"), ("status", "all"),
                                                       ("page", page), ("labels[]", label)])
            data = body.get("data", body)
            batch = data.get("payload", [])
            out.extend(c for c in batch if label in (c.get("labels") or []))
            if len(batch) < 25:
                break
        return out

    def messages(self, conversation_id: int, limit: int = 200) -> list[dict[str, Any]]:
        collected: dict[int, dict[str, Any]] = {}
        before: int | None = None
        while len(collected) < limit:
            body = self._get(f"/conversations/{int(conversation_id)}/messages",
                             params={"before": before} if before is not None else None)
            new = [m for m in body.get("payload", []) if m.get("id") not in collected]
            if not new:
                break
            for m in new:
                collected[m["id"]] = m
            before = min(m["id"] for m in new)
        return sorted(collected.values(), key=lambda m: m["id"])

    def labels(self, conversation_id: int) -> list[str]:
        return list(self._get(f"/conversations/{int(conversation_id)}/labels").get("payload", []))

    def remove_label(self, conversation_id: int, label: str) -> list[str]:
        remaining = [x for x in self.labels(conversation_id) if x != label]
        path = f"/conversations/{int(conversation_id)}/labels"
        try:
            r = self._http.post(path, json={"labels": remaining})
        except httpx.HTTPError as exc:
            raise ClientError(f"Chatwoot request failed: {type(exc).__name__}") from exc
        if r.status_code != 200:
            raise ClientError(f"Chatwoot returned HTTP {r.status_code} for POST {path}")
        return remaining


LEAD_FIELDS = ["id", "name", "x_linkedin_profile", "x_linkedin_sent_note_id"]


class Odoo:
    """JSON-RPC against /jsonrpc with a user login and API key."""

    def __init__(self, url: str, db: str, user: str, password: str,
                 transport: httpx.BaseTransport | None = None, timeout: float = 30.0) -> None:
        self._http = httpx.Client(base_url=url, timeout=timeout, transport=transport)
        self.db, self.user, self.password = db, user, password
        self._uid: int | None = None

    def _call(self, service: str, method: str, *args: Any) -> Any:
        payload = {"jsonrpc": "2.0", "method": "call", "id": 1,
                   "params": {"service": service, "method": method, "args": list(args)}}
        try:
            r = self._http.post("/jsonrpc", json=payload)
        except httpx.HTTPError as exc:
            raise ClientError(f"Odoo request failed: {type(exc).__name__}") from exc
        if r.status_code != 200:
            raise ClientError(f"Odoo returned HTTP {r.status_code}")
        body = r.json()
        if body.get("error"):
            msg = (body["error"].get("data") or {}).get("message") or body["error"].get("message")
            raise ClientError(f"Odoo error: {msg}")
        return body.get("result")

    def uid(self) -> int:
        if self._uid is None:
            uid = self._call("common", "login", self.db, self.user, self.password)
            if not uid:
                raise ClientError("Odoo login failed")
            self._uid = int(uid)
        return self._uid

    def _kw(self, model: str, method: str, args: list, kwargs: dict | None = None) -> Any:
        return self._call("object", "execute_kw", self.db, self.uid(), self.password, model, method, args, kwargs or {})

    def read_lead(self, lead_id: int) -> dict[str, Any] | None:
        rows = self._kw("crm.lead", "read", [[int(lead_id)]], {"fields": LEAD_FIELDS})
        return rows[0] if rows else None

    def mark_sent(self, lead_id: int, note_id: int) -> None:
        self._kw("crm.lead", "write", [[int(lead_id)], {"x_linkedin_sent_note_id": str(note_id),
                                                       "x_linkedin_message_sent": True}])
