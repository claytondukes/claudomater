"""Run startup: arm the fence, drift check, config load, run log, policy.

This is the seam the orchestrator session calls before its first phase:
- the write fence is ARMED here (hooks.provision) and lives only for the
  run - teardown (`omater teardown` / hooks.deprovision) removes it. The
  fence is run-scoped since parity finding P1-1: a project-level hook fires
  in EVERY Claude session in the repo, so between runs it must not exist,
  and while a run is live the AGENT_ENV marker keeps it inert for any
  session that is not an omater-spawned agent;
- `omater init --verify` semantics run after arming (config/gitignore/PATH
  drift, plus fence canonical-form check);
- the resolved policy is written to the run log, so changing
  `deployment_type` visibly changes the model chain, review floor, and CI
  tier in the run log itself.
"""

from __future__ import annotations

from pathlib import Path

from claudomater import hooks, initcmd
from claudomater.config import ProjectConfig, load_project_config
from claudomater.phases import worktree_dirt_paths
from claudomater.runlog import RunError, RunLog


def start_run(
    project_root: Path | str, run_id: str | None = None
) -> tuple[RunLog, ProjectConfig]:
    root = Path(project_root).resolve()
    try:
        hooks.provision(root)  # arm the run-scoped fence (idempotent)
    except hooks.HookProvisionError as exc:
        raise RunError(f"cannot arm the write fence: {exc}") from exc
    try:
        problems = initcmd.run_verify(root) + hooks.verify(root, require=True)
        if problems:
            raise RunError(
                "refusing to start a run with provisioning drift: "
                + "; ".join(problems)
            )
        cfg = load_project_config(root)
        log = RunLog.create(root, run_id=run_id)
    except BaseException as exc:
        # A start that FAILS must not leave the fence armed - the lifecycle
        # is "exists only while a run is live". One exception: when the
        # failure is the one-live-run conflict (or anything else while a
        # live run exists), that run owns the fence and it must stay.
        try:
            RunLog.attach(root)  # raises unless a live run exists
        except RunError:
            try:
                hooks.deprovision(root)
            except hooks.HookProvisionError as cleanup:
                # The ORIGINAL failure is the story; a cleanup failure must
                # never mask it. Attach it as a note so both surface.
                exc.add_note(
                    "cleanup also failed: could not disarm the write fence "
                    f"({cleanup}); remove it with `omater teardown`"
                )
        raise
    log.event("run", "policy", cfg.policy())
    # The salvage exclusion baseline (F2): paths dirty at run START are the
    # operator's deliberately-uncommitted state, and `wip(phase-crash)`
    # salvage must never sweep them onto the branch. Recorded in the run
    # log — not process memory — so crash-recovery adoption inherits the
    # ORIGINAL baseline instead of mistaking crashed-phase work for it.
    log.event(
        "run", "worktree-baseline", {"paths": sorted(worktree_dirt_paths(root))}
    )
    return log, cfg
