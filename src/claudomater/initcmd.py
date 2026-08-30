"""`omater init`: provision a consumer repo.

- writes a starter `.omater.yaml` (if missing),
- gitignores the runs/scratch dir.

The PreToolUse write fence is NOT installed here (parity finding P1-1): a
project-level hook fires in EVERY Claude session in the repo, humans
included, so the fence is run-scoped - `run.start_run` arms it, teardown
(`omater teardown`) removes it, and the AGENT_ENV marker keeps it inert for
non-agent sessions while armed.

`omater init --verify` is the between-runs drift check: config, gitignore,
PATH, and - if a fence hook is PRESENT - its canonical form (missing is the
healthy between-runs state; a drifted leftover reports loudly).
"""

from __future__ import annotations

import shutil
from pathlib import Path

from claudomater import hooks
from claudomater.config import ConfigError, PROJECT_CONFIG_NAME, load_project_config

GITIGNORE_LINE = ".omater/"
# command_ok verifiers running pytest in the consumer repo leave a
# .pytest_cache/ at its root; ignore it up front rather than letting a dev
# phase commit it. Convenience, not drift: `--verify` requires only
# GITIGNORE_LINE (run logs must never be committed; a cache dir is harmless).
GITIGNORE_EXTRA_LINES = (".pytest_cache/",)

TEMPLATE = """\
# claudomater project config — committed, so pipeline behavior is reviewable
# and versioned like code. See the claudomater README for every knob.
project: {project}
deployment_type: sandbox   # sandbox | internal | production | mission-critical
forge: github              # github | bitbucket

# models: {{}}             # every role is a knob; deployment_type sets defaults
#   orchestrator: ...
#   create: ...
#   dev: ...
#   sr_review: ...
#   merge: ...
#   escalation: ...
#   lessons: ...           # the close pass (skip at sandbox)

merge:
  converge: required       # required | off
  reviewer: copilot        # copilot (GitHub-only) | agent

secrets_deny: []           # env var names scrubbed from retained transcripts

adapters:
  issue_tracker: null      # core ships the null adapter; wire your own here
  qa_board: null

learning:
  scopes: [global]

ci:
  tier_on_push: fast       # fast | full
  tier_on_merge: full

gates:
  copilot_max_rounds_kpi: 1   # a target the run report scores
  review_round_alarm: 15      # hard stop: past this, page the operator with a
                              # surface diagnosis instead of grinding
  board_steps_required: false
"""


def run_init(root: Path | str, force: bool = False) -> list[str]:
    root = Path(root).resolve()
    actions: list[str] = []

    cfg_path = root / PROJECT_CONFIG_NAME
    if force or not cfg_path.exists():
        cfg_path.write_text(TEMPLATE.format(project=root.name), encoding="utf-8")
        actions.append(f"wrote {cfg_path}")
    else:
        actions.append(f"kept existing {cfg_path}")

    # The write fence is run-scoped (P1-1): start_run arms it, teardown
    # removes it. init installs nothing into .claude/settings.json.
    gitignore = root / ".gitignore"
    lines = (
        gitignore.read_text(encoding="utf-8").splitlines()
        if gitignore.exists()
        else []
    )
    missing = [
        line
        for line in (GITIGNORE_LINE, *GITIGNORE_EXTRA_LINES)
        if line not in lines
    ]
    if missing:
        with open(gitignore, "a", encoding="utf-8") as fh:
            if lines and lines[-1].strip():
                fh.write("\n")
            fh.write("\n".join(missing) + "\n")
        actions.append(f"gitignored {', '.join(missing)}")
    else:
        actions.append(f"{GITIGNORE_LINE} already gitignored")

    scratch = root / hooks.SCRATCH_SUBDIR
    scratch.mkdir(parents=True, exist_ok=True)

    if shutil.which("omater") is None:
        # A hook whose command isn't on PATH exits 127, which Claude Code
        # treats as allow — the fence would be a silent no-op.
        actions.append(
            "WARNING: 'omater' is not on PATH; the provisioned hook will be "
            "a no-op until claudomater is installed (pip install claudomater)"
        )
    return actions


def run_verify(root: Path | str) -> list[str]:
    """Drift detection. Empty list = healthy; the orchestrator refuses to
    start a run while this reports problems. The fence hook is checked in
    require=False mode: missing is the healthy between-runs state (the run
    path re-checks with require=True AFTER start_run arms it)."""
    root = Path(root).resolve()
    problems = hooks.verify(root, require=False)

    cfg_path = root / PROJECT_CONFIG_NAME
    if not cfg_path.exists():
        problems.append(f"{cfg_path} missing — run `omater init`")
    else:
        try:
            load_project_config(root)
        except ConfigError as exc:
            problems.append(str(exc))

    gitignore = root / ".gitignore"
    try:
        lines = (
            gitignore.read_text(encoding="utf-8").splitlines()
            if gitignore.exists()
            else []
        )
    except OSError as exc:
        lines = []
        problems.append(f"cannot read {gitignore}: {exc}")
    if GITIGNORE_LINE not in lines:
        problems.append(
            f"{GITIGNORE_LINE} not in .gitignore — run logs must never be committed"
        )

    if shutil.which("omater") is None:
        # The provisioned hook shells out to `omater`; command-not-found is
        # exit 127, which Claude Code treats as ALLOW — the write fence would
        # be silently disarmed (e.g. a non-activated venv at run time).
        problems.append(
            "'omater' is not on PATH — the write-fence hook would be a silent "
            "no-op; activate the environment claudomater is installed in"
        )
    return problems
