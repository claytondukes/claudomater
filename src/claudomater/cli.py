"""`omater` CLI.

Exit codes for `omater usage` (scriptable guardrail checks):
0 = ok, 2 = error, 3 = pause, 4 = degrade.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
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
    if not hooks.fence_active():
        # Not an omater-spawned agent session: the project-level hook fires
        # for EVERY Claude session in the repo, and fencing a human's
        # session is the P1-1 inversion. Allow, before even reading stdin.
        return EXIT_OK
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError, TypeError, OSError):
        # unrecognized/unreadable input: the fence denies only what it can
        # parse — raising here would disarm it for this invocation
        return EXIT_OK
    allow, reason = hooks.evaluate_pre_tool_use(payload, args.root)
    response = hooks.hook_response(allow, reason)
    if response is not None:
        print(json.dumps(response))
    return EXIT_OK


def _cmd_teardown(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    try:
        changed = hooks.deprovision(root)
    except hooks.HookProvisionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(
        f"write-fence hook removed from {hooks.settings_path(root)}"
        if changed
        else "write-fence hook was not installed; nothing to remove"
    )
    return EXIT_OK


def _open_store(args: argparse.Namespace):
    from claudomater import learnstore

    cfg = load_user_config(args.user_config)
    secrets_deny: list[str] = []
    if args.project:
        # the project's deny list guards lesson content written from its
        # runs — the corpus outlives the run that produced it
        secrets_deny = load_project_config(Path(args.project).resolve()).secrets_deny
    return learnstore.LearnStore.open(
        args.db or cfg.learning_db_path,
        export_dir=args.export_dir or cfg.learning_export_path,
        secrets_deny=secrets_deny,
    )


def _cmd_learn(args: argparse.Namespace) -> int:
    from claudomater import learnstore

    try:
        store = _open_store(args)
    except (ConfigError, learnstore.LearnStoreError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    try:
        if args.learn_cmd in ("add", "refine", "supersede"):
            fn = getattr(store, args.learn_cmd)
            kwargs = {"rule": args.rule, "why": args.why}
            if args.learn_cmd == "refine":
                kwargs = {k: v for k, v in kwargs.items() if v is not None}
            lesson_id = fn(args.scope, args.domain, args.topic, **kwargs)
            # echo the STORED (scrubbed) key, never the raw --topic: the
            # terminal/CI log is a retention surface like any other
            stored = store.conn.execute(
                "SELECT scope, domain, topic FROM lesson WHERE id=?",
                (lesson_id,),
            ).fetchone()
            print(
                f"{args.learn_cmd}: lesson {lesson_id} "
                f"({stored['scope']}/{stored['domain']}/{stored['topic']})"
            )
        elif args.learn_cmd == "list":
            rows = store.lessons(args.scope or ["global"], domains=args.domain or None)
            if args.json:
                print(json.dumps(rows, indent=2, sort_keys=True))
            else:
                for r in rows:
                    print(f"{r['scope']}/{r['domain']}/{r['topic']} [{r['status']}] {r['rule']}")
                print(f"{len(rows)} live lesson(s)")
        elif args.learn_cmd == "search":
            rows = store.search(args.query, args.scope or ["global"])
            if args.json:
                print(json.dumps(rows, indent=2, sort_keys=True))
            else:
                for r in rows:
                    print(f"{r['scope']}/{r['domain']}/{r['topic']} [refs {r['refs']}] {r['rule']}")
                print(f"{len(rows)} match(es)")
        elif args.learn_cmd == "candidates":
            rows = store.candidates()
            for r in rows:
                print(
                    f"{r['scope']}/{r['domain']}/{r['topic']} "
                    f"[refs {r['refs']}, sessions {r['sessions']}] {r['rule']}"
                )
            print(
                f"{len(rows)} promotion candidate(s); promote with "
                "`omater learn promote --scope S --domain D --topic T`"
            )
        elif args.learn_cmd == "promote":
            lesson_id = store.promote(args.scope, args.domain, args.topic)
            print(
                f"promoted: lesson {lesson_id} "
                f"({args.scope}/{args.domain}) is now always-loaded for its scope"
            )
        elif args.learn_cmd == "export":
            for path in store.export():
                print(f"exported {path}")
        elif args.learn_cmd == "import":
            stats = store.import_dir()
            print(f"imported: {stats.new} new, {stats.updated} updated, {stats.unchanged} unchanged")
        elif args.learn_cmd == "sync":
            result = learnstore.sync(store, push=args.push)
            print(
                f"sync: {result['new']} new, {result['updated']} updated, "
                f"committed={result['committed']}, pushed={result['pushed']}"
            )
    except learnstore.LearnStoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except sqlite3.DatabaseError as exc:
        # belt for the residual class: post-open sqlite failures (locked
        # index, disk-level trouble) stay a clean CLI error, not a traceback
        print(f"error: learning DB failure: {exc}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        store.close()
    return EXIT_OK


def _cmd_sprint(args: argparse.Namespace) -> int:
    from claudomater import learnstore, sprint as sprint_mod

    try:
        store = _open_store(args)
    except (ConfigError, learnstore.LearnStoreError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    path = Path(args.path).resolve() if getattr(args, "path", None) else None
    try:
        if args.sprint_cmd == "import":
            doc = sprint_mod.SprintDoc.read(path)
            count = sprint_mod.import_doc(store, args.sprint_project, doc)
            print(f"imported {count} row(s) from {path}")
            for entry in sprint_mod.unknown_statuses(doc):
                # surfaced, never corrected: legacy values are audit trail
                print(
                    f"  legacy value (left untouched): line {entry.line_no} "
                    f"{entry.key}: {entry.status}"
                )
        elif args.sprint_cmd == "export":
            changed = sprint_mod.export(store, args.sprint_project, path)
            print(f"{path}: {'rewritten' if changed else 'already in sync'}")
        elif args.sprint_cmd == "set":
            changed = sprint_mod.set_status(
                store, args.sprint_project, args.key, args.status, path
            )
            print(
                f"{args.key} -> {args.status} "
                f"({'file rewritten' if changed else 'file already in sync'})"
            )
        elif args.sprint_cmd == "status":
            rows = sprint_mod.stories(store, args.sprint_project, epic=args.epic)
            if args.json:
                print(json.dumps(rows, indent=2, sort_keys=True))
            else:
                for r in rows:
                    print(f"{r['key']}: {r['status']}")
                print(f"{len(rows)} tracked row(s)")
    except sprint_mod.SprintError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except OSError as exc:
        # `export`/`set` write as well as read, so a fixed "cannot read"
        # would misdirect triage on a full disk or a failed rename
        print(f"error: status file I/O failed ({path}): {exc}", file=sys.stderr)
        return EXIT_ERROR
    except sqlite3.DatabaseError as exc:
        print(f"error: learning DB failure: {exc}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        store.close()
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
        # attach(): liveness-checked (a finished run accepts no control) and
        # re-checked atomically at the event append — a bare _attach left
        # `omater resume|abort|approve` able to append control-* after a
        # terminal event and flip is_live() back on.
        log = RunLog.attach(root, run_id=args.run)
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

    p = sub.add_parser("init", help="provision a project (config, gitignore)")
    p.add_argument("root", nargs="?", default=".")
    p.add_argument("--verify", action="store_true", help="drift check only")
    p.add_argument("--force", action="store_true", help="overwrite existing config")
    p.set_defaults(fn=_cmd_init)

    p = sub.add_parser(
        "teardown",
        help="disarm the write fence (remove the run-scoped PreToolUse hook)",
    )
    p.add_argument("root", nargs="?", default=".")
    p.set_defaults(fn=_cmd_teardown)

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

    p = sub.add_parser("learn", help="learning store (local index + JSONL exports)")
    learn_sub = p.add_subparsers(dest="learn_cmd", required=True)

    def _learn_parser(name: str, help_text: str) -> argparse.ArgumentParser:
        lp = learn_sub.add_parser(name, help=help_text)
        lp.add_argument("--user-config", default=None, help=argparse.SUPPRESS)
        lp.add_argument("--db", default=None, help="override learning.db_path")
        lp.add_argument(
            "--export-dir", default=None, help="override learning.export_path"
        )
        lp.add_argument(
            "--project",
            default=None,
            help="project root whose secrets_deny scrubs lesson content",
        )
        lp.set_defaults(fn=_cmd_learn)
        return lp

    for verb, help_text in (
        ("add", "a genuinely new lesson (refuses an existing live key)"),
        ("refine", "merge better wording into the existing lesson"),
        ("supersede", "replace the old judgment; the old row stays as audit trail"),
    ):
        lp = _learn_parser(verb, help_text)
        lp.add_argument("--scope", required=True)
        lp.add_argument("--domain", required=True)
        lp.add_argument("--topic", required=True)
        required_content = verb != "refine"
        lp.add_argument("--rule", required=required_content, default=None)
        lp.add_argument("--why", required=required_content, default=None)

    lp = _learn_parser("list", "live lessons for the given scopes")
    lp.add_argument("--scope", action="append", default=None)
    lp.add_argument("--domain", action="append", default=None)
    lp.add_argument("--json", action="store_true")

    lp = _learn_parser("search", "FTS over rule+why, live rows only")
    lp.add_argument("query")
    lp.add_argument("--scope", action="append", default=None)
    lp.add_argument("--json", action="store_true")

    _learn_parser(
        "candidates", "promotion candidates (3+ uses across 2+ runs) for review"
    )
    lp = _learn_parser(
        "promote", "HUMAN-gated: make a lesson always-loaded for its scope"
    )
    lp.add_argument("--scope", required=True)
    lp.add_argument("--domain", required=True)
    lp.add_argument("--topic", required=True)

    _learn_parser("export", "write the deterministic per-scope JSONL export")
    _learn_parser("import", "import per-scope JSONL (latest updated_at wins)")
    lp = _learn_parser("sync", "git pull -> import -> export -> commit")
    lp.add_argument("--push", action="store_true", help="push after committing")

    p = sub.add_parser(
        "sprint", help="sprint tracking (DB writer, sprint-status.yaml export)"
    )
    sprint_sub = p.add_subparsers(dest="sprint_cmd", required=True)

    def _sprint_parser(name: str, help_text: str) -> argparse.ArgumentParser:
        sp = sprint_sub.add_parser(name, help=help_text)
        sp.add_argument("--user-config", default=None, help=argparse.SUPPRESS)
        sp.add_argument("--db", default=None, help="override learning.db_path")
        sp.add_argument(
            "--export-dir", default=None, help=argparse.SUPPRESS
        )
        sp.add_argument("--project", default=None, help=argparse.SUPPRESS)
        sp.add_argument(
            "--sprint-project",
            default="ui3",
            help="project name the story rows are keyed by",
        )
        sp.set_defaults(fn=_cmd_sprint)
        return sp

    sp = _sprint_parser("import", "seed the DB from an existing sprint-status.yaml")
    sp.add_argument("path", help="path to sprint-status.yaml")

    sp = _sprint_parser("export", "write the DB's statuses through to the file")
    sp.add_argument("path", help="path to sprint-status.yaml")

    sp = _sprint_parser("set", "flip one status in the DB and write through")
    sp.add_argument("key", help="epic / story / retrospective key")
    sp.add_argument("status", help="the new status (validated for the key's kind)")
    sp.add_argument("path", help="path to sprint-status.yaml")

    sp = _sprint_parser("status", "the sprint view, rendered from the tables")
    sp.add_argument("--epic", default=None, help="restrict to one epic")
    sp.add_argument("--json", action="store_true")

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
    except (ConfigError, hooks.HookProvisionError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
