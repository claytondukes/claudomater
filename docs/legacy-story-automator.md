# Legacy v1: story-automator

The bundled v1 runtime is the Python implementation of the BMAD
`story-automator` skill (itself a port of an earlier Go implementation). It
remains functional and bundled until the v2 pipeline reaches full parity and
consumers cut over. **Unlike v2, it is BMAD-coupled**: it hard-requires a
BMAD project (a `_bmad/` directory and BMAD config) and installs as a
Claude Code skill.

- Install and prerequisites: [../INSTALL.md](../INSTALL.md)
- Skill entrypoint: [../SKILL.md](../SKILL.md), which follows
  [../workflow.md](../workflow.md)

## Bootstrapping a BMAD project for v1

A fresh BMAD install ships the standard `bmad-*` skills but not the
automator's review bridge, and never writes the per-project config:

```bash
.claude/skills/story-automator/scripts/story-automator setup [PROJECT_ROOT]
```

`PROJECT_ROOT` defaults to the git toplevel. Idempotent (creates only
what's missing; `--force` overwrites):

- installs the `bmad-story-automator-review` skill (the review-step bridge,
  hard-required by the policy),
- writes `_bmad/automator/story-automator.yaml` prefilled with the project
  name and a test gauntlet auto-detected from the stack (npm / cargo / go /
  pytest),
- verifies the Stop hook in `.claude/settings.json` points at the global
  skill, and gitignores the run marker.

The automator's init step runs this automatically when it detects the
review bridge is missing.

New projects should start on v2 (`omater`) instead - see
[getting-started.md](getting-started.md).
