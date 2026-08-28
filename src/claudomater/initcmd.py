"""`omater init`: provision a consumer repo.

- writes a starter `.omater.yaml` (if missing),
- provisions the PreToolUse write-fence hook into `.claude/settings.json`,
- gitignores the runs/scratch dir.

`omater init --verify` is the drift check the orchestrator runs at every
run start: it re-verifies the hooks and config without changing anything.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from claudomater import hooks
from claudomater.config import ConfigError, PROJECT_CONFIG_NAME, load_project_config

GITIGNORE_LINE = ".omater/"

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
  copilot_max_rounds_kpi: 1
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

    if hooks.provision(root):
        actions.append(f"provisioned write-fence hook in {hooks.settings_path(root)}")
    else:
        actions.append("write-fence hook already provisioned")

    gitignore = root / ".gitignore"
    lines = (
        gitignore.read_text(encoding="utf-8").splitlines()
        if gitignore.exists()
        else []
    )
    if GITIGNORE_LINE not in lines:
        with open(gitignore, "a", encoding="utf-8") as fh:
            if lines and lines[-1].strip():
                fh.write("\n")
            fh.write(GITIGNORE_LINE + "\n")
        actions.append(f"gitignored {GITIGNORE_LINE}")
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
    start a run while this reports problems."""
    root = Path(root).resolve()
    problems = hooks.verify(root)

    cfg_path = root / PROJECT_CONFIG_NAME
    if not cfg_path.exists():
        problems.append(f"{cfg_path} missing — run `omater init`")
    else:
        try:
            load_project_config(root)
        except ConfigError as exc:
            problems.append(str(exc))

    gitignore = root / ".gitignore"
    lines = (
        gitignore.read_text(encoding="utf-8").splitlines()
        if gitignore.exists()
        else []
    )
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
