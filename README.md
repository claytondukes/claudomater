# claudomater

Owned dev-pipeline orchestration for Claude Code. One orchestrator session
drives a pipeline of subagent phases - each with an explicit contract
(model, prompt, structured JSON result) and a verifier the orchestrator
runs against reality (files, git, CI) before advancing. No terminal
scraping, no pane watching: progress is a tail-able run log, and usage
guardrails pause or degrade runs before an account's quota does it for you.

> Formerly `claude-o-mator`. The v2 CLI is `omater`; the legacy v1
> `story-automator` skill (BMAD-coupled) is still bundled until cutover.

## Quick start

```bash
pipx install .        # or: pip install -e .
omater init           # in the project you want to orchestrate
omater usage          # guardrail snapshot + decision
omater start          # arm the fence + commit guard, open the run log
tail -f .omater/runs/current/progress.log
...                   # your driver runs phases
omater teardown       # disarm everything - a driver that starts a run ends it
```

No BMAD files or tooling are required - see
[docs/bmad-interop.md](docs/bmad-interop.md) for the fresh-repo minimums
and how claudomater runs alongside an existing BMAD project.

## Documentation

| Topic | Doc |
|---|---|
| Install, provision a project, first run | [docs/getting-started.md](docs/getting-started.md) |
| `.omater.yaml` and `~/.omater/config.yaml`, every knob | [docs/configuration.md](docs/configuration.md) |
| Run lifecycle, write fence, commit guard, usage guardrails, park/resume, notifications | [docs/runs-and-guardrails.md](docs/runs-and-guardrails.md) |
| Phase contracts, verifiers, prompt injection, writing a driver | [docs/phases.md](docs/phases.md) |
| Lesson store, sync, injection credit, human-gated promotion | [docs/learning.md](docs/learning.md) |
| Sprint tracking, completion + epic-close gates, QA-board finish flow, conventions sweep, run metrics | [docs/story-pipeline.md](docs/story-pipeline.md) |
| BMAD: fresh repos and running alongside | [docs/bmad-interop.md](docs/bmad-interop.md) |
| Full command reference | [docs/cli.md](docs/cli.md) |
| Legacy v1 story-automator | [docs/legacy-story-automator.md](docs/legacy-story-automator.md) |

## Design stance

Every gate fails loudly and closed: a count that cannot be read never reads
as matching, a missing config never reads as permission, and "no check
needed" is always a recorded decision rather than a silence. Pipeline
behavior lives in the committed `.omater.yaml`, so process changes are
reviewable diffs; account facts live in the operator's
`~/.omater/config.yaml`. Core provides composable seams - drivers compose
them.

## Development

```bash
pip install -e '.[dev]'
pytest
```
