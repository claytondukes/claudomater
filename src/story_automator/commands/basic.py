from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from ..core.utils import (
    ensure_dir,
    file_exists,
    get_project_slug,
    now_utc_z,
    read_text,
    run_cmd,
    write_atomic,
    write_json,
)


def _workflow_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _workflow_doc_relative(doc_name: str) -> str:
    doc_path = _workflow_root() / "data" / doc_name
    project_root = Path(os.environ.get("PROJECT_ROOT") or os.getcwd()).resolve()
    try:
        return str(doc_path.resolve().relative_to(project_root))
    except ValueError:
        return str(doc_path.resolve())


def _stop_hook_command(command: str) -> str:
    command_parts = command.split()
    if not command_parts:
        return command
    candidates = [
        _workflow_root() / "scripts" / "story-automator",
        Path(shutil.which("story-automator")) if shutil.which("story-automator") else None,
        Path(sys.argv[0]).resolve() if Path(sys.argv[0]).exists() and os.access(Path(sys.argv[0]), os.X_OK) else None,
    ]
    for candidate in candidates:
        if candidate and candidate.exists() and os.access(candidate, os.X_OK):
            command_parts[0] = str(candidate)
            return " ".join(command_parts)
    return f"{shutil.which('python3') or 'python3'} -m story_automator {' '.join(command_parts[1:])}".strip()


def cmd_derive_project_slug(args: list[str]) -> int:
    if args and args[0] in {"--help", "-h"}:
        print("Usage: derive-project-slug [--project-root PATH]")
        return 0
    project_root = os.getcwd()
    for idx, arg in enumerate(args):
        if arg == "--project-root" and idx + 1 < len(args):
            project_root = args[idx + 1]
    write_json({"ok": True, "slug": get_project_slug(project_root), "projectRoot": project_root})
    return 0


def cmd_ensure_marker_gitignore(args: list[str]) -> int:
    gitignore = ""
    entry = ""
    for idx, arg in enumerate(args):
        if arg == "--gitignore" and idx + 1 < len(args):
            gitignore = args[idx + 1]
        if arg == "--entry" and idx + 1 < len(args):
            entry = args[idx + 1]
    if not gitignore or not entry:
        write_json({"ok": False, "error": "missing_args"})
        return 1
    path = Path(gitignore)
    if not path.exists():
        path.write_text("")
    content = path.read_text()
    for line in content.replace("\r\n", "\n").split("\n"):
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and stripped == entry:
            write_json({"ok": True, "changed": False, "path": str(path)})
            return 0
    prefix = "" if not content or content.endswith("\n") else "\n"
    with path.open("a") as handle:
        handle.write(f"{prefix}{entry}\n")
    write_json({"ok": True, "changed": True, "path": str(path)})
    return 0


def cmd_ensure_stop_hook(args: list[str]) -> int:
    settings = ""
    command = ""
    timeout = 10
    for idx, arg in enumerate(args):
        if arg == "--settings" and idx + 1 < len(args):
            settings = args[idx + 1]
        elif arg == "--command" and idx + 1 < len(args):
            command = args[idx + 1]
        elif arg == "--timeout" and idx + 1 < len(args):
            timeout = int(args[idx + 1])
    if not settings or not command:
        write_json({"ok": False, "error": "missing_required_args"})
        return 1
    command = _stop_hook_command(command)
    ensure_dir(Path(settings).parent)
    payload = {
        "hooks": {
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": command,
                            "timeout": timeout,
                        }
                    ]
                }
            ]
        }
    }
    path = Path(settings)
    if not path.exists():
        write_atomic(path, json.dumps(payload, indent=2))
        write_json({"ok": True, "changed": True, "reason": "created", "path": str(path)})
        return 0
    try:
        root = json.loads(path.read_text())
    except json.JSONDecodeError:
        write_json({"ok": False, "error": "invalid_json", "path": str(path)})
        return 1
    hooks = root.setdefault("hooks", {})
    stop_hooks = hooks.setdefault("Stop", [])
    exists = False
    needs_update = False
    for entry in stop_hooks:
        for hook in entry.get("hooks", []):
            existing = hook.get("command")
            if existing == command or ("story-automator" in str(existing) and "stop-hook" in str(existing)):
                exists = True
                if existing != command:
                    hook["command"] = command
                    needs_update = True
                if hook.get("timeout") != timeout:
                    hook["timeout"] = timeout
                    needs_update = True
    if exists and not needs_update:
        write_json({"ok": True, "changed": False, "reason": "already_configured", "path": str(path)})
        return 0
    if exists and needs_update:
        write_atomic(path, json.dumps(root, indent=2))
        write_json({"ok": True, "changed": False, "reason": "hook_normalized", "path": str(path)})
        return 0
    stop_hooks.append(payload["hooks"]["Stop"][0])
    write_atomic(path, json.dumps(root, indent=2))
    write_json({"ok": True, "changed": True, "reason": "added", "path": str(path)})
    return 0


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _pid_in_ancestry(target: int) -> bool:
    """True when `target` is this process or one of its ancestors.

    The Stop hook runs as a child of the stopping Claude session, so the
    orchestrator's pid (recorded in the marker) appears in the ancestry
    only when the stopping session IS the orchestrator.
    """
    pid = os.getpid()
    for _ in range(20):
        if pid == target:
            return True
        if pid <= 1:
            return False
        try:
            import subprocess
            out = subprocess.run(
                ["ps", "-o", "ppid=", "-p", str(pid)],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            pid = int(out)
        except (ValueError, OSError, Exception):
            return False
    return False


# Heartbeat age beyond which a persisted wait no longer exempts the stop.
# The ladder's largest gap is 300s and every wake tick refreshes the marker
# heartbeat, so 900s means the pacing loop (wakeup -> check -> heartbeat ->
# re-schedule) has died and blocking must resume for crash recovery.
PACED_WAIT_STALE_SECONDS = 900


def _heartbeat_age_seconds(value: object) -> float | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            stamp = datetime.fromtimestamp(float(raw), tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    return (datetime.now(timezone.utc) - stamp).total_seconds()


def _state_wait_session(state_file: str) -> str:
    try:
        text = Path(state_file).read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in text.splitlines():
        if line.startswith("waitSession:"):
            return line.split(":", 1)[1].split("#", 1)[0].strip().strip("\"'")
    return ""


def _paced_wait_active(payload: dict) -> bool:
    """True when the orchestrator is between wait-ladder polls.

    The wait-ladder (data/wait-ladder.md) has the orchestrator persist
    `waitSession` in the state doc, schedule a wakeup, and end the turn.
    Blocking that turn-end re-invokes the session immediately, cancelling
    the wakeup and collapsing the ladder into a tight poll loop (Story 1.2
    run, 2026-07-14: 35 blocks in 19 minutes). A fresh marker heartbeat
    proves the pacing loop is alive; once it goes stale the wakeup is
    presumed dead and blocking resumes so recovery can take over.
    """
    state_file = str(payload.get("stateFile") or "").strip()
    if not state_file or not _state_wait_session(state_file):
        return False
    age = _heartbeat_age_seconds(payload.get("heartbeat"))
    return age is not None and abs(age) < PACED_WAIT_STALE_SECONDS


def _stamp_child_turn_completed() -> None:
    session = os.environ.get("STORY_AUTOMATOR_SESSION", "").strip()
    if not session:
        return
    try:
        from ..core.tmux_runtime import session_paths
        from ..core.utils import iso_now
        from ..core.tmux_runtime import update_session_state
        paths = session_paths(session)
        if not paths.state.exists():
            return
        update_session_state(paths.state, turnCompletedAt=iso_now())
    except Exception:
        # Never let the stamp break the Stop hook — heuristics still cover us.
        return


def cmd_stop_hook(_: list[str]) -> int:
    sys.stdin.read()
    if os.environ.get("STORY_AUTOMATOR_CHILD", "").lower() == "true":
        # Deterministic completion signal (2026-07-03): the harness fires this
        # hook when the child session's turn ends, so stamp the session state
        # file — check-session treats the stamp as primary completion evidence,
        # with the pane heuristics (verb timer, frozen pane) as fallback for
        # agents that don't run Claude hooks (codex) or pre-stamp sessions.
        _stamp_child_turn_completed()
        return 0
    marker = Path(os.getcwd()) / ".claude" / ".story-automator-active"
    if not marker.exists():
        return 0
    try:
        payload = json.loads(marker.read_text())
    except json.JSONDecodeError:
        return 0
    remaining = payload.get("storiesRemaining", 0)
    if isinstance(remaining, str) and remaining.isdigit():
        remaining = int(remaining)
    if not remaining:
        return 0
    # Bystander exemption: the marker records the orchestrator's pid. If that
    # orchestrator is still alive and is NOT an ancestor of this stopping
    # session, this is some other Claude session in the project (debugging,
    # unrelated work) — do not nag it to take over a run that is already
    # being driven. If the orchestrator pid is dead (crash), fall through and
    # block: any stopping session should then pick up recovery.
    try:
        marker_pid = int(payload.get("pid") or 0)
    except (TypeError, ValueError):
        marker_pid = 0
    if marker_pid and _pid_alive(marker_pid) and not _pid_in_ancestry(marker_pid):
        return 0
    # Paced-wait exemption: a persisted waitSession plus a fresh heartbeat
    # means a ScheduleWakeup is pending — the wakeup is the pump, so let the
    # turn end. Stale heartbeat or no wait state falls through and blocks.
    if _paced_wait_active(payload):
        return 0
    reason = (
        "Story Automator active "
        f"({remaining} stories remaining). Read "
        + _workflow_doc_relative("stop-hook-recovery.md")
    )
    print(json.dumps({"decision": "block", "reason": reason}, indent=2))
    return 0


def cmd_commit_story(args: list[str]) -> int:
    repo = ""
    story = ""
    title = ""
    for idx, arg in enumerate(args):
        if arg == "--repo" and idx + 1 < len(args):
            repo = args[idx + 1]
        elif arg == "--story" and idx + 1 < len(args):
            story = args[idx + 1]
        elif arg == "--title" and idx + 1 < len(args):
            title = args[idx + 1]
    if not repo or not story or not title:
        write_json({"ok": False, "error": "missing_args"})
        return 1
    if not Path(repo).is_dir():
        write_json({"ok": False, "error": "repo_not_found"})
        return 1
    status = run_cmd("git", "-C", repo, "status", "--porcelain")
    if status.exit_code != 0:
        write_json({"ok": False, "error": "git_status_failed"})
        return 1
    lines = [line for line in status.output.strip().splitlines() if line.strip()]
    if not lines:
        write_json({"ok": False, "error": "no_changes"})
        return 0
    if run_cmd("git", "-C", repo, "add", "-A").exit_code != 0:
        write_json({"ok": False, "error": "git_add_failed"})
        return 1
    message = f"feat(story-{story}): {title}"
    commit = run_cmd("git", "-C", repo, "commit", "-m", message)
    if commit.exit_code != 0:
        write_json({"ok": False, "error": "commit_failed"})
        return 1
    sha = run_cmd("git", "-C", repo, "rev-parse", "HEAD").output.strip()
    write_json({"ok": True, "commit": sha})
    return 0


def cmd_list_sessions(args: list[str]) -> int:
    if args and args[0] in {"--help", "-h"}:
        print("Usage: list-sessions --slug SLUG")
        return 0
    slug = ""
    for idx, arg in enumerate(args):
        if arg == "--slug" and idx + 1 < len(args):
            slug = args[idx + 1]
    if not slug:
        write_json({"ok": False, "error": "missing_slug"})
        return 1
    if shutil.which("tmux") is None:
        write_json({"ok": False, "error": "tmux_not_found", "sessions": [], "count": 0})
        return 0
    result = run_cmd("tmux", "list-sessions", "-F", "#{session_name}")
    if result.exit_code != 0:
        write_json({"ok": True, "sessions": [], "count": 0})
        return 0
    prefix = f"sa-{slug}-"
    sessions = [line for line in result.output.splitlines() if line.startswith(prefix)]
    write_json({"ok": True, "sessions": sessions, "count": len(sessions)})
    return 0
