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
  credentials file) and **fails closed on unknown data**: no credentials, an
  unreadable cache, or a malformed reading reads as over-threshold → pause +
  notify, never run blind. A *stale* cache is not unknown: when the last
  reading's recorded account provenance matches the active account, a pause
  requires staleness AND a near-limit reading (the reading projected forward
  at 0.5 pp/min reaching a pause threshold — self-capping, so old readings
  eventually pause anyway); a stale reading that is nowhere near a limit
  proceeds at degraded confidence instead of pausing a healthy run. Degrades
  never act on stale data. Per-window `pause | degrade` behavior, a
  scoped-quota degrade path, and account-switch re-baselining come from user
  config. A fake-usage injection path (`OMATER_FAKE_USAGE`) makes every
  guardrail branch testable in CI. Park recovery is first-class:
  `guardrails.make_guardrail_check` builds the spawn-gate callable with its
  account baseline seeded from the run log (so a park/resume boundary keeps
  account-switch detection), and `guardrails.wait_for_unpark` is the resume
  loop a driver calls on a paused outcome — it polls the gate and watches
  the control channel, so a parked run wakes on capacity, an account
  switch, or an operator `resume`/`abort`, not just clock-or-human.
- **Slack notifications** — `PAUSED-QUOTA`, `DEGRADED`, `ESCALATED`,
  `RUN-COMPLETE`, `PROMPT-BLOCKED`, sent the moment the state changes so an
  overnight run never saves its bad news for the morning.
- **Learning store** (`omater learn`) — cross-project lessons in a local
  SQLite index (`learning.db_path`, never committed: FTS retrieval and the
  volatile use-counters live here) with a **deterministic JSONL export as
  the source of truth** (`learning.export_path`, git-carried: one file per
  scope, rows sorted, byte-reproducible, volatile counters excluded, written
  through on every DB write). Writes are classified — `add` refuses an
  existing live key, `refine` merges wording, `supersede` writes a new
  judgment and keeps the old row as audit trail — and lesson content passes
  the same `secrets_deny` scrub as transcripts: the corpus outlives the run
  that produced it. `omater learn sync` = pull → import (latest `updated_at`
  wins, supersession chains relinked) → export → commit with an
  `omater-learn:` prefix; the local index is fully reconstructible from the
  JSONL at any time. **Injection closes the write-only-corpus loop**: a
  phase gets its scopes' promoted (always-loaded) lessons plus a
  domain-seeded FTS retrieval (budgeted, refs-ranked) rendered as FRAMED
  DATA with ids; the injected set is a run-log event before the agent
  exists, the result's `lessons_applied` is validated against exactly that
  set (an id never injected mints no credit, failed phases mint nothing),
  and only validated uses move the `refs`/`sessions` counters. Promotion
  stays HUMAN-gated: `omater learn candidates` surfaces lessons used 3+
  times across 2+ runs, and only an operator's `omater learn promote`
  (scope-budgeted) makes a lesson always-loaded — the tool never
  self-promotes; auto-promotion is an instruction-injection channel.
- **Sprint tracking** (`omater sprint`) — the DB is the writer and
  `sprint-status.yaml` is a **byte-exact write-through export** until the
  file retires. That file is not a data file with comments on it: it is a
  curated audit record (rules preamble, per-line justifications, a
  STRUCTURAL CHANGE LOG that is the only account of why epics were
  re-sliced) that happens to carry a status map, so a regenerating
  exporter would destroy it and read as success. Instead the export is a
  **span model**: every line keeps its raw bytes, an entry line also
  records the character offsets of its status token, and a flip is
  `raw[:start] + new + raw[end:]`. Indentation, separator spacing, inline
  comments and their exact gap survive *by construction* — there is no
  code path that could reformat them. Reading NEVER validates a status:
  `optional` was banned as a retro value in 2026-08-21, and historical
  `optional` lines are audit trail an exporter must carry through, not
  correct — the vocabulary gates writes only, and `import` reports legacy
  values instead of fixing them. Epic membership is read POSITIONALLY,
  because a story key cannot be parsed (a sub-epic `epic-4-5` makes
  `4-5-1-...` ambiguous between epic 4 and epic 4-5). A key the DB tracks
  but the file lacks is a loud failure, never an appended line: choosing
  where a new story belongs is a planning decision the exporter has no
  basis to make. A key the file drops is the mirror case: reported, and
  removed only by an explicit `import --prune`, because the DB is on its
  way to being the writer and a truncated file must not delete real
  tracking as a side effect.
- **`omater init`** — writes a starter `.omater.yaml` and gitignores the
  runs dir. The PreToolUse write fence (denies Write/Edit outside the
  project root, pattern-matches Bash for out-of-tree writes) is RUN-SCOPED
  and AGENT-SCOPED: `omater start` arms it into the consumer repo's
  `.claude/settings.json`, `omater teardown` removes it, and while armed it
  self-disarms (allows) for any session not carrying the `OMATER_PHASE_AGENT`
  marker the phase executor injects — a project-level hook fires in EVERY
  Claude session in the repo, and the fence contains spawned agents, never
  the human (parity finding P1-1). Driver contract: a driver that started a
  run ends it with `omater teardown` after the terminal event — between
  runs the fence must not exist. `omater init --verify` is the
  between-runs drift check. The fence is a redirector for tool-shaped
  writes, **not a jail**: writes constructed inside quoted interpreter code
  (`python -c`, tempfile) pass the Bash scan by design; the measured
  backstop is the per-phase `permission_denials` capture in the run log
  plus verifier discipline.
- **Commit guard** — a run-scoped, agent-gated git `pre-commit` hook that
  blocks a phase agent's commits touching paths outside the run's declared
  write scope (`.omater.yaml` `commit_scope`, per repo: `"."` and each
  `artifact_roots` git repo). Same P1-1 lifecycle as the fence (`omater
  start` arms, `omater teardown` removes, operator commits pass ungated),
  but FAIL-CLOSED while gated: a missing scope file, unreadable config, or
  missing `omater` binary blocks the commit instead of waving it through.
  It composes with the fence at the git layer — the staged file list is
  exact, no bash parsing involved. Arming refuses foreign pre-commit hooks
  and any `core.hooksPath` that redirects hooks away from the repo's own
  `.git/hooks`; the armed scope is written to the run log.

### Install

```bash
pip install -e .
omater init          # in the project you want to orchestrate
omater usage         # guardrail snapshot + decision
```

### CLI

| Command | What it does |
|---|---|
| `omater init [ROOT] [--verify] [--force]` | Provision config template + gitignore; `--verify` = between-runs drift check (exit 1 on drift) |
| `omater start [ROOT]` | Start a run: arm the write fence + commit guard, drift check, run-log creation, resolved policy written to the log |
| `omater teardown [ROOT]` | Disarm the write fence and the commit guard (root repo + artifact-root repos) |
| `omater usage [--json]` | Fetch usage, evaluate guardrails; exit 0 ok / 3 pause / 4 degrade |
| `omater policy [ROOT] [--json]` | Show the resolved policy (model chain, review floor, CI tier) for the project's `deployment_type` |
| `omater notify KIND MESSAGE` | Send a Slack notification through the configured webhook |
| `omater learn add\|supersede --scope S --domain D --topic T --rule R --why W` | Classified lesson writes (scrubbed via `--project`'s `secrets_deny`) |
| `omater learn refine --scope S --domain D --topic T [--rule R] [--why W]` | Merge better wording into the existing lesson (at least one of `--rule`/`--why`) |
| `omater learn list [--scope S] [--domain D]` | Live lessons (superseded rows never surface) |
| `omater learn search QUERY [--scope S]` | FTS over rule+why, live rows only |
| `omater learn candidates` | Promotion candidates (3+ uses across 2+ runs) for human review |
| `omater learn promote --scope S --domain D --topic T` | HUMAN-gated: make a lesson always-loaded for its scope (line-budgeted) |
| `omater learn export\|import\|sync [--push]` | Deterministic per-scope JSONL export; import (latest wins); pull→import→export→commit |
| `omater sprint import PATH [--prune]` | Seed the DB from a `sprint-status.yaml`; `--prune` also drops tracked rows the file no longer carries (opt-in, never automatic) |
| `omater sprint export PATH` | Write the DB's statuses back through the file (byte-exact apart from flipped tokens) |
| `omater sprint set KEY STATUS PATH` | Flip one status: validated for the key's kind, written to the DB, then written through to the file |
| `omater sprint status [--epic N] [--json]` | The sprint view, rendered on demand from the tables |
| `omater sprint add-epic EPIC PATH [--story KEY]...` | Create a new epic block in the DB and the file: epic line, stories, and — always, last in the block — `epic-N-retrospective: fable-review-required` (the retro status is a constant; the banned `optional` is unrepresentable). Insertion is byte-exact: existing content survives untouched |
| `omater sprint check-retros PATH` | The retro-vocabulary gate: fail if any `*-retrospective:` line carries the banned `optional`; a missing file fails loudly (it must never read as a pass); prints the status distribution on success |
| `omater resume\|abort\|approve [--run ID]` | Write a control event a paused/escalated run consumes (also under `omater control …`) |
| `omater hook pre-tool-use --root PATH` | The provisioned PreToolUse write fence (reads the hook payload on stdin) |
| `omater hook pre-commit` | The commit guard's git hook entrypoint (gated on the agent marker; fail-closed past the gate) |

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
