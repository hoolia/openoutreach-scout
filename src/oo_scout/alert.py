"""Alerts to a Slack incoming webhook; always logged, also without a webhook."""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger("oo_scout.alert")


class Alerter:
    def __init__(self, webhook_url: str = "", transport: httpx.BaseTransport | None = None) -> None:
        self.webhook_url = webhook_url
        self._transport = transport

    def __call__(self, text: str) -> None:
        logger.error("ALERT: %s", text)
        if not self.webhook_url:
            return
        try:
            with httpx.Client(timeout=15, transport=self._transport) as c:
                c.post(self.webhook_url, json={"text": f":warning: LinkedIn scout: {text}"})
        except httpx.HTTPError as exc:
            logger.error("Slack alert failed: %s", type(exc).__name__)
