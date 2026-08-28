"""`omater` CLI.

Exit codes for `omater usage` (scriptable guardrail checks):
0 = ok, 2 = error, 3 = pause, 4 = degrade.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from claudomater import __version__, guardrails, hooks, initcmd
from claudomater import notify as notify_mod
from claudomater.config import ConfigError, load_project_config, load_user_config
from claudomater.runlog import (
    CONTROL_ACTIONS,
    RunError,
    RunLog,
    runs_root,
    validate_run_id,
)
from claudomater.usage import UsageUnavailable, read_usage

EXIT_OK = 0
EXIT_ERROR = 2
EXIT_PAUSE = 3
EXIT_DEGRADE = 4


def _cmd_init(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    if args.verify:
        problems = initcmd.run_verify(root)
        if problems:
            for problem in problems:
                print(f"DRIFT: {problem}", file=sys.stderr)
            return 1
        print(f"ok: {root} is provisioned and drift-free")
        return EXIT_OK
    for action in initcmd.run_init(root, force=args.force):
        print(action)
    return EXIT_OK


def _cmd_usage(args: argparse.Namespace) -> int:
    cfg = load_user_config(args.user_config)
    try:
        snapshot = read_usage(max_stale=cfg.usage.max_stale_seconds)
    except UsageUnavailable as exc:
        snapshot = exc  # evaluate() turns this into the fail-closed pause
    decision = guardrails.evaluate(snapshot, cfg)
    if args.json:
        print(json.dumps(decision.as_dict(), indent=2, sort_keys=True))
    else:
        if decision.snapshot:
            s = decision.snapshot

            def pct(v):  # a missing window prints as unknown, never crashes
                return f"{v:.0f}%" if v is not None else "?"

            scoped = (
                f" · {s.scoped_model or 'scoped'} {pct(s.scoped)}"
                if s.scoped is not None
                else ""
            )
            print(
                f"5h {pct(s.five_hour)} · 7d {pct(s.seven_day)}{scoped} "
                f"(source: {s.source}, account: {s.account.get('email') or s.account})"
            )
        print(f"decision: {decision.action}")
        for reason in decision.reasons:
            print(f"  - {reason}")
    return {"ok": EXIT_OK, "pause": EXIT_PAUSE, "degrade": EXIT_DEGRADE}[
        decision.action
    ]


def _cmd_policy(args: argparse.Namespace) -> int:
    try:
        cfg = load_project_config(Path(args.root).resolve())
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    policy = cfg.policy()
    if args.json:
        print(json.dumps(policy, indent=2, sort_keys=True))
        return EXIT_OK
    print(f"project: {policy['project']}")
    print(f"deployment_type: {policy['deployment_type']}")
    print(f"forge: {policy['forge']}")
    print("models:")
    for role, model in policy["models"].items():
        print(f"  {role}: {model}")
    print(f"review_floor: {policy['review_floor']}")
    print(f"red_green: {policy['red_green']}")
    print(f"ci_on_push: {policy['ci_on_push']} · ci_on_merge: {policy['ci_on_merge']}")
    print(f"qa_board: {policy['qa_board']}")
    print(f"close_pass: {policy['close_pass']}")
    print(
        f"merge: converge={policy['merge']['converge']} "
        f"reviewer={policy['merge']['reviewer']}"
    )
    return EXIT_OK


def _cmd_notify(args: argparse.Namespace) -> int:
    cfg = load_user_config(args.user_config)
    notifier = notify_mod.Notifier.from_user_config(cfg)
    try:
        delivered = notifier.notify(args.kind, args.message)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if not delivered:
        print(f"error: {notifier.last_error}", file=sys.stderr)
        return EXIT_ERROR
    print("notified")
    return EXIT_OK


def _cmd_hook(args: argparse.Namespace) -> int:
    if args.hook != "pre-tool-use":
        print(f"error: unknown hook {args.hook!r}", file=sys.stderr)
        return EXIT_ERROR
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return EXIT_OK  # unrecognized input: the fence denies only what it can parse
    allow, reason = hooks.evaluate_pre_tool_use(payload, args.root)
    response = hooks.hook_response(allow, reason)
    if response is not None:
        print(json.dumps(response))
    return EXIT_OK


def _cmd_start(args: argparse.Namespace) -> int:
    from claudomater.run import start_run

    try:
        log, cfg = start_run(Path(args.root).resolve(), run_id=args.run_id)
    except RunError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(f"run {log.run_id} started for {cfg.project} ({cfg.deployment_type})")
    print(f"tail -f {log.run_dir.parent / 'current' / 'progress.log'}")
    return EXIT_OK


def _cmd_control(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    try:
        if args.run and args.run != "current":
            run_dir = runs_root(root) / validate_run_id(args.run)
            if not run_dir.is_dir():
                raise RunError(f"no run {args.run!r} under {runs_root(root)}")
            log = RunLog(run_dir, args.run)
        else:
            log = RunLog._attach(runs_root(root) / "current")
            if log is None:
                raise RunError(f"no current run under {runs_root(root)}")
        log.write_control(args.action)
    except RunError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(f"control event written: {args.action} -> run {log.run_id}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="omater", description="claudomater pipeline tooling"
    )
    parser.add_argument("--version", action="version", version=f"omater {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="provision a project (hooks, config, gitignore)")
    p.add_argument("root", nargs="?", default=".")
    p.add_argument("--verify", action="store_true", help="drift check only")
    p.add_argument("--force", action="store_true", help="overwrite existing config")
    p.set_defaults(fn=_cmd_init)

    p = sub.add_parser("usage", help="fetch usage and evaluate guardrails")
    p.add_argument("--json", action="store_true")
    p.add_argument("--user-config", default=None, help=argparse.SUPPRESS)
    p.set_defaults(fn=_cmd_usage)

    p = sub.add_parser("policy", help="show the project's resolved policy")
    p.add_argument("root", nargs="?", default=".")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=_cmd_policy)

    p = sub.add_parser("notify", help="send a Slack notification")
    p.add_argument("kind", choices=notify_mod.KINDS)
    p.add_argument("message")
    p.add_argument("--user-config", default=None, help=argparse.SUPPRESS)
    p.set_defaults(fn=_cmd_notify)

    p = sub.add_parser("hook", help="hook entrypoints (called by Claude Code)")
    p.add_argument("hook", choices=["pre-tool-use"])
    p.add_argument("--root", required=True)
    p.set_defaults(fn=_cmd_hook)

    p = sub.add_parser(
        "start", help="start a run (drift check + run log + policy record)"
    )
    p.add_argument("root", nargs="?", default=".")
    p.add_argument("--run-id", default=None)
    p.set_defaults(fn=_cmd_start)

    p = sub.add_parser(
        "control", help="answer a paused/escalated run (resume | abort | approve)"
    )
    p.add_argument("action", choices=CONTROL_ACTIONS)
    p.add_argument("--root", default=".")
    p.add_argument("--run", default="current")
    p.set_defaults(fn=_cmd_control)

    # `omater resume | abort | approve` — the shapes the notifications name.
    for action in CONTROL_ACTIONS:
        p = sub.add_parser(action, help=f"shorthand for `omater control {action}`")
        p.add_argument("--root", default=".")
        p.add_argument("--run", default="current")
        p.set_defaults(fn=_cmd_control, action=action)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
