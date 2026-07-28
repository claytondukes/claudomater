"""Story 24-7 AC1 — unit tests for `state-update --append-array`.

Contract:
- Append flow-style YAML array entries via
  `orchestrator-helper state-update <state> --append-array stepsCompleted=<value>`.
- Idempotent: re-appending a value already present is a no-op.
- Returns JSON with `appended` and `alreadyPresent` for the last append.
- Refuses to touch block-style array lines (returns
  `array_line_not_flow_style` with exit 1).
- Anti-prefix-collision: must not match `stepsCompletedExtra:` when key is
  `stepsCompleted`.
- Existing `--set k=v` semantics are unchanged.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

import pytest

from story_automator.commands.orchestrator import _state_update


def _run(args: list[str]) -> tuple[int, dict]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = _state_update(args)
    return code, json.loads(buf.getvalue())


def _doc(tmp_path, body: str):
    p = tmp_path / "state.md"
    p.write_text(body, encoding="utf-8")
    return p


class TestAppendArray:
    def test_first_append_into_empty_array(self, tmp_path) -> None:
        path = _doc(tmp_path, "stepsCompleted: []\nstatus: READY\n")
        code, out = _run([str(path), "--append-array", "stepsCompleted=step-02-preflight"])
        assert code == 0
        assert out == {
            "ok": True,
            "updated": ["stepsCompleted"],
            "appended": "step-02-preflight",
            "alreadyPresent": False,
        }
        assert "stepsCompleted: [step-02-preflight]\n" in path.read_text()

    def test_idempotent_reappend_same_value(self, tmp_path) -> None:
        path = _doc(tmp_path, "stepsCompleted: [step-02-preflight]\n")
        code, out = _run([str(path), "--append-array", "stepsCompleted=step-02-preflight"])
        assert code == 0
        assert out["alreadyPresent"] is True
        assert out["appended"] == "step-02-preflight"
        # Array unchanged — value written exactly once.
        assert path.read_text().count("step-02-preflight") == 1

    def test_append_into_populated_array(self, tmp_path) -> None:
        path = _doc(tmp_path, "stepsCompleted: [a, b, c]\n")
        code, out = _run([str(path), "--append-array", "stepsCompleted=d"])
        assert code == 0
        assert out["alreadyPresent"] is False
        assert "stepsCompleted: [a, b, c, d]\n" in path.read_text()

    def test_no_prefix_collision_on_similar_key(self, tmp_path) -> None:
        """`stepsCompletedExtra:` must not be touched when key is `stepsCompleted`."""
        body = (
            "stepsCompletedExtra: [should, not, change]\n"
            "stepsCompleted: []\n"
        )
        path = _doc(tmp_path, body)
        code, out = _run([str(path), "--append-array", "stepsCompleted=step-x"])
        assert code == 0
        text = path.read_text()
        assert "stepsCompletedExtra: [should, not, change]\n" in text
        assert "stepsCompleted: [step-x]\n" in text

    def test_block_style_array_rejected(self, tmp_path) -> None:
        """Block-style arrays trigger fail-loud structured error (AC: parser scope)."""
        body = "stepsCompleted:\n  - a\n  - b\n"
        path = _doc(tmp_path, body)
        code, out = _run([str(path), "--append-array", "stepsCompleted=c"])
        assert code == 1
        assert out["ok"] is False
        assert out["error"] == "array_line_not_flow_style"
        assert out["key"] == "stepsCompleted"

    def test_key_missing_returns_error(self, tmp_path) -> None:
        path = _doc(tmp_path, "status: READY\n")
        code, out = _run([str(path), "--append-array", "stepsCompleted=foo"])
        assert code == 1
        assert out["ok"] is False
        assert out["error"] == "key_not_found"

    def test_file_not_found(self, tmp_path) -> None:
        code, out = _run([str(tmp_path / "nope.md"), "--append-array", "stepsCompleted=x"])
        assert code == 1
        assert out["ok"] is False
        assert out["error"] == "file_not_found"

    def test_two_appends_into_same_array_one_invocation(self, tmp_path) -> None:
        """step-02a appends both step-02-preflight and step-02a-preflight-config in one call."""
        path = _doc(tmp_path, "stepsCompleted: []\n")
        code, out = _run([
            str(path),
            "--append-array", "stepsCompleted=step-02-preflight",
            "--append-array", "stepsCompleted=step-02a-preflight-config",
        ])
        assert code == 0
        assert out["ok"] is True
        text = path.read_text()
        assert "stepsCompleted: [step-02-preflight, step-02a-preflight-config]\n" in text

    def test_combined_set_and_append_one_invocation(self, tmp_path) -> None:
        body = "currentStep: step-old\nstepsCompleted: [a]\n"
        path = _doc(tmp_path, body)
        code, out = _run([
            str(path),
            "--set", "currentStep=step-new",
            "--append-array", "stepsCompleted=b",
        ])
        assert code == 0
        assert out["ok"] is True
        assert set(out["updated"]) == {"currentStep", "stepsCompleted"}
        assert out["appended"] == "b"
        assert out["alreadyPresent"] is False
        text = path.read_text()
        assert "currentStep: step-new\n" in text
        assert "stepsCompleted: [a, b]\n" in text


class TestSetUnchanged:
    def test_set_still_replaces_line(self, tmp_path) -> None:
        body = "status: READY\nlastUpdated: old\n"
        path = _doc(tmp_path, body)
        code, out = _run([str(path), "--set", "status=IN_PROGRESS"])
        assert code == 0
        assert out == {"ok": True, "updated": ["status"]}
        assert "status: IN_PROGRESS\n" in path.read_text()

    def test_set_missing_key_returns_error_unchanged(self, tmp_path) -> None:
        path = _doc(tmp_path, "status: READY\n")
        code, out = _run([str(path), "--set", "missing=foo"])
        assert code == 1
        assert out["ok"] is False
