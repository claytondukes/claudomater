"""Story 24-7 AC2 — unit tests for `validate-completion`.

Contract:
- `orchestrator-helper validate-completion --state <path>` reads
  storyRange from the state doc's flow-style array, computes required
  stepsCompleted entries (preflight x3 + per-story x3 + step-03c), and
  emits {ok: true|false, missing: [...]} (exit 0/1).
- `--story-range CSV` overrides (used by synthetic tests only).
- step-04-wrapup is intentionally excluded — validate is called from
  inside step-04 before it self-appends.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

import pytest

from story_automator.commands.orchestrator import _validate_completion


def _run(args: list[str]) -> tuple[int, dict]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = _validate_completion(args)
    return code, json.loads(buf.getvalue())


def _state_doc(tmp_path, *, story_range: str, steps_completed: str) -> str:
    body = (
        f"storyRange: {story_range}\n"
        f"stepsCompleted: {steps_completed}\n"
        "status: READY\n"
    )
    p = tmp_path / "state.md"
    p.write_text(body, encoding="utf-8")
    return str(p)


def _all_required_for(story_ids: list[str]) -> list[str]:
    entries = [
        "step-02-preflight",
        "step-02a-preflight-config",
        "step-02b-preflight-finalize",
    ]
    for sid in story_ids:
        entries += [
            f"step-03-execute:{sid}",
            f"step-03a-execute-review:{sid}",
            f"step-03b-execute-finish:{sid}",
        ]
    entries.append("step-03c-execute-complete")
    return entries


class TestValidateCompletion:
    def test_all_present_ok_true(self, tmp_path) -> None:
        ids = ["24-1", "24-2"]
        completed = _all_required_for(ids)
        state = _state_doc(
            tmp_path,
            story_range="[24-1, 24-2]",
            steps_completed=f"[{', '.join(completed)}]",
        )
        code, out = _run(["--state", state])
        assert code == 0
        assert out == {"ok": True, "missing": []}

    def test_one_preflight_missing(self, tmp_path) -> None:
        ids = ["24-1"]
        completed = [e for e in _all_required_for(ids) if e != "step-02a-preflight-config"]
        state = _state_doc(
            tmp_path,
            story_range="[24-1]",
            steps_completed=f"[{', '.join(completed)}]",
        )
        code, out = _run(["--state", state])
        assert code == 1
        assert out["ok"] is False
        assert out["missing"] == ["step-02a-preflight-config"]

    def test_one_per_story_missing(self, tmp_path) -> None:
        ids = ["15-2", "15-3"]
        completed = [
            e for e in _all_required_for(ids)
            if e != "step-03b-execute-finish:15-3"
        ]
        state = _state_doc(
            tmp_path,
            story_range="[15-2, 15-3]",
            steps_completed=f"[{', '.join(completed)}]",
        )
        code, out = _run(["--state", state])
        assert code == 1
        assert out["missing"] == ["step-03b-execute-finish:15-3"]

    def test_empty_steps_completed_lists_every_required(self, tmp_path) -> None:
        state = _state_doc(
            tmp_path,
            story_range="[24-7]",
            steps_completed="[]",
        )
        code, out = _run(["--state", state])
        assert code == 1
        assert out["ok"] is False
        assert out["missing"] == _all_required_for(["24-7"])

    def test_state_doc_missing_returns_structured_error(self, tmp_path) -> None:
        code, out = _run(["--state", str(tmp_path / "nope.md")])
        assert code == 1
        assert out["ok"] is False
        assert out["error"] == "file_not_found"

    def test_step_04_wrapup_not_required(self, tmp_path) -> None:
        """Calling site is step-04 itself; it appends after validate passes."""
        ids = ["24-7"]
        completed = _all_required_for(ids)  # explicitly omits step-04-wrapup
        state = _state_doc(
            tmp_path,
            story_range="[24-7]",
            steps_completed=f"[{', '.join(completed)}]",
        )
        code, out = _run(["--state", state])
        assert code == 0

    def test_story_range_override_csv(self, tmp_path) -> None:
        """--story-range CSV overrides state doc value (synthetic-test path)."""
        # State doc says [24-1, 24-2] but we override to [99-1] — required
        # set should be computed from override.
        completed = _all_required_for(["99-1"])
        state = _state_doc(
            tmp_path,
            story_range="[24-1, 24-2]",
            steps_completed=f"[{', '.join(completed)}]",
        )
        code, out = _run(["--state", state, "--story-range", "99-1"])
        assert code == 0

    def test_state_path_required(self, tmp_path) -> None:
        code, out = _run([])
        assert code == 1
        assert out["error"] == "state_path_required"

    def test_story_range_block_style_fails_loudly(self, tmp_path) -> None:
        body = "storyRange:\n  - 24-1\nstepsCompleted: []\n"
        p = tmp_path / "state.md"
        p.write_text(body, encoding="utf-8")
        code, out = _run(["--state", str(p)])
        assert code == 1
        assert out["error"] == "story_range_parse_failed"
