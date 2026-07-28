# story-automator

Python implementation of BMAD `story-automator`.

This package is the Python port of [`bma-d/lz-story-automator-go`](https://github.com/bma-d/lz-story-automator-go).

Status: works as the Python runtime bundled by this repository, but has been tested less than the Go implementation.

## Setting up a new BMAD project

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
