"""Stop-hook paced-wait exemption (Story 1.2 run postmortem, 2026-07-14).

The wait-ladder ends the orchestrator's turn after every ScheduleWakeup, but
the hook blocked the marker-owning session at every turn-end while stories
remained — each block re-invoked the session immediately, cancelling the
wakeup it had just scheduled (35 blocks in 19 minutes; the Story 1.1 run only
escaped by falling back to forbidden in-turn sleep loops). The hook must let
the turn end while a persisted `waitSession` plus a fresh marker heartbeat
prove the pacing loop is alive, and resume blocking once the heartbeat goes
stale so crash recovery still engages.
"""

from __future__ import annotations

import io
import json
import os
from datetime import datetime, timedelta, timezone

from story_automator.commands.basic import (
    PACED_WAIT_STALE_SECONDS,
    _heartbeat_age_seconds,
    _paced_wait_active,
    cmd_stop_hook,
)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _state_doc(tmp_path, wait_session: str) -> str:
    state = tmp_path / "orchestration-1.md"
    state.write_text(
        "---\n"
        "epic: \"1\"\n"
        "status: IN_PROGRESS\n"
        f"waitSession: {wait_session}\n"
        "waitKind: create\n"
        "waitPolls: 3\n"
        "---\n",
        encoding="utf-8",
    )
    return str(state)


def _payload(tmp_path, wait_session: str = "sa-acme-e1-s1-2-create", heartbeat: str | None = None) -> dict:
    return {
        "epic": "1",
        "currentStory": "1.2",
        "storiesRemaining": 1,
        "stateFile": _state_doc(tmp_path, wait_session),
        "heartbeat": heartbeat if heartbeat is not None else _iso(_now()),
        "pid": os.getpid(),
    }


class TestPacedWaitActive:
    def test_fresh_heartbeat_and_wait_session_is_active(self, tmp_path):
        assert _paced_wait_active(_payload(tmp_path)) is True

    def test_empty_wait_session_is_not_active(self, tmp_path):
        assert _paced_wait_active(_payload(tmp_path, wait_session="")) is False

    def test_stale_heartbeat_is_not_active(self, tmp_path):
        stale = _iso(_now() - timedelta(seconds=PACED_WAIT_STALE_SECONDS + 60))
        assert _paced_wait_active(_payload(tmp_path, heartbeat=stale)) is False

    def test_missing_heartbeat_is_not_active(self, tmp_path):
        assert _paced_wait_active(_payload(tmp_path, heartbeat="")) is False

    def test_missing_state_file_is_not_active(self, tmp_path):
        payload = _payload(tmp_path)
        payload["stateFile"] = str(tmp_path / "does-not-exist.md")
        assert _paced_wait_active(payload) is False

    def test_no_state_file_key_is_not_active(self, tmp_path):
        payload = _payload(tmp_path)
        del payload["stateFile"]
        assert _paced_wait_active(payload) is False

    def test_epoch_heartbeat_is_accepted(self, tmp_path):
        payload = _payload(tmp_path, heartbeat=str(int(_now().timestamp())))
        assert _paced_wait_active(payload) is True

    def test_quoted_and_commented_wait_session_parses(self, tmp_path):
        payload = _payload(tmp_path)
        state = tmp_path / "orchestration-1.md"
        state.write_text('---\nwaitSession: "sa-x-create"   # pending\n---\n', encoding="utf-8")
        assert _paced_wait_active(payload) is True

    def test_comment_only_wait_session_is_empty(self, tmp_path):
        payload = _payload(tmp_path)
        state = tmp_path / "orchestration-1.md"
        state.write_text('---\nwaitSession:    # cleared\n---\n', encoding="utf-8")
        assert _paced_wait_active(payload) is False


class TestHeartbeatAge:
    def test_iso_z_format(self):
        age = _heartbeat_age_seconds(_iso(_now() - timedelta(seconds=120)))
        assert age is not None and 115 <= age <= 130

    def test_garbage_returns_none(self):
        assert _heartbeat_age_seconds("not-a-timestamp") is None
        assert _heartbeat_age_seconds(None) is None


class TestCmdStopHook:
    """End-to-end through cmd_stop_hook with a real marker in cwd."""

    def _run(self, tmp_path, monkeypatch, capsys, marker: dict | None):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("STORY_AUTOMATOR_CHILD", raising=False)
        monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
        if marker is not None:
            claude_dir = tmp_path / ".claude"
            claude_dir.mkdir(exist_ok=True)
            (claude_dir / ".story-automator-active").write_text(json.dumps(marker), encoding="utf-8")
        exit_code = cmd_stop_hook([])
        return exit_code, capsys.readouterr().out

    def test_paced_wait_allows_the_orchestrator_to_stop(self, tmp_path, monkeypatch, capsys):
        code, out = self._run(tmp_path, monkeypatch, capsys, _payload(tmp_path))
        assert code == 0
        assert out == ""

    def test_no_wait_state_still_blocks_the_orchestrator(self, tmp_path, monkeypatch, capsys):
        code, out = self._run(tmp_path, monkeypatch, capsys, _payload(tmp_path, wait_session=""))
        assert code == 0
        assert json.loads(out)["decision"] == "block"

    def test_stale_heartbeat_blocks_for_recovery(self, tmp_path, monkeypatch, capsys):
        stale = _iso(_now() - timedelta(seconds=PACED_WAIT_STALE_SECONDS + 60))
        code, out = self._run(tmp_path, monkeypatch, capsys, _payload(tmp_path, heartbeat=stale))
        assert code == 0
        assert json.loads(out)["decision"] == "block"

    def test_no_marker_allows_stop(self, tmp_path, monkeypatch, capsys):
        code, out = self._run(tmp_path, monkeypatch, capsys, None)
        assert code == 0
        assert out == ""
