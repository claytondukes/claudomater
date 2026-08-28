"""Slack notifications (user-level webhook).

Fires on PAUSED-QUOTA, DEGRADED, ESCALATED, RUN-COMPLETE, and PROMPT-BLOCKED,
at the moment the state change happens — overnight runs must not save their
bad news for the morning. Notification failures are reported to the caller
(for the run log) but never raised: losing a Slack message must not kill a
run, and a missing webhook just disables notify.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable

from claudomater.config import UserConfig

PAUSED_QUOTA = "PAUSED-QUOTA"
DEGRADED = "DEGRADED"
ESCALATED = "ESCALATED"
RUN_COMPLETE = "RUN-COMPLETE"
PROMPT_BLOCKED = "PROMPT-BLOCKED"

KINDS = (PAUSED_QUOTA, DEGRADED, ESCALATED, RUN_COMPLETE, PROMPT_BLOCKED)

# Transport: callable(url, body_bytes) -> HTTP status code.
TransportFn = Callable[[str, bytes], int]


def _default_transport(url: str, body: bytes) -> int:
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
        return resp.status


class Notifier:
    def __init__(
        self,
        webhook_url: str | None,
        transport: TransportFn | None = None,
    ):
        self.webhook_url = webhook_url
        self._transport = transport or _default_transport
        self.last_error: str | None = None

    @classmethod
    def from_user_config(
        cls, cfg: UserConfig, transport: TransportFn | None = None
    ) -> "Notifier":
        return cls(cfg.slack_webhook, transport=transport)

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url)

    def notify(
        self,
        kind: str,
        message: str,
        project: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> bool:
        """Send one notification. Returns True on delivery; False when
        disabled or delivery failed (reason in self.last_error)."""
        if kind not in KINDS:
            raise ValueError(f"unknown notification kind {kind!r} (known: {KINDS})")
        self.last_error = None
        if not self.webhook_url:
            self.last_error = "notify disabled: no slack webhook configured"
            return False

        prefix = f"[{kind}]"
        if project:
            prefix += f" {project}:"
        text = f"{prefix} {message}"
        if detail:
            text += "\n```" + json.dumps(detail, indent=2, sort_keys=True) + "```"

        body = json.dumps({"text": text}).encode("utf-8")
        try:
            status = self._transport(self.webhook_url, body)
        except (urllib.error.URLError, OSError) as exc:
            self.last_error = f"notify failed: {exc}"
            return False
        if not 200 <= status < 300:
            self.last_error = f"notify failed: webhook returned {status}"
            return False
        return True
