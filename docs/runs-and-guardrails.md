# Runs, containment, and guardrails

## Run lifecycle

```bash
omater start [ROOT]      # drift check, arm fence + commit guard, open run log
...                      # driver runs phases
omater teardown [ROOT]   # disarm everything
```

A driver that started a run ends it with `omater teardown` after the
terminal event. Between runs the fence must not exist - `omater init
--verify` is the drift check. `omater start` refuses a second live run and
refuses provisioning drift.

## Run log

Every run writes `.omater/runs/<run-id>/`:

- `progress.log` - human-readable, append-only. `tail -f
  .omater/runs/current/progress.log` is the replacement for watching a
  terminal pane.
- `events.jsonl` - structured events. State transitions are written BEFORE
  the action executes (write-ahead), so a fresh orchestrator can adopt an
  orphaned run by replaying events against reality.
- `transcripts/` - retained phase transcripts, scrubbed against the
  project's `secrets_deny` list.

## Write fence

A PreToolUse hook that denies Write/Edit outside the project root and
pattern-matches Bash for out-of-tree writes. It is **run-scoped and
agent-scoped**: `omater start` arms it into the consumer repo's
`.claude/settings.json`, `omater teardown` removes it, and while armed it
self-disarms for any session not carrying the `OMATER_PHASE_AGENT` marker
the phase executor injects - a project-level hook fires in EVERY Claude
session in the repo, and the fence contains spawned agents, never the
human.

The fence is a redirector for tool-shaped writes, **not a jail**: writes
constructed inside quoted interpreter code (`python -c`, tempfile) pass the
Bash scan by design. The measured backstop is the per-phase
`permission_denials` capture in the run log plus verifier discipline.

`artifact_roots` in `.omater.yaml` declares directories that legitimately
resolve outside the tree (symlinked artifact repos) as committed,
reviewable exceptions.

## Commit guard

A run-scoped, agent-gated git `pre-commit` hook that blocks a phase agent's
commits touching paths outside the run's declared write scope
(`commit_scope`, per repo: `"."` and each `artifact_roots` git repo). Same
lifecycle as the fence; operator commits pass ungated. **Fail-closed while
gated**: a missing scope file, unreadable config, or missing `omater`
binary blocks the commit instead of waving it through. Arming refuses
foreign pre-commit hooks and any `core.hooksPath` redirection; the armed
scope is written to the run log. It composes with the fence at the git
layer - the staged file list is exact, no bash parsing involved.

## Usage guardrails

`omater usage` fetches the OAuth usage endpoint via a credential-provider
chain (env token, macOS keychain, Claude Code credentials file) and
evaluates the operator's thresholds. Exit codes: 0 ok, 3 pause, 4 degrade.

- **Fails closed on unknown data**: no credentials, an unreadable cache, or
  a malformed reading reads as over-threshold - pause + notify, never run
  blind.
- **Stale is not unknown**: when the cached reading's account provenance
  matches the active account, a pause requires staleness AND a near-limit
  reading (projected forward at 0.5 pp/min - self-capping, so old readings
  eventually pause anyway). A stale reading nowhere near a limit proceeds
  at degraded confidence. Degrades never act on stale data.
- Per-window `pause | degrade` behavior, the scoped-quota degrade path, and
  account-switch re-baselining come from `~/.omater/config.yaml`.
- `OMATER_FAKE_USAGE` injects a fake reading so every guardrail branch is
  testable in CI.

## Park and resume

Park-not-terminate: a paused phase outcome is recoverable, not fatal.
`guardrails.make_guardrail_check` builds the spawn-gate callable with its
account baseline seeded from the run log (a park/resume boundary keeps
account-switch detection), and `guardrails.wait_for_unpark` is the resume
loop a driver calls on a paused outcome - it polls the gate and watches the
control channel, so a parked run wakes on capacity, an account switch, or
an operator `resume`/`abort`, not just clock-or-human.

## Control channel

```bash
omater resume | abort | approve [--run ID]     # shorthands for `omater control ...`
```

Writes a control event a paused or escalated run consumes.

## Notifications

With `slack_webhook` configured: `PAUSED-QUOTA`, `DEGRADED`, `ESCALATED`,
`RUN-COMPLETE`, `PROMPT-BLOCKED` - sent the moment the state changes, so an
overnight run never saves its bad news for the morning. Manual send:
`omater notify KIND MESSAGE`.
