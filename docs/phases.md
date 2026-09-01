# Phases and drivers

## The model

One orchestrator session (the **driver** - a human-authored script or a
Claude session) drives a pipeline of **subagent phases**. Each phase has an
explicit contract:

- a model (per-role from `deployment_type`, overridable in `models:`),
- a prompt,
- a required structured JSON result (an agent that ends without one is a
  failed phase),
- **verifiers** the runner executes against reality - files, git, CI -
  before the phase counts as done. Narration never satisfies a verifier.

No terminal scraping, no pane watching: progress is the tail-able run log.

## PhaseRunner

`claudomater.phases.PhaseRunner.run_phase(spec)`:

1. Spawns the phase agent through an executor abstraction
   (`ClaudeCliExecutor` runs `claude` CLI; tests inject fakes).
2. Requires the structured JSON result and checks `required_fields`.
3. Runs the spec's verifiers (`claudomater.verifiers`: file-exists inside
   the tree or a declared artifact root, git branch exists, worktree clean,
   result-field equality, ...).
4. On failure, retries once from the branch's last committed state,
   salvaging uncommitted work as a `wip(phase-crash)` commit, then
   escalates.
5. Retained transcripts are scrubbed against `secrets_deny`.
6. A `paused` outcome (usage guardrail) is recoverable: the driver calls
   `guardrails.wait_for_unpark` and re-runs the phase
   ([runs-and-guardrails.md](runs-and-guardrails.md#park-and-resume)).

## Prompt injection seams

Two composable seams the DRIVER applies to a `PhaseSpec` before running it
(core never builds prompts on its own):

- **`inject_lessons(spec, store, scopes, domains)`** - the scopes' promoted
  (always-loaded) lessons plus a domain-seeded FTS retrieval, rendered as
  framed data with ids. The injected set is a run-log event before the
  agent exists; the result's `lessons_applied` is validated against exactly
  that set (an id never injected mints no credit; failed phases mint
  nothing). See [learning.md](learning.md).
- **`inject_conventions(spec, cfg)`** - the project's `conventions:` list
  from `.omater.yaml`, verbatim, under a fixed frame. Standing style/policy
  rules live in committed config, not in a GO prompt's restated
  standing-rules paragraph.

## Writing a driver

A driver composes the public seams; nothing in core hardcodes a pipeline
shape. The proven pattern:

```
omater start                          # fence + guard armed, run log open
for each phase:
    spec = PhaseSpec(name, model, prompt, required_fields, verifiers, ...)
    spec = inject_lessons(spec, ...)
    spec = inject_conventions(spec, cfg)
    outcome = runner.run_phase(spec)  # park-recover on paused
# merge phase (PR + review convergence) is driver/session-owned
omater gate completion --story-file ... --merge-sha ...   # before any done-flip
qaboard.finish_story(..., metrics_facts=..., metrics_path=...)
sprint.set_story_file_status(story_file, "done")
omater sprint import && omater sprint set ...             # import before first set
omater gate close-epic EPIC --sprint ...
omater teardown
```

Rules of the road, each a burn scar:

- Events before actions (write-ahead) - a crash must leave the log showing
  what was in flight.
- `omater sprint import` before the first `set` of any session - the
  write-through export writes ALL tracked statuses, and a stale DB reverts
  file-side edits.
- Gate and finish flow run BEFORE any done-flip.
- `omater sweep` in every pre-push gate.
- Phase agents never push, never open PRs, never touch
  `sprint-status.yaml` - those are driver-owned.
