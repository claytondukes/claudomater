# claudomater

Owned dev-pipeline orchestration for Claude Code. One orchestrator session
drives a pipeline of **subagent phases** — each with an explicit contract
(model, prompt, structured JSON result) and a **verifier** the orchestrator
runs against reality (files, git, CI) before advancing. No terminal
scraping, no pane watching: progress is a tail-able run log, and usage
guardrails pause or degrade runs before an account's quota does it for you.

> Formerly `claude-o-mator`. The v2 CLI is `omater`; the legacy v1
> `story-automator` runtime is still bundled (see below) until cutover.

## omater (v2)

Phase 0 skeleton — the pieces every pipeline run stands on:

- **Config loaders** — per-project `.omater.yaml` (committed, reviewable) and
  per-operator `~/.omater/config.yaml` (account facts: thresholds, degrade
  paths, webhooks). `deployment_type` (`sandbox` / `internal` / `production` /
  `mission-critical`) sets model-role defaults, the review-severity floor, and
  CI tier; every role is an overridable knob.
- **Run log** — every run writes `.omater/runs/<run-id>/progress.log`
  (human-readable, append-only) and `events.jsonl` (structured). State
  transitions are written *before* the action executes, so a fresh
  orchestrator can adopt an orphaned run by replaying events against reality.
  `tail -f .omater/runs/current/progress.log` is the new "watch the pane".
- **Phase runner** — spawns phase agents through an executor abstraction,
  requires a structured JSON result (an agent that ends without one is a
  failed phase), runs verifiers, retries once from the branch's last committed
  state (salvaging uncommitted work as `wip(phase-crash)`), then escalates.
  Retained transcripts are scrubbed against the project's `secrets_deny` list.
- **Usage guardrails** — `omater usage` fetches the OAuth usage endpoint via a
  credential-provider chain (env token → macOS keychain → Claude Code
  credentials file) and **fails closed**: no credentials or a stale cache
  reads as over-threshold → pause + notify, never run blind. Per-window
  `pause | degrade` behavior, a scoped-quota degrade path, and account-switch
  re-baselining come from user config. A fake-usage injection path
  (`OMATER_FAKE_USAGE`) makes every guardrail branch testable in CI.
- **Slack notifications** — `PAUSED-QUOTA`, `DEGRADED`, `ESCALATED`,
  `RUN-COMPLETE`, `PROMPT-BLOCKED`, sent the moment the state changes so an
  overnight run never saves its bad news for the morning.
- **`omater init`** — provisions the PreToolUse write-fence hook into the
  consumer repo's `.claude/settings.json` (denies Write/Edit outside the
  project root, pattern-matches Bash for out-of-tree writes), writes a
  starter `.omater.yaml`, and gitignores the runs dir. `omater init --verify`
  is the drift check run at every run start.

### Install

```bash
pip install -e .
omater init          # in the project you want to orchestrate
omater usage         # guardrail snapshot + decision
```

### CLI

| Command | What it does |
|---|---|
| `omater init [ROOT] [--verify] [--force]` | Provision hooks + config template; `--verify` = drift check (exit 1 on drift) |
| `omater start [ROOT]` | Start a run: drift check, run-log creation, resolved policy written to the log |
| `omater usage [--json]` | Fetch usage, evaluate guardrails; exit 0 ok / 3 pause / 4 degrade |
| `omater policy [ROOT] [--json]` | Show the resolved policy (model chain, review floor, CI tier) for the project's `deployment_type` |
| `omater notify KIND MESSAGE` | Send a Slack notification through the configured webhook |
| `omater resume\|abort\|approve [--run ID]` | Write a control event a paused/escalated run consumes (also under `omater control …`) |
| `omater hook pre-tool-use --root PATH` | The provisioned PreToolUse write fence (reads the hook payload on stdin) |

Development: `pip install -e '.[dev]' && pytest`.

---

## story-automator (legacy v1)

Python implementation of the BMAD `story-automator` (a port of the earlier Go
implementation). It remains functional and bundled until the v2 pipeline
reaches parity and consumers cut over.

### Setting up a new BMAD project

A fresh BMAD install ships the standard `bmad-*` skills but not the automator's
review bridge, and never writes the per-project config. Bootstrap a project with:

```bash
.claude/skills/story-automator/scripts/story-automator setup [PROJECT_ROOT]
```

`PROJECT_ROOT` defaults to the git toplevel of the current directory. The command
(idempotent — it only creates what's missing, `--force` to overwrite):

- installs the `bmad-story-automator-review` skill (the review-step bridge, hard-required by the policy),
- writes `_bmad/automator/story-automator.yaml` prefilled with the project name (from BMAD config) and a test gauntlet auto-detected from the stack (npm / cargo / go / pytest),
- verifies the Stop hook in `.claude/settings.json` points at the global skill, and gitignores the run marker.

The automator's init step runs this automatically when it detects the review
bridge is missing, so you rarely need to call it by hand.
