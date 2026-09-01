# CLI reference

`omater --help` for the live list; `omater CMD --help` for full flags.

## Project and runs

| Command | What it does |
|---|---|
| `omater init [ROOT] [--verify] [--force]` | Provision config template + gitignore; `--verify` = between-runs drift check (exit 1 on drift) |
| `omater start [ROOT]` | Start a run: drift check, arm the write fence + commit guard, create the run log, write the resolved policy |
| `omater teardown [ROOT]` | Disarm the write fence and the commit guard (root repo + artifact-root repos) |
| `omater policy [ROOT] [--json]` | Show the resolved policy (model chain, review floor, CI tier) for the project's `deployment_type` |
| `omater usage [--json]` | Fetch usage, evaluate guardrails; exit 0 ok / 3 pause / 4 degrade |
| `omater notify KIND MESSAGE` | Send a Slack notification through the configured webhook |
| `omater resume \| abort \| approve [--run ID]` | Write a control event a paused/escalated run consumes (also `omater control ...`) |
| `omater hook pre-tool-use --root PATH` | The provisioned PreToolUse write fence (hook payload on stdin) |
| `omater hook pre-commit` | The commit guard's git hook entrypoint (agent-gated; fail-closed past the gate) |

## Learning

| Command | What it does |
|---|---|
| `omater learn add \| supersede --scope S --domain D --topic T --rule R --why W` | Classified lesson writes (scrubbed via `--project`'s `secrets_deny`) |
| `omater learn refine --scope S --domain D --topic T [--rule R] [--why W]` | Merge better wording into the existing lesson |
| `omater learn list [--scope S] [--domain D]` | Live lessons (superseded rows never surface) |
| `omater learn search QUERY [--scope S]` | FTS over rule+why, live rows only |
| `omater learn candidates` | Promotion candidates (3+ uses across 2+ runs) for human review |
| `omater learn promote --scope S --domain D --topic T` | HUMAN-gated: make a lesson always-loaded for its scope (line-budgeted) |
| `omater learn export \| import \| sync [--push]` | Deterministic per-scope JSONL export; import (latest wins); sync = git pull, then import, export, and commit |

## Sprint

| Command | What it does |
|---|---|
| `omater sprint import PATH [--prune]` | Seed the DB from a `sprint-status.yaml`; `--prune` also drops tracked rows the file no longer carries (opt-in). Run before the first `set` of any session |
| `omater sprint export PATH` | Write the DB's statuses back through the file (byte-exact apart from flipped tokens) |
| `omater sprint set KEY STATUS PATH` | Flip one status: validated for the key's kind, DB first, then written through |
| `omater sprint status [--epic N] [--json]` | The sprint view, rendered from the tables |
| `omater sprint add-epic EPIC PATH --epic-file FILE [--story KEY]...` | Create an epic block (byte-exact insertion). `--epic-file` must carry a `## Definition of Done` section; the retro line is always `fable-review-required` |
| `omater sprint check-retros PATH` | Fail if any `*-retrospective:` line carries the banned `optional`; missing file fails loudly |

## Gates, sweep, report

| Command | What it does |
|---|---|
| `omater gate completion --story-file P --merge-sha S [ROOT]` | Completion-integrity gate: tasks + File List vs the merge commit, exempt list from config only, invocation logged to the live run |
| `omater gate close-epic EPIC --sprint PATH [ROOT]` | Epic close: artifact-repo pushed precheck, board gate, matrix audited-count vs the sprint file's story count (mismatch fails loudly) |
| `omater sweep --range A..B [--repo R]` | Conventions sweep over a diff's ADDED lines (em-dashes outside code spans, attribution footers); exit 0 clean, 2 on findings |
| `omater report --metrics PATH [--epic N]` | Per-epic table or cross-epic trends from the run-metrics JSONL the finish flow writes |
