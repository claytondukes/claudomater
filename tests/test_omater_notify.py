"""Slack notifier: payload shape, kinds, and non-raising failure modes."""

from __future__ import annotations

import json

import pytest

from claudomater.config import UserConfig
from claudomater.notify import KINDS, Notifier, PAUSED_QUOTA, RUN_COMPLETE


class RecordingTransport:
    def __init__(self, status=200, exc=None):
        self.status = status
        self.exc = exc
        self.sent: list[tuple[str, dict]] = []

    def __call__(self, url, body):
        if self.exc:
            raise self.exc
        self.sent.append((url, json.loads(body)))
        return self.status


def test_notify_posts_kind_project_and_message():
    transport = RecordingTransport()
    notifier = Notifier("https://hooks.example/x", transport=transport)
    assert notifier.notify(PAUSED_QUOTA, "7d window at 96%", project="demo")
    url, payload = transport.sent[0]
    assert url == "https://hooks.example/x"
    assert payload["text"].startswith("[PAUSED-QUOTA] demo:")
    assert "96%" in payload["text"]


def test_detail_is_rendered_as_code_block():
    transport = RecordingTransport()
    notifier = Notifier("https://hooks.example/x", transport=transport)
    notifier.notify(RUN_COMPLETE, "run done", detail={"stories": 3})
    _, payload = transport.sent[0]
    assert '"stories": 3' in payload["text"]


def test_unknown_kind_raises():
    notifier = Notifier("https://hooks.example/x", transport=RecordingTransport())
    with pytest.raises(ValueError, match="unknown notification kind"):
        notifier.notify("PAUSED", "msg")


def test_all_designed_kinds_are_present():
    assert set(KINDS) == {
        "PAUSED-QUOTA",
        "DEGRADED",
        "ESCALATED",
        "RUN-COMPLETE",
        "PROMPT-BLOCKED",
    }


def test_missing_webhook_disables_without_raising():
    notifier = Notifier(None)
    assert not notifier.enabled
    assert notifier.notify(RUN_COMPLETE, "msg") is False
    assert "no slack webhook" in notifier.last_error


def test_transport_error_is_reported_not_raised():
    notifier = Notifier(
        "https://hooks.example/x", transport=RecordingTransport(exc=OSError("boom"))
    )
    assert notifier.notify(RUN_COMPLETE, "msg") is False
    assert "boom" in notifier.last_error


def test_non_2xx_is_a_failure():
    notifier = Notifier("https://hooks.example/x", transport=RecordingTransport(status=500))
    assert notifier.notify(RUN_COMPLETE, "msg") is False
    assert "500" in notifier.last_error


def test_from_user_config():
    cfg = UserConfig(slack_webhook="https://hooks.example/y")
    assert Notifier.from_user_config(cfg).webhook_url == "https://hooks.example/y"
