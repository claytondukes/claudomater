from __future__ import annotations

import os
import shlex
import sys
import time
from pathlib import Path

from story_automator.core import model_select, project_config
from story_automator.core.runtime_policy import PolicyError, load_runtime_policy, step_contract
from story_automator.core.success_verifiers import resolve_success_contract, run_success_verifier
from story_automator.core.tmux_runtime import (
    agent_cli,
    agent_type,
    generate_session_name,
    heartbeat_check,
    runtime_mode,
    session_status,
    skill_prefix,
    spawn_session,
    tmux_has_session,
    tmux_kill_session,
    tmux_list_sessions,
)
from story_automator.core.utils import (
    get_project_root,
    print_json,
    project_hash,
    project_slug,
    read_text,
)


def cmd_tmux_wrapper(args: list[str]) -> int:
    if not args:
        return _usage(1)
    if args[0] in {"--help", "-h"}:
        return _usage(0)
    action = args[0]
    if action == "spawn":
        return _spawn(args[1:])
    if action == "name":
        if len(args) < 4:
            return _usage(1)
        cycle = args[4] if len(args) > 4 else ""
        print(generate_session_name(args[1], args[2], args[3], cycle))
        return 0
    if action == "list":
        sessions, _ = tmux_list_sessions("--project-only" in args[1:])
        print("\n".join(sessions))
        return 0
    if action == "kill":
        if len(args) < 2:
            return _usage(1)
        tmux_kill_session(args[1])
        return 0
    if action == "kill-all":
        sessions, _ = tmux_list_sessions("--project-only" in args[1:])
        for session in sessions:
            tmux_kill_session(session)
        print(f"Killed {len(sessions)} sessions")
        return 0
    if action == "exists":
        if len(args) < 2:
            return _usage(1)
        if tmux_has_session(args[1]):
            print("true")
            return 0
        print("false")
        return 1
    if action == "build-cmd":
        return _build_cmd(args[1:])
    if action == "project-slug":
        print(project_slug())
        return 0
    if action == "project-hash":
        print(project_hash())
        return 0
    if action == "story-suffix":
        if len(args) < 2:
            return _usage(1)
        print(args[1].replace(".", "-"))
        return 0
    if action == "agent-type":
        print(agent_type())
        return 0
    if action == "agent-cli":
        print(agent_cli(agent_type()))
        return 0
    if action == "skill-prefix":
        print(skill_prefix(agent_type()))
        return 0
    return _usage(1)


def _usage(code: int) -> int:
    target = __import__("sys").stderr if code else __import__("sys").stdout
    print("Usage: tmux-wrapper <action> [args...]", file=target)
    print("", file=target)
    print("Actions:", file=target)
    print('  spawn <step> <epic> <story_id> --command "..." [--cycle N] [--agent TYPE]', file=target)
    print("  name <step> <epic> <story_id> [--cycle N]", file=target)
    print("  list [--project-only]", file=target)
    print("  kill <session_name>", file=target)
    print("  kill-all [--project-only]", file=target)
    print("  exists <session_name>", file=target)
    print("  build-cmd <step> <story_id> [--agent TYPE] [--state-file PATH] [extra_instruction]", file=target)
    print("  project-slug", file=target)
    print("  project-hash", file=target)
    print("  story-suffix <story_id>", file=target)
    print("  agent-type", file=target)
    print("  agent-cli", file=target)
    print("  skill-prefix", file=target)
    return code


def _spawn(args: list[str]) -> int:
    if args and args[0] in {"--help", "-h"}:
        return _usage(0)
    if len(args) < 3:
        return _usage(1)
    step, epic, story_id = args[:3]
    command = ""
    cycle = ""
    agent = agent_type()
    auto_answer_elicitation = False
    state_doc_file = ""
    tail = args[3:]
    for idx, arg in enumerate(tail):
        if arg == "--command" and idx + 1 < len(tail):
            command = tail[idx + 1]
        elif arg == "--cycle" and idx + 1 < len(tail):
            cycle = tail[idx + 1]
        elif arg == "--agent" and idx + 1 < len(tail):
            agent = tail[idx + 1]
        elif arg == "--auto-answer-elicitation":
            auto_answer_elicitation = True
        elif arg == "--state-doc-file" and idx + 1 < len(tail):
            state_doc_file = tail[idx + 1]
    if not command:
        print("--command is required", file=__import__("sys").stderr)
        return 1
    session = generate_session_name(step, epic, story_id, cycle)
    root = get_project_root()
    out, code = spawn_session(
        session,
        command,
        agent,
        root,
        mode=runtime_mode(),
        auto_answer_elicitation=auto_answer_elicitation,
        state_doc_file=state_doc_file,
    )
    if code != 0:
        print(out.strip(), file=__import__("sys").stderr)
        return 1
    print(session)
    return 0


def _build_cmd(args: list[str]) -> int:
    if args and args[0] in {"--help", "-h"}:
        return _usage(0)
    if len(args) < 2:
        return _usage(1)
    step, story_id = args[:2]
    agent = ""
    extra = ""
    tail = args[2:]
    idx = 0
    state_file = ""
    try:
        while idx < len(tail):
            if tail[idx] == "--agent":
                agent = _flag_value(tail, idx, "--agent")
                idx += 2
                continue
            if tail[idx] == "--state-file":
                state_file = _flag_value(tail, idx, "--state-file")
                idx += 2
                continue
            extra = f"{extra} {tail[idx]}".strip()
            idx += 1
    except PolicyError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    agent = agent or agent_type()
    story_prefix = story_id.replace(".", "-")
    root = get_project_root()
    try:
        policy = load_runtime_policy(root, state_file=state_file)
        contract = step_contract(policy, step)
        prompt = _render_step_prompt(contract, story_id, story_prefix, extra, root)
    except (OSError, PolicyError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    # Model tier: review/retro are the quality gate and always run top-tier;
    # create/dev/auto follow the story's complexity. Fable downgrades to Opus
    # when its weekly limit is exhausted (checked live per spawn). See
    # data/model-selection.md.
    _models_cfg = policy.get("models") if isinstance(policy, dict) else None
    _step_model = model_select.select_model(
        step,
        story_id,
        state_file,
        _models_cfg,
        warn=lambda message: print(message, file=sys.stderr),
    )
    ai_command = os.environ.get("AI_COMMAND")
    if ai_command and not os.environ.get("AI_AGENT"):
        cli = ai_command
    elif agent != "codex":
        cli = f"{agent_cli(agent)} --model {_step_model}"
    else:
        cli = "codex exec"
    quoted_prompt = shlex.quote(prompt)
    if agent == "codex" and not ai_command:
        codex_home = f"/tmp/sa-codex-home-{project_hash(root)}"
        auth_src = os.path.expanduser("~/.codex/auth.json")
        print(
            f'mkdir -p "{codex_home}"'
            + f' && if [ -f "{auth_src}" ]; then ln -sf "{auth_src}" "{codex_home}/auth.json"; fi'
            + f' && CODEX_HOME="{codex_home}" codex exec -s workspace-write -c \'approval_policy="never"\''
            + f' -c \'model_reasoning_effort="high"\''
            + f" --disable plugins --disable sqlite --disable shell_snapshot {quoted_prompt}"
        )
    else:
        print(f"unset CLAUDECODE && {cli} {quoted_prompt}")
    return 0


def _render_step_prompt(
    contract: dict[str, object],
    story_id: str,
    story_prefix: str,
    extra_instruction: str,
    project_root: str = "",
) -> str:
    prompt_cfg = contract.get("prompt") or {}
    assets = (contract.get("assets") or {}).get("files") or {}
    template = read_text(str(prompt_cfg.get("templatePath") or ""))
    root = project_root or get_project_root()
    cfg = project_config.load_project_config(root)
    replacements = {
        "{{story_id}}": story_id,
        "{{story_prefix}}": story_prefix,
        "{{label}}": str(contract.get("label") or ""),
        "{{skill_line}}": _prompt_line("READ this skill first", str(assets.get("skill") or "")),
        "{{workflow_line}}": _prompt_line("READ this workflow file next", str(assets.get("workflow") or "")),
        "{{instructions_line}}": _prompt_line("Then read", str(assets.get("instructions") or "")),
        "{{checklist_line}}": _prompt_line("Validate with", str(assets.get("checklist") or "")),
        "{{template_line}}": _prompt_line("Use template", str(assets.get("template") or "")),
        "{{extra_instruction}}": extra_instruction.strip() or str(prompt_cfg.get("defaultExtraInstruction") or ""),
        "{{project_root}}": str(root),
        "{{project_name}}": project_config.project_name(root, cfg),
        "{{reviewer_bridge}}": project_config.reviewer_bridge(root, cfg),
        "{{test_gauntlet}}": project_config.test_gauntlet_block(root, cfg),
        "{{branch_pattern}}": project_config.branch_pattern_bash(root, cfg),
    }
    for key, value in replacements.items():
        template = template.replace(key, value)
    return template


def _prompt_line(prefix: str, value: str) -> str:
    return f"{prefix}: {value}\n" if value else ""


def cmd_heartbeat_check(args: list[str]) -> int:
    if not args:
        print("error,0.0,,no_session")
        return 0
    session = args[0]
    agent = "auto"
    tail = args[1:]
    for idx, arg in enumerate(tail):
        if arg == "--agent" and idx + 1 < len(tail):
            agent = tail[idx + 1]
    status, cpu, pid, prompt = heartbeat_check(session, agent, project_root=get_project_root(), mode=runtime_mode())
    print(f"{status},{cpu:.1f},{pid},{prompt}")
    return 0


def cmd_codex_status_check(args: list[str]) -> int:
    return _status_check(args, codex=True)


def cmd_tmux_status_check(args: list[str]) -> int:
    return _status_check(args, codex=False)


def _status_check(args: list[str], codex: bool) -> int:
    if not args:
        print("error,0,0,no_session,30,error")
        return 0 if codex else 1
    session = args[0]
    full = "--full" in args[1:]
    project_root: str | None = None
    tail = args[1:]
    idx = 0
    while idx < len(tail):
        if tail[idx] == "--project-root" and idx + 1 < len(tail):
            project_root = tail[idx + 1]
            idx += 2
            continue
        idx += 1
    status = session_status(session, full=full, codex=codex, project_root=project_root, mode=runtime_mode())
    print(",".join([status["status"], str(status["todos_done"]), str(status["todos_total"]), status["active_task"], str(status["wait_estimate"]), status["session_state"]]))
    return 0 if codex else (0 if status["status"] != "error" else 1)


def cmd_monitor_session(args: list[str]) -> int:
    if not args:
        print("Usage: monitor-session <session_name> [options]", file=__import__("sys").stderr)
        return 1
    if args[0] in {"--help", "-h"}:
        print("Usage: monitor-session <session_name> [options]")
        print("Options: --max-polls N --initial-wait N --project-root PATH --timeout MIN --verbose --json --agent TYPE --workflow TYPE --story-key KEY --state-file PATH")
        return 0
    session = args[0]
    max_polls = 30
    initial_wait = 5
    timeout_minutes = 60
    json_output = False
    agent = os.environ.get("AI_AGENT", "claude")
    workflow = "dev"
    story_key = ""
    state_file = ""
    project_root = get_project_root()
    idx = 1
    while idx < len(args):
        arg = args[idx]
        if arg == "--max-polls" and idx + 1 < len(args):
            max_polls = int(args[idx + 1])
            idx += 2
            continue
        if arg == "--initial-wait" and idx + 1 < len(args):
            initial_wait = int(args[idx + 1])
            idx += 2
            continue
        if arg == "--timeout" and idx + 1 < len(args):
            timeout_minutes = int(args[idx + 1])
            idx += 2
            continue
        if arg == "--json":
            json_output = True
        elif arg == "--agent" and idx + 1 < len(args):
            agent = args[idx + 1]
            idx += 2
            continue
        elif arg == "--workflow" and idx + 1 < len(args):
            workflow = args[idx + 1]
            idx += 2
            continue
        elif arg == "--story-key" and idx + 1 < len(args):
            story_key = args[idx + 1]
            idx += 2
            continue
        elif arg == "--state-file":
            try:
                state_file = _flag_value(args, idx, "--state-file")
            except PolicyError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            idx += 2
            continue
        elif arg == "--project-root" and idx + 1 < len(args):
            project_root = args[idx + 1]
            idx += 2
            continue
        idx += 1
    if agent == "codex":
        timeout_minutes = timeout_minutes * 3 // 2
    time.sleep(max(0, initial_wait))
    start = time.time()
    last_done = 0
    last_total = 0
    for _poll in range(1, max_polls + 1):
        if time.time() - start >= timeout_minutes * 60:
            return _emit_monitor(json_output, "timeout", last_done, last_total, "", f"exceeded_{timeout_minutes}m")
        status = session_status(session, full=False, codex=agent == "codex", project_root=project_root, mode=runtime_mode())
        if int(status["todos_done"]) or int(status["todos_total"]):
            last_done = int(status["todos_done"])
            last_total = int(status["todos_total"])
        state = str(status["session_state"])
        if state == "completed":
            output = session_status(session, full=True, codex=agent == "codex", project_root=project_root, mode=runtime_mode())["active_task"]
            verification = _verify_monitor_completion(
                workflow,
                project_root=project_root,
                story_key=story_key,
                output_file=str(output),
                state_file=state_file or None,
            )
            if verification is not None:
                verified, verifier_name = verification
                if bool(verified.get("verified")):
                    reason = "normal_completion" if verifier_name == "session_exit" else "verified_complete"
                    return _emit_monitor(
                        json_output,
                        "completed",
                        last_done,
                        last_total,
                        str(output),
                        reason,
                        output_verified=bool(verified.get("verified")),
                    )
                return _emit_monitor(
                    json_output,
                    "incomplete",
                    last_done,
                    last_total,
                    str(output),
                    str(verified.get("reason") or "workflow_not_verified"),
                    output_verified=bool(verified.get("verified")),
                )
            return _emit_monitor(json_output, "completed", last_done, last_total, str(output), "normal_completion")
        if state == "crashed":
            crashed = session_status(session, full=True, codex=agent == "codex", project_root=project_root, mode=runtime_mode())
            return _emit_monitor(
                json_output,
                "crashed",
                last_done,
                last_total,
                str(crashed["active_task"]),
                f"exit_code_{int(crashed['wait_estimate'])}",
            )
        if state == "stuck":
            output = session_status(session, full=True, codex=agent == "codex", project_root=project_root, mode=runtime_mode())["active_task"]
            return _emit_monitor(json_output, "stuck", 0, 0, str(output), "never_active")
        if state == "not_found":
            return _emit_monitor(json_output, "not_found", last_done, last_total, "", "session_gone")
        time.sleep(min(180 if agent == "codex" else 120, max(5, int(status["wait_estimate"]))))
    output = session_status(session, full=True, codex=agent == "codex", project_root=project_root, mode=runtime_mode())["active_task"]
    return _emit_monitor(json_output, "timeout", last_done, last_total, str(output), "max_polls_exceeded")


def cmd_check_session(args: list[str]) -> int:
    """Single non-blocking status check for a tmux session.

    Emits the same JSON contract as monitor-session, plus final_state
    "running" while the session is still working. Never sleeps: the
    orchestrator paces re-checks with a stepped ScheduleWakeup ladder
    (300s, 180s, then 120s repeating) — see data/wait-ladder.md. This
    replaces blocking monitor-session waits, which exceed the harness
    Bash tool timeout and stall the orchestration.
    """
    if not args:
        print("Usage: check-session <session_name> [options]", file=__import__("sys").stderr)
        return 1
    if args[0] in {"--help", "-h"}:
        print("Usage: check-session <session_name> [options]")
        print("Options: --project-root PATH --started-at EPOCH --timeout MIN --json --agent TYPE --workflow TYPE --story-key KEY --state-file PATH")
        return 0
    session = args[0]
    json_output = False
    agent = os.environ.get("AI_AGENT", "claude")
    workflow = "dev"
    story_key = ""
    state_file = ""
    started_at = 0
    timeout_minutes = 0
    project_root = get_project_root()
    idx = 1
    while idx < len(args):
        arg = args[idx]
        if arg == "--json":
            json_output = True
        elif arg == "--agent" and idx + 1 < len(args):
            agent = args[idx + 1]
            idx += 2
            continue
        elif arg == "--workflow" and idx + 1 < len(args):
            workflow = args[idx + 1]
            idx += 2
            continue
        elif arg == "--story-key" and idx + 1 < len(args):
            story_key = args[idx + 1]
            idx += 2
            continue
        elif arg == "--state-file":
            try:
                state_file = _flag_value(args, idx, "--state-file")
            except PolicyError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            idx += 2
            continue
        elif arg == "--started-at" and idx + 1 < len(args):
            started_at = int(args[idx + 1])
            idx += 2
            continue
        elif arg == "--timeout" and idx + 1 < len(args):
            timeout_minutes = int(args[idx + 1])
            idx += 2
            continue
        elif arg == "--project-root" and idx + 1 < len(args):
            project_root = args[idx + 1]
            idx += 2
            continue
        idx += 1
    if agent == "codex" and timeout_minutes:
        timeout_minutes = timeout_minutes * 3 // 2
    status = session_status(session, full=False, codex=agent == "codex", project_root=project_root, mode=runtime_mode())
    done = int(status["todos_done"])
    total = int(status["todos_total"])
    state = str(status["session_state"])
    if state == "completed":
        output = session_status(session, full=True, codex=agent == "codex", project_root=project_root, mode=runtime_mode())["active_task"]
        verification = _verify_monitor_completion(
            workflow,
            project_root=project_root,
            story_key=story_key,
            output_file=str(output),
            state_file=state_file or None,
        )
        if verification is not None:
            verified, verifier_name = verification
            if bool(verified.get("verified")):
                reason = "normal_completion" if verifier_name == "session_exit" else "verified_complete"
                return _emit_monitor(json_output, "completed", done, total, str(output), reason, output_verified=True)
            return _emit_monitor(
                json_output,
                "incomplete",
                done,
                total,
                str(output),
                str(verified.get("reason") or "workflow_not_verified"),
                output_verified=False,
            )
        return _emit_monitor(json_output, "completed", done, total, str(output), "normal_completion")
    if state == "crashed":
        crashed = session_status(session, full=True, codex=agent == "codex", project_root=project_root, mode=runtime_mode())
        return _emit_monitor(json_output, "crashed", done, total, str(crashed["active_task"]), f"exit_code_{int(crashed['wait_estimate'])}")
    if state == "stuck":
        output = session_status(session, full=True, codex=agent == "codex", project_root=project_root, mode=runtime_mode())["active_task"]
        return _emit_monitor(json_output, "stuck", 0, 0, str(output), "never_active")
    if state == "not_found":
        return _emit_monitor(json_output, "not_found", done, total, "", "session_gone")
    if started_at and timeout_minutes and time.time() - started_at >= timeout_minutes * 60:
        return _emit_monitor(json_output, "timeout", done, total, "", f"exceeded_{timeout_minutes}m")
    return _emit_monitor(json_output, "running", done, total, "", "still_working")


def _emit_monitor(
    json_output: bool,
    state: str,
    done: int,
    total: int,
    output_file: str,
    reason: str,
    *,
    output_verified: bool | None = None,
) -> int:
    if json_output:
        print_json(
            {
                "final_state": state,
                "todos_done": done,
                "todos_total": total,
                "output_file": output_file,
                "exit_reason": reason,
                "output_verified": False if output_verified is None else output_verified,
            }
        )
    else:
        print(f"{state},{done},{total},{output_file},{reason}")
    return 0


def _verify_monitor_completion(
    workflow: str,
    *,
    project_root: str,
    story_key: str,
    output_file: str,
    state_file: str | Path | None = None,
) -> tuple[dict[str, object], str] | None:
    try:
        contract = resolve_success_contract(project_root, workflow, state_file=state_file)
    except (FileNotFoundError, PolicyError):
        return ({"verified": False, "reason": "verifier_contract_invalid"}, "")
    verifier_name = str(contract.get("verifier") or "").strip()
    if not verifier_name:
        return ({"verified": False, "reason": "verifier_contract_invalid"}, "")
    if verifier_name in {"create_story_artifact", "review_completion", "epic_complete"} and not story_key.strip():
        return ({"verified": False, "reason": "story_key_required", "verifier": verifier_name}, verifier_name)
    try:
        result = run_success_verifier(
            verifier_name,
            project_root=project_root,
            story_key=story_key,
            output_file=output_file,
            contract=contract,
        )
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError, PolicyError):
        return ({"verified": False, "reason": "verifier_contract_invalid"}, verifier_name)
    return (result, verifier_name)


def _flag_value(args: list[str], idx: int, flag: str) -> str:
    if idx + 1 >= len(args) or not args[idx + 1].strip() or args[idx + 1].startswith("--"):
        raise PolicyError(f"{flag} requires a value")
    return args[idx + 1]
