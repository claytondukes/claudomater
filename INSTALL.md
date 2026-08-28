# Installing story-automator on a new machine

A portable Claude Code skill that automates the full BMAD story build cycle
(create → dev → automate → senior-review → retrospective) with resumable tmux
orchestration and an optional GitHub Copilot PR-review convergence loop.

## Prerequisites

- **Claude Code** (CLI)
- **Python ≥ 3.11** (`python3 --version`)
- **tmux** (`brew install tmux` / `apt install tmux`) — the orchestrator runs each step in a tmux session
- **ripgrep (`rg`)** (`brew install ripgrep` / `apt install ripgrep`) — the runtime step docs use `rg` to scan the state document
- **git**
- A **BMAD** project (the target repo has a `_bmad/` directory)
- **GitHub CLI (`gh`)** — only if you want the PR + Copilot review loop. Skip it and set `open_pr: false` / `copilot_loop: false` (see config below).
- **`jq`** (`brew install jq` / `apt install jq`) — only needed to run the bundled shell test scripts (`tests/*.sh`); not required for normal operation.

## 1. Install the skill into your Claude config

Clone straight into your Claude skills dir (default `~/.claude/skills`; if you
set `CLAUDE_CONFIG_DIR`, use that instead):

```bash
git clone git@github.com:claytondukes/claudomater.git ~/.claude/skills/story-automator
```

Update later with `git -C ~/.claude/skills/story-automator pull`. The skill is
self-contained (stdlib Python only — no `pip install` needed).

## 2. Bootstrap each BMAD project

From inside a BMAD project (or pass the path), run once:

```bash
~/.claude/skills/story-automator/scripts/story-automator setup
```

This is idempotent and only creates what's missing (`--force` to overwrite). It:

- installs the `bmad-story-automator-review` review-bridge skill into the project's `.claude/skills/`,
- writes `_bmad/automator/story-automator.yaml` (prefilled with the project name and a test gauntlet auto-detected from the stack: npm / cargo / go / pytest),
- points the Claude Code **Stop hook** at your install (the path is resolved for *your* machine automatically),
- gitignores the run marker.

The automator's init step also runs this automatically if it detects the review
bridge is missing, so you rarely need to call it by hand.

Health-check any project with:

```bash
~/.claude/skills/story-automator/scripts/story-automator doctor
```

## 3. Run it

In Claude Code, from the project root:

> run story automator

## Per-project config — `_bmad/automator/story-automator.yaml`

Every key is optional; safe defaults apply if the file (or any key) is absent.

| Key | Default | Purpose |
|-----|---------|---------|
| `project_name` | git dir name | tmux session prefix + branch slug |
| `test_gauntlet` | auto-detected | shell commands the automate step must all pass before review (`[]` = no gate) |
| `branch_pattern` | `epic{epic}/{story_slug}` | story branch name. Placeholders: `{epic}`, `{story_slug}`, `{story_id}`, `{story_prefix}` |
| `reviewer.bridge` | `bmad-story-automator-review` | the review-step skill (installed by setup) |
| `open_pr` | `true` | open a PR at the finish phase |
| `copilot_loop` | `true` | drive the GitHub Copilot review to convergence (needs `gh`) |

## Notes

- Requires the standard BMAD building-block skills (`bmad-create-story`,
  `bmad-dev-story`, `bmad-qa-generate-e2e-tests`, `bmad-retrospective`,
  `bmad-sprint-status`). These are stable across recent BMAD releases; if BMAD
  renames one, update its `skillCandidates` list in
  `data/orchestration-policy.json` (a one-line edit — `doctor` will tell you which).
- Memory writes go to `~/.claude/projects/<slugified-project-root>/memory/`
  (Claude Code's standard per-project memory location).
- Run the test suite from the skill root with: `PYTHONPATH=src python3 -m pytest -q`
