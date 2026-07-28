from __future__ import annotations

import json
import os
import re
from pathlib import Path

from story_automator.core.frontmatter import (
    extract_last_action,
    find_frontmatter_value,
    find_frontmatter_value_case,
    parse_frontmatter,
    parse_simple_frontmatter,
)
from story_automator.core.runtime_policy import (
    PolicyError,
    crash_max_retries,
    load_runtime_policy,
    review_max_cycles,
    summarize_state_policy_fields,
)
from story_automator.core.review_verify import verify_code_review_completion
from story_automator.core.success_verifiers import resolve_success_contract, run_success_verifier
from story_automator.core.sprint import sprint_status_epic, sprint_status_get
from story_automator.core.story_keys import normalize_story_key, sprint_status_file
from story_automator.core.utils import (
    atomic_write,
    ensure_dir,
    extract_json_line,
    file_exists,
    get_project_root,
    iso_now,
    print_json,
    read_text,
    run_cmd,
    trim_lines,
)
from .orchestrator_epic_agents import (
    agents_build_action,
    agents_resolve_action,
    check_blocking_action,
    check_epic_complete_action,
    get_epic_stories_action,
)
from .orchestrator_parse import parse_output_action


def cmd_orchestrator_helper(args: list[str]) -> int:
    if not args:
        return _usage(1)
    if args[0] in {"--help", "-h"}:
        return _usage(0)
    action = args[0]
    dispatch = {
        "sprint-status": _sprint_status,
        "parse-output": parse_output_action,
        "marker": _marker,
        "state-list": _state_list,
        "state-latest": _state_latest,
        "state-latest-incomplete": _state_latest_incomplete,
        "state-summary": _state_summary,
        "state-update": _state_update,
        "validate-completion": _validate_completion,
        "escalate": _escalate,
        "commit-ready": _commit_ready,
        "normalize-key": _normalize_key,
        "story-file-status": _story_file_status,
        "verify-step": _verify_step,
        "verify-code-review": _verify_code_review,
        "check-epic-complete": check_epic_complete_action,
        "get-epic-stories": get_epic_stories_action,
        "check-blocking": check_blocking_action,
        "agents-build": agents_build_action,
        "agents-resolve": agents_resolve_action,
    }
    handler = dispatch.get(action)
    if handler is None:
        return _usage(1)
    return handler(args[1:])


def _usage(code: int) -> int:
    target = __import__("sys").stderr if code else __import__("sys").stdout
    print("Usage: orchestrator-helper <action> [args]", file=target)
    print("", file=target)
    print("Actions:", file=target)
    print("  sprint-status get <story_key>", file=target)
    print("  sprint-status exists", file=target)
    print("  sprint-status check-epic <epic>", file=target)
    print("  parse-output <file> <step>", file=target)
    print("  marker create --epic E --story S --remaining N --state-file F", file=target)
    print("  marker remove", file=target)
    print("  marker check", file=target)
    print("  marker heartbeat", file=target)
    print("  state-list <folder>", file=target)
    print("  state-latest <folder> [status]", file=target)
    print("  state-latest-incomplete <folder>", file=target)
    print("  state-summary <file>", file=target)
    print("  state-update <file> --set k=v [--append-array k=v]", file=target)
    print("  validate-completion --state <file> [--story-range CSV]", file=target)
    print("  escalate <trigger> <context>", file=target)
    print("  commit-ready <story_id>", file=target)
    print("  normalize-key <input> [--to id|key|prefix|json]", file=target)
    print("  story-file-status <story>", file=target)
    print("  verify-step <step> <story_or_epic> [--state-file path] [--output-file path]", file=target)
    print("  verify-code-review <story>", file=target)
    print("  check-epic-complete <epic> <story> [--state-file path]", file=target)
    print("  get-epic-stories <epic> [--state-file path]", file=target)
    print("  check-blocking <story_id>", file=target)
    print("  agents-build --state-file path --complexity-file path --output path --config-json '{}'", file=target)
    print("  agents-resolve (--state-file path | --agents-file path) --story ID --task create|dev|auto|review", file=target)
    return code


def _sprint_status(args: list[str]) -> int:
    if not args:
        print("Usage: orchestrator-helper sprint-status <get|exists|check-epic> [args]", file=__import__("sys").stderr)
        return 1
    project_root = get_project_root()
    if args[0] == "get":
        if len(args) < 2:
            print("Usage: orchestrator-helper sprint-status get <story_key>", file=__import__("sys").stderr)
            return 1
        status = sprint_status_get(project_root, args[1])
        if not status.found and status.reason:
            print_json({"found": False, "status": status.status, "reason": status.reason})
            return 0
        if not status.found:
            print_json({"found": False, "story": args[1], "status": "not_found"})
            return 0
        print_json({"found": True, "story": status.story, "status": status.status, "done": status.done})
        return 0
    if args[0] == "exists":
        print("true" if file_exists(sprint_status_file(project_root)) else "false")
        return 0
    if args[0] == "check-epic":
        if len(args) < 2:
            print("Usage: orchestrator-helper sprint-status check-epic <epic>", file=__import__("sys").stderr)
            return 1
        stories, done = sprint_status_epic(project_root, args[1])
        if not stories:
            print_json({"ok": False, "epic": args[1], "allStoriesDone": False, "reason": "no_stories_found", "count": 0})
            return 0
        print_json({"ok": True, "epic": args[1], "allStoriesDone": done == len(stories), "total": len(stories), "done": done, "count": len(stories), "stories": stories})
        return 0
    print("Usage: orchestrator-helper sprint-status <get|exists|check-epic> [args]", file=__import__("sys").stderr)
    return 1


def _claude_ancestor_pid() -> int:
    """Nearest ancestor process whose executable is the claude CLI.

    Marker create/heartbeat run as Bash children of the orchestrating
    Claude session, so this resolves the orchestrator's session pid —
    the pid the Stop hook compares against to exempt bystander sessions.
    Returns 0 when no claude ancestor is found (hook then fail-safe
    blocks every session, the pre-fix behavior).
    """
    import subprocess
    pid = os.getpid()
    for _ in range(20):
        if pid <= 1:
            return 0
        try:
            out = subprocess.run(
                ["ps", "-o", "ppid=,comm=", "-p", str(pid)],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            parts = out.split(None, 1)
            ppid = int(parts[0])
            comm = parts[1] if len(parts) > 1 else ""
        except (ValueError, IndexError, OSError):
            return 0
        if comm.rstrip().endswith("/claude") or comm.rstrip() == "claude":
            return pid
        pid = ppid
    return 0


def _marker(args: list[str]) -> int:
    if not args:
        print("Usage: orchestrator-helper marker <create|remove|check|heartbeat> [args]", file=__import__("sys").stderr)
        return 1
    marker_file = Path(get_project_root()) / ".claude" / ".story-automator-active"
    if args[0] == "create":
        options = {"epic": "", "story": "", "remaining": "0", "state-file": "", "project-slug": "", "pid": "0", "heartbeat": ""}
        idx = 1
        while idx < len(args):
            key = args[idx].lstrip("-")
            if idx + 1 < len(args):
                options[key] = args[idx + 1]
                idx += 2
            else:
                idx += 1
        ensure_dir(marker_file.parent)
        payload = {
            "epic": options["epic"],
            "currentStory": options["story"],
            "storiesRemaining": int(options["remaining"] or "0"),
            "stateFile": options["state-file"],
            "createdAt": iso_now(),
            "heartbeat": options["heartbeat"] or iso_now(),
            "pid": _claude_ancestor_pid() or int(options["pid"] or "0"),
            "projectSlug": options["project-slug"],
        }
        atomic_write(marker_file, json.dumps(payload, indent=2) + "\n")
        print(f"Marker created: {marker_file}")
        return 0
    if args[0] == "remove":
        if marker_file.exists():
            marker_file.unlink()
        print("Marker removed")
        return 0
    if args[0] == "check":
        if marker_file.exists():
            print(f'{{"exists":true,"file":"{marker_file}"}}')
            print(marker_file.read_text(encoding="utf-8"), end="")
            return 0
        print('{"exists":false}')
        return 0
    if args[0] == "heartbeat":
        if not marker_file.exists():
            print("No marker file to update")
            return 1
        payload = json.loads(marker_file.read_text(encoding="utf-8"))
        payload["heartbeat"] = iso_now()
        live_pid = _claude_ancestor_pid()
        if live_pid:
            payload["pid"] = live_pid
        atomic_write(marker_file, json.dumps(payload, indent=2) + "\n")
        print(f"Heartbeat updated: {payload['heartbeat']}")
        return 0
    print("Usage: orchestrator-helper marker <create|remove|check|heartbeat> [args]", file=__import__("sys").stderr)
    return 1


def _state_list(args: list[str]) -> int:
    if not args or not Path(args[0]).is_dir():
        print_json({"ok": False, "error": "folder_not_found", "files": []})
        return 1
    files = []
    for path in sorted(Path(args[0]).glob("orchestration-*.md")):
        files.append({"path": str(path), "status": find_frontmatter_value(path, "status") or "unknown", "lastUpdated": find_frontmatter_value(path, "lastUpdated") or "unknown"})
    print_json({"ok": True, "files": files})
    return 0


def _state_latest(args: list[str]) -> int:
    if not args or not Path(args[0]).is_dir():
        print_json({"ok": False, "error": "folder_not_found"})
        return 1
    status_filter = args[1] if len(args) > 1 else ""
    matches = []
    for path in Path(args[0]).glob("orchestration-*.md"):
        status = find_frontmatter_value(path, "status")
        if status_filter and status != status_filter:
            continue
        matches.append((find_frontmatter_value(path, "lastUpdated"), str(path)))
    if not matches:
        print_json({"ok": False, "error": "no_match"})
        return 0
    updated, path = max(matches)
    print_json({"ok": True, "path": path, "lastUpdated": updated})
    return 0


def _state_latest_incomplete(args: list[str]) -> int:
    if not args or not Path(args[0]).is_dir():
        print_json({"ok": False, "error": "folder_not_found"})
        return 1
    matches = []
    for path in Path(args[0]).glob("orchestration-*.md"):
        status = find_frontmatter_value(path, "status")
        if status == "COMPLETE":
            continue
        matches.append((find_frontmatter_value(path, "lastUpdated"), status, str(path)))
    if not matches:
        print_json({"ok": False, "error": "no_incomplete_state"})
        return 0
    updated, status, path = max(matches)
    print_json({"ok": True, "path": path, "lastUpdated": updated, "status": status})
    return 0


def _state_summary(args: list[str]) -> int:
    if not args or not file_exists(args[0]):
        print_json({"ok": False, "error": "file_not_found"})
        return 1
    fields = parse_simple_frontmatter(read_text(args[0]))
    snapshot_file, snapshot_hash, policy_version, legacy_policy, policy_error = summarize_state_policy_fields(
        fields,
        project_root=get_project_root(),
    )
    payload = {
        "ok": True,
        "epic": str(fields.get("epic") or ""),
        "epicName": str(fields.get("epicName") or ""),
        "currentStory": str(fields.get("currentStory") or ""),
        "currentStep": str(fields.get("currentStep") or ""),
        "status": str(fields.get("status") or ""),
        "lastUpdated": str(fields.get("lastUpdated") or ""),
        "policyVersion": policy_version,
        "policySnapshotFile": snapshot_file,
        "policySnapshotHash": snapshot_hash,
        "legacyPolicy": legacy_policy,
        "lastAction": extract_last_action(args[0]),
    }
    if policy_error:
        payload["policyError"] = policy_error
    print_json(payload)
    return 0


def _state_update(args: list[str]) -> int:
    if not args or not file_exists(args[0]):
        print_json({"ok": False, "error": "file_not_found"})
        return 1
    text = read_text(args[0])
    updated: list[str] = []
    last_appended_value: str | None = None
    last_appended_already_present: bool = False
    last_appended_key: str | None = None
    idx = 1
    while idx < len(args):
        if args[idx] == "--set" and idx + 1 < len(args):
            key, value = args[idx + 1].split("=", 1)
            replaced, count = re.subn(rf"(?m)^{re.escape(key)}:.*$", f"{key}: {value}", text)
            if count:
                text = replaced
                updated.append(key)
            idx += 2
            continue
        if args[idx] == "--append-array" and idx + 1 < len(args):
            key, value = args[idx + 1].split("=", 1)
            match = re.search(rf"(?m)^{re.escape(key)}:(.*)$", text)
            if match is None:
                print_json({"ok": False, "error": "key_not_found", "key": key})
                return 1
            raw_value = match.group(1).strip()
            flow_match = re.fullmatch(r"\[(.*)\]", raw_value)
            if flow_match is None:
                print_json({"ok": False, "error": "array_line_not_flow_style", "key": key})
                return 1
            inner = flow_match.group(1).strip()
            current_items = [item.strip() for item in inner.split(",")] if inner else []
            current_items = [item for item in current_items if item]
            already_present = value in current_items
            if already_present:
                new_items = current_items
            else:
                new_items = current_items + [value]
            new_line = f"{key}: [{', '.join(new_items)}]"
            text = text[: match.start()] + new_line + text[match.end():]
            if key not in updated:
                updated.append(key)
            last_appended_key = key
            last_appended_value = value
            last_appended_already_present = already_present
            idx += 2
            continue
        idx += 1
    if not updated:
        print_json({"ok": False, "error": "keys_not_found", "updated": []})
        return 1
    Path(args[0]).write_text(text, encoding="utf-8")
    result: dict = {"ok": True, "updated": updated}
    if last_appended_key is not None:
        result["appended"] = last_appended_value
        result["alreadyPresent"] = last_appended_already_present
    print_json(result)
    return 0


def _validate_completion(args: list[str]) -> int:
    """Story 24-7 — verify stepsCompleted contains every required entry.

    Required entries (read off `data/workflow-diagram.md` "State document
    writes" table):
      - step-02-preflight, step-02a-preflight-config, step-02b-preflight-finalize
      - For each story_id in storyRange:
          step-03-execute:{story_id}
          step-03a-execute-review:{story_id}
          step-03b-execute-finish:{story_id}
      - step-03c-execute-complete

    step-04-wrapup is intentionally NOT in the required set — this helper
    is invoked FROM step-04 before it appends its own marker.
    """
    state_path = ""
    story_range_override: list[str] | None = None
    idx = 0
    while idx < len(args):
        if args[idx] == "--state" and idx + 1 < len(args):
            state_path = args[idx + 1]
            idx += 2
            continue
        if args[idx] == "--story-range" and idx + 1 < len(args):
            story_range_override = [s.strip() for s in args[idx + 1].split(",") if s.strip()]
            idx += 2
            continue
        idx += 1
    if not state_path:
        print_json({"ok": False, "error": "state_path_required"})
        return 1
    if not file_exists(state_path):
        print_json({"ok": False, "error": "file_not_found", "state": state_path})
        return 1
    text = read_text(state_path)

    def _parse_flow_array(key: str) -> list[str] | None:
        match = re.search(rf"(?m)^{re.escape(key)}:(.*)$", text)
        if match is None:
            return None
        raw = match.group(1).strip()
        flow = re.fullmatch(r"\[(.*)\]", raw)
        if flow is None:
            return None
        inner = flow.group(1).strip()
        if not inner:
            return []
        return [item.strip() for item in inner.split(",") if item.strip()]

    if story_range_override is not None:
        story_range = story_range_override
    else:
        parsed = _parse_flow_array("storyRange")
        if parsed is None:
            print_json({"ok": False, "error": "story_range_parse_failed"})
            return 1
        story_range = [item.strip('"').strip("'") for item in parsed]

    steps_completed = _parse_flow_array("stepsCompleted")
    if steps_completed is None:
        print_json({"ok": False, "error": "steps_completed_parse_failed"})
        return 1

    required: list[str] = [
        "step-02-preflight",
        "step-02a-preflight-config",
        "step-02b-preflight-finalize",
    ]
    for story_id in story_range:
        required.append(f"step-03-execute:{story_id}")
        required.append(f"step-03a-execute-review:{story_id}")
        required.append(f"step-03b-execute-finish:{story_id}")
    required.append("step-03c-execute-complete")

    completed_set = set(steps_completed)
    missing = [entry for entry in required if entry not in completed_set]

    if missing:
        print_json({"ok": False, "missing": missing})
        return 1
    print_json({"ok": True, "missing": []})
    return 0


def _escalate(args: list[str]) -> int:
    trigger = args[0] if args else ""
    context = args[1] if len(args) > 1 else ""
    state_file = ""
    idx = 2
    try:
        while idx < len(args):
            if args[idx] == "--state-file":
                state_file = _flag_value(args, idx, "--state-file")
                idx += 2
                continue
            idx += 1
    except PolicyError as exc:
        print_json({"escalate": True, "reason": str(exc)})
        return 0
    try:
        policy = load_runtime_policy(get_project_root(), state_file=state_file)
    except (FileNotFoundError, PolicyError) as exc:
        print_json({"escalate": True, "reason": str(exc)})
        return 0
    if trigger == "review-loop":
        cycles = _parse_context_int(context, "cycles")
        limit = review_max_cycles(policy)
        if cycles >= limit:
            print_json({"escalate": True, "reason": f"Review loop exceeded max cycles ({cycles}/{limit})"})
        else:
            print_json({"escalate": False})
        return 0
    if trigger == "session-crash":
        retries = _parse_context_int(context, "retries")
        limit = crash_max_retries(policy)
        if retries >= limit:
            print_json({"escalate": True, "reason": f"Session crashed after {retries} retries"})
        else:
            print_json({"escalate": False, "action": "retry"})
        return 0
    if trigger == "story-validation":
        created = _parse_context_int(context, "created")
        if created != 1:
            print_json({"escalate": True, "reason": "No story file created" if created == 0 else f"Runaway creation: {created} files"})
        else:
            print_json({"escalate": False})
        return 0
    print_json({"escalate": False, "reason": "Unknown trigger"})
    return 0


def _commit_ready(args: list[str]) -> int:
    if not args:
        print_json({"ready": False, "reason": "story_id required"})
        return 1
    project_root = get_project_root()
    status = sprint_status_get(project_root, args[0])
    if status.done:
        out, _ = run_cmd("git", "-C", project_root, "status", "--porcelain")
        if out.strip():
            print_json({"ready": True, "story": args[0], "status": "done", "uncommitted_changes": True})
            return 0
        print_json({"ready": False, "reason": "No uncommitted changes", "story": args[0]})
        return 0
    print_json({"ready": False, "reason": "Story not done yet", "story": args[0], "current_status": status.status})
    return 0


def _normalize_key(args: list[str]) -> int:
    if not args:
        print_json({"ok": False, "error": "input required"})
        return 1
    fmt = "json"
    if len(args) >= 3 and args[1] == "--to":
        fmt = args[2]
    result = normalize_story_key(get_project_root(), args[0])
    if result is None:
        print_json({"ok": False, "error": "unrecognized format", "input": args[0]})
        return 1
    if fmt == "id":
        print(result.id)
    elif fmt == "prefix":
        print(result.prefix)
    elif fmt == "key":
        print(result.key)
    else:
        print_json({"ok": True, "id": result.id, "prefix": result.prefix, "key": result.key})
    return 0


def _story_file_status(args: list[str]) -> int:
    if not args:
        print_json({"ok": False, "error": "story input required"})
        return 1
    norm = normalize_story_key(get_project_root(), args[0])
    if norm is None:
        print_json({"ok": False, "error": "could not normalize story key", "input": args[0]})
        return 1
    matches = sorted((Path(get_project_root()) / "_bmad-output" / "implementation-artifacts").glob(f"{norm.prefix}-*.md"))
    if not matches:
        print_json({"ok": False, "error": "story file not found", "prefix": norm.prefix})
        return 1
    print_json({"ok": True, "story_key": norm.key, "file": str(matches[0]), "status": find_frontmatter_value_case(matches[0], "Status") or "unknown", "title": find_frontmatter_value_case(matches[0], "Title")})
    return 0


def _verify_code_review(args: list[str]) -> int:
    if not args:
        print_json({"verified": False, "reason": "story_key_required"})
        return 1
    state_file = ""
    tail = args[1:]
    try:
        idx = 0
        while idx < len(tail):
            if tail[idx] == "--state-file":
                state_file = _flag_value(tail, idx, "--state-file")
                idx += 2
                continue
            idx += 1
    except PolicyError as exc:
        print_json({"verified": False, "reason": "review_contract_invalid", "input": args[0], "error": str(exc)})
        return 1
    payload = verify_code_review_completion(get_project_root(), args[0], state_file=state_file or None)
    print_json(payload)
    return 0 if bool(payload.get("verified")) else 1


def _verify_step(args: list[str]) -> int:
    if len(args) < 2:
        print_json({"verified": False, "reason": "step_and_story_required"})
        return 1
    step, story_key = args[:2]
    state_file = ""
    output_file = ""
    tail = args[2:]
    try:
        idx = 0
        while idx < len(tail):
            arg = tail[idx]
            if arg in {"--state-file", "--output-file"}:
                if idx + 1 >= len(tail) or not tail[idx + 1].strip() or tail[idx + 1].startswith("--"):
                    raise PolicyError(f"{arg} requires a value")
                if arg == "--state-file":
                    state_file = tail[idx + 1]
                else:
                    output_file = tail[idx + 1]
                idx += 2
                continue
            idx += 1
        contract = resolve_success_contract(get_project_root(), step, state_file=state_file or None)
        verifier = str(contract.get("verifier") or "").strip()
        if not verifier:
            raise PolicyError(f"missing success verifier for {step}")
        payload = run_success_verifier(
            verifier,
            project_root=get_project_root(),
            story_key=story_key,
            output_file=output_file,
            contract=contract,
        )
        exit_code = 0
    except (FileNotFoundError, PolicyError, ValueError) as exc:
        payload = {"verified": False, "step": step, "input": story_key, "reason": "verifier_contract_invalid", "error": str(exc)}
        exit_code = 1
    print_json(payload)
    return exit_code


def _parse_context_int(context: str, key: str) -> int:
    match = re.search(rf"{re.escape(key)}=(\d+)", context)
    return int(match.group(1)) if match else 0


def _flag_value(args: list[str], idx: int, flag: str) -> str:
    if idx + 1 >= len(args) or not args[idx + 1].strip() or args[idx + 1].startswith("--"):
        raise PolicyError(f"{flag} requires a value")
    return args[idx + 1]
