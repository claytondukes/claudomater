"""Story 24-10 AC7 — integration test for the pane-watcher.

Spins up a real tmux session that prints the elicitation prompt and
blocks on `read`. Calls the watcher; asserts that:

1. With `AUTO_ANSWER_ELICITATION=true` set on the session, the
   `read` unblocks (proving `y\\n` was sent), the state-JSON
   debounce field is populated, and the state-doc Action Log gains
   an `AUTO-ANSWER:` entry.
2. With the env var unset / false, the `read` keeps blocking (the
   gate is real and reversible), and the state-doc gains an
   `AUTO-ANSWER-SKIPPED:` entry instead.

Skipped automatically when `tmux` is not on PATH (so unit-only CI
runs still pass). Run locally with:

    PYTHONPATH=src python -m pytest tests/test_auto_answer_integration.py -v
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from story_automator.core.tmux_runtime import (
    SessionPaths,
    _maybe_auto_answer_elicitation_prompt,
    load_session_state,
)


pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None,
    reason="tmux not installed; AC7 integration test requires real tmux",
)


def _tmux(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _session_alive(session: str) -> bool:
    return _tmux("has-session", "-t", session).returncode == 0


@pytest.fixture
def tmux_session(tmp_path: Path):
    """Spawn a tmux session that prints the elicitation prompt and
    blocks on `read`. Yield (session_name, paths, state_doc_file).
    Tears the session down on test exit.
    """
    session = f"sa-test-24-10-{uuid.uuid4().hex[:8]}"
    paths = SessionPaths(
        state=tmp_path / "session.state.json",
        command=tmp_path / "session.cmd",
        runner=tmp_path / "session.runner",
        output=tmp_path / "session.out",
    )
    state_doc = tmp_path / "orchestration-test.md"
    state_doc.write_text(
        "---\nstatus: IN_PROGRESS\n---\n\n## Action Log\n\n"
        "<!-- entries appended here -->\n"
    )

    yield session, paths, state_doc

    if _session_alive(session):
        _tmux("kill-session", "-t", session)


def _spawn_blocking_prompt_session(session: str, *, env: dict[str, str]) -> None:
    """Start a tmux session running `bash -c 'echo PROMPT; read; echo OK'`."""
    args = ["new-session", "-d", "-s", session, "-x", "200", "-y", "50"]
    for key, value in env.items():
        args += ["-e", f"{key}={value}"]
    args += [
        "bash",
        "-c",
        "echo 'Apply all 3 edits? (y/n)'; read answer; echo \"got: $answer\"",
    ]
    result = _tmux(*args)
    assert result.returncode == 0, f"tmux new-session failed: {result.stderr}"
    # Give bash a moment to print the prompt before the watcher runs.
    time.sleep(0.5)


def test_watcher_sends_y_when_gate_is_on(tmux_session) -> None:
    """AC4 + AC5 — gate on, match, send `y`, log entry written."""
    session, paths, state_doc = tmux_session

    _spawn_blocking_prompt_session(
        session,
        env={
            "AUTO_ANSWER_ELICITATION": "true",
            "STATE_DOC_FILE": str(state_doc),
        },
    )

    pre_text = _tmux("capture-pane", "-t", session, "-p").stdout
    assert "Apply all 3 edits?" in pre_text, (
        f"prompt did not render before watcher ran; pane=\n{pre_text}"
    )

    _maybe_auto_answer_elicitation_prompt(session, paths, state={})

    # Give bash time to consume `y\n` and run the trailing `echo got:`.
    for _ in range(20):
        time.sleep(0.1)
        if not _session_alive(session):
            break

    post_text = _tmux("capture-pane", "-t", session, "-p", "-S", "-50").stdout
    # bash already exited and tmux session torn down by `remain-on-exit`
    # default (off in our spawn). Treat "got: y" being absent only if
    # the session is still alive — otherwise success.
    if _session_alive(session):
        assert "got: y" in post_text, (
            f"watcher did not unblock the read; pane=\n{post_text}"
        )

    state_after = load_session_state(paths.state)
    assert state_after.get("autoAnswerLastPrompt") == "Apply all 3 edits? (y/n)"
    assert state_after.get("autoAnswerLastAt")

    log_text = state_doc.read_text()
    assert "AUTO-ANSWER:" in log_text
    assert "Apply all 3 edits? (y/n)" in log_text
    assert 'sent="y\\n"' in log_text


def test_watcher_skips_when_gate_is_off(tmux_session) -> None:
    """AC3 + AC9 — gate off, match, log SKIPPED, no `y` sent."""
    session, paths, state_doc = tmux_session

    _spawn_blocking_prompt_session(
        session,
        env={
            "AUTO_ANSWER_ELICITATION": "false",
            "STATE_DOC_FILE": str(state_doc),
        },
    )

    _maybe_auto_answer_elicitation_prompt(session, paths, state={})

    # 0.5s grace — if `y` had been sent, bash would exit. It must NOT.
    time.sleep(0.5)
    assert _session_alive(session), (
        "watcher unexpectedly answered the prompt while gate was off"
    )
    pane = _tmux("capture-pane", "-t", session, "-p").stdout
    assert "got:" not in pane, f"read unexpectedly unblocked; pane=\n{pane}"

    state_after = load_session_state(paths.state)
    assert state_after.get("autoAnswerSkippedPrompt") == "Apply all 3 edits? (y/n)"
    assert not state_after.get("autoAnswerLastPrompt"), (
        "send-path stash should not be touched when gate is off"
    )

    log_text = state_doc.read_text()
    assert "AUTO-ANSWER-SKIPPED:" in log_text
    assert "Apply all 3 edits? (y/n)" in log_text


def test_watcher_is_noop_when_no_match(tmux_session) -> None:
    """No whitelist match → no state mutation, no log entry, no send."""
    session, paths, state_doc = tmux_session

    args = ["new-session", "-d", "-s", session, "-x", "200", "-y", "50"]
    args += ["-e", "AUTO_ANSWER_ELICITATION=true"]
    args += ["-e", f"STATE_DOC_FILE={state_doc}"]
    args += ["bash", "-c", "echo 'just a normal line'; sleep 30"]
    result = _tmux(*args)
    assert result.returncode == 0
    time.sleep(0.3)

    _maybe_auto_answer_elicitation_prompt(session, paths, state={})

    state_after = load_session_state(paths.state)
    assert not state_after, f"state should be empty on no-match; got {state_after}"

    log_text = state_doc.read_text()
    assert "AUTO-ANSWER" not in log_text


def test_watcher_debounces_repeat_match(tmux_session) -> None:
    """AC4 — same literal match within debounce window does NOT re-send."""
    session, paths, state_doc = tmux_session

    _spawn_blocking_prompt_session(
        session,
        env={
            "AUTO_ANSWER_ELICITATION": "true",
            "STATE_DOC_FILE": str(state_doc),
        },
    )

    # First call: sends `y`, persists debounce state.
    _maybe_auto_answer_elicitation_prompt(session, paths, state={})
    first_state = load_session_state(paths.state)
    first_at = first_state.get("autoAnswerLastAt")

    # Simulate a follow-up poll within the debounce window. Recreate
    # the stalled prompt visually (bash exited after first `y`; spawn
    # a NEW session with the same prompt) so the match would otherwise
    # fire again. The watcher should see the debounce stash and skip.
    if not _session_alive(session):
        _spawn_blocking_prompt_session(
            session,
            env={
                "AUTO_ANSWER_ELICITATION": "true",
                "STATE_DOC_FILE": str(state_doc),
            },
        )

    _maybe_auto_answer_elicitation_prompt(session, paths, state=first_state)
    second_state = load_session_state(paths.state)
    # The debounce timestamp must be unchanged (no re-send happened).
    assert second_state.get("autoAnswerLastAt") == first_at, (
        "debounce window failed; watcher re-sent within 30s"
    )
