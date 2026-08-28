"""Run startup: drift check, config load, run-log creation, policy logging.

This is the seam the orchestrator session calls before its first phase:
- `omater init --verify` semantics run first (hook drift detection at every
  run start);
- the resolved policy is written to the run log, so changing
  `deployment_type` visibly changes the model chain, review floor, and CI
  tier in the run log itself.
"""

from __future__ import annotations

from pathlib import Path

from claudomater import initcmd
from claudomater.config import ProjectConfig, load_project_config
from claudomater.runlog import RunError, RunLog


def start_run(
    project_root: Path | str, run_id: str | None = None
) -> tuple[RunLog, ProjectConfig]:
    root = Path(project_root).resolve()
    problems = initcmd.run_verify(root)
    if problems:
        raise RunError(
            "refusing to start a run with provisioning drift: " + "; ".join(problems)
        )
    cfg = load_project_config(root)
    log = RunLog.create(root, run_id=run_id)
    log.event("run", "policy", cfg.policy())
    return log, cfg
