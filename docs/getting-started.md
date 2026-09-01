# Getting started

## Install

```bash
pipx install .            # or: pip install -e .
omater --version
```

Development install: `pip install -e '.[dev]' && pytest`.

## Provision a project

From (or naming) the repo you want to orchestrate:

```bash
omater init [ROOT]
```

This writes a commented starter `.omater.yaml` (committed, so pipeline
behavior is reviewable and versioned like code) and gitignores the
`.omater/runs/` directory. It installs **nothing** into
`.claude/settings.json` - the write fence and commit guard are run-scoped,
armed by `omater start` and removed by `omater teardown`.

`omater init --verify` is the between-runs drift check (exit 1 on drift):
between runs the fence must not exist.

## Operator config (optional)

`~/.omater/config.yaml` carries account facts: usage thresholds and
per-window pause/degrade behavior, the degrade model path, the Slack
webhook, and the learning store paths (`learning.db_path`,
`learning.export_path`). A missing file yields spec defaults - nothing here
is required to start. See [configuration.md](configuration.md).

## First run

```bash
omater usage        # guardrail snapshot + decision (exit 0 ok / 3 pause / 4 degrade)
omater start        # drift check, arm fence + commit guard, open the run log
tail -f .omater/runs/current/progress.log
...                 # your driver runs phases (see phases.md)
omater teardown     # disarm everything; a driver that starts a run ends it
```

## Do I need BMAD?

No - the core is self-contained. The story-pipeline features (`sprint`,
`gate`, the QA-board finish flow, `report`) operate on convention-shaped
artifact files you author yourself, and on a previously-BMAD repo they run
alongside the existing files in place. Both cases:
[bmad-interop.md](bmad-interop.md).
