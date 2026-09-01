# Configuration

Two files, two owners:

- **`.omater.yaml`** at the project root - committed, reviewable, versioned
  like code. Pipeline behavior lives here.
- **`~/.omater/config.yaml`** - the operator's account facts. Optional; a
  missing file yields spec defaults.

## `.omater.yaml` (project)

`omater init` writes a commented starter. Full knob reference:

| Key | Meaning |
|---|---|
| `project` | Project name (keys sprint rows, run logs). |
| `deployment_type` | `sandbox` \| `internal` \| `production` \| `mission-critical`. Sets model-role defaults, the review-severity floor, and CI tier. Every derived value is individually overridable. |
| `forge` | `github` \| `bitbucket`. |
| `models` | Per-role overrides: `orchestrator`, `create`, `dev`, `sr_review`, `merge`, `escalation`, `lessons`. Unset roles come from `deployment_type`. |
| `merge.converge` | `required` \| `off` - whether a PR must reach review convergence before merge. |
| `merge.reviewer` | `copilot` (GitHub-only) \| `agent`. |
| `secrets_deny` | Env var NAMES whose values are scrubbed from retained transcripts and lesson content. |
| `adapters.issue_tracker` | Issue tracker wiring; `null` = none. |
| `adapters.qa_board` | QA-board wiring (mapping: `authoring_dir`, `board_url`, `gate_dir`, `gate` argv with `{epic}` substitution) or `null` = no board. Validated at load; a half-declared adapter refuses rather than reading as "no board". |
| `learning.scopes` | Lesson scopes injected into phases (for example `[global, typescript, myproject]`). |
| `artifact_roots` | Directories that hold this project's artifacts but resolve OUTSIDE the tree (typically a symlinked output dir). An explicit, committed exception to write containment - never a blanket symlink-follow. |
| `commit_scope` | Per-repo commit allowlist for the pre-commit guard: `"."` is the project root; any other key must be an `artifact_roots` git repo. An armed repo with no declared scope blocks every gated commit (fail-closed). |
| `surface_rules` | The QA-board surface gate's SURFACE-TOUCHING / exclusion pattern lists (`surface:`, `exclude:`, `exclude_root_dotfiles:`). Exclusions win; `**` respects path-segment boundaries. `null` = no surface gate declared. |
| `completion.exempt` | File List path prefixes that legitimately ride outside the merge commit (driver-owned artifacts in a separate repo). Config is the ONLY source of exemptions - no code path accepts an ad-hoc exempt list. |
| `conventions` | A list of standing style/policy rule strings, injected VERBATIM into every phase prompt via `phases.inject_conventions`. Policy belongs in config, not in a GO prompt's standing-rules paragraph. |
| `ci.tier_on_push` / `ci.tier_on_merge` | `fast` \| `full`. |
| `gates.copilot_max_rounds_kpi` | A target the run report scores (not a stop). |
| `gates.review_round_alarm` | Hard stop: past this many review rounds, page the operator with a surface diagnosis instead of grinding. |
| `gates.board_steps_required` | Whether surface stories must produce a board step. |

## `~/.omater/config.yaml` (operator)

| Key | Meaning |
|---|---|
| `usage.pause_at` | Per-window pause thresholds (percent). |
| `usage.on_threshold` | Per-window behavior at threshold: `pause` \| `degrade`. |
| `usage.degrade_scoped_at` | Scoped-quota (model-specific) degrade threshold; default 80. |
| `usage.degrade_path` | Ordered model list a degrade steps down through. |
| `slack_webhook` | Enables notifications ([runs-and-guardrails.md](runs-and-guardrails.md#notifications)). |
| `learning.db_path` | Local SQLite lesson index (never committed). Default `~/.omater/learning.db`. |
| `learning.export_path` | Git-carried JSONL lesson export directory (source of truth). Default `~/.dotfiles/omater/lessons`. |

Environment expansion (`${VAR}`) is applied when loading.
