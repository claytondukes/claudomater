# Copilot code-review instructions for claudomater

claudomater orchestrates agent-driven story pipelines: a phase runner that
spawns headless `claude` agents, verifier gates, a write-ahead run log,
usage guardrails, and a PreToolUse bash write-fence. Every review finding
here is triaged against a permanent disposition ledger, so aim findings at
correctness — the sections below say what this repo enforces, which designs
are deliberate, and where the seams are.

## Enforce (real defect classes — report freely)

- **Fail loudly.** No silent fallbacks, no recover-and-continue, no
  swallowed exceptions. Unknown or malformed input fails CLOSED (raises,
  pauses, or refuses) — it never passes a gate by default.
- **No TODOs, stubs, or placeholder implementations** in shipped code.
- **A behavior fix carries a regression test that fails on the pre-fix
  code.** A fix without its test is incomplete.
- **`hooks.py` runs synchronously inside every Bash tool call: scans must
  stay linear** in input size. Former quadratics are pinned by timing
  regression tests; flag any new nested scan over the same text.
- **PR-description numeric claims (test counts) must be current as of the
  latest push** — `merge.stale_numeric_claims` encodes this rule.
- **No attribution or generation footers** in commit messages or PR bodies.
- **Python 3.11–3.14**: no syntax or stdlib usage outside that range.

## Deliberate design — do not suggest changing these

- **The write fence is an accident seatbelt, not a security boundary.** It
  protects against a well-meaning agent's mistakes, not an evading
  adversary. Its bash recognizer is **frozen by decision**: any construct it
  has not fully resolved (substitutions, globs, escapes, surviving tildes,
  parameter expansion) classifies as UNRECOGNIZED and **fails OPEN by
  contract**. Suggestions to extend bash-semantics coverage or to close
  fail-open paths are out of policy — false DENIES are the priority defect
  class here, not misses (see the `lastpipe`/`cdable_vars` ledger
  dismissals).
- **Verifier-decides.** Run state derives from git and the run-log events,
  never from an agent's claims or a status field. Do not suggest trusting
  agent-reported results, introducing status fields as sources of truth, or
  skipping re-verification against reality.

## Context — the seams, so findings can anchor precisely

- **`runlog.RunLog`**: append-only JSONL, write-ahead (intent is logged
  before the action runs). Liveness = last event is not terminal.
  `attach()` appends from a sibling process with no bookkeeping event;
  `adopt()` is orchestrator takeover and records its verb. Parked
  (`run-parked`) is a lifecycle fact cleared only by `phase-spawn` or
  `control-resume` — bookkeeping and sibling appends do not unpark — and
  `run-failed` is refused while parked. Appends serialize under an flock;
  every check-then-append rule lives inside that critical section.
- **`review.review_gate(findings, floor)`** is the single gating seam: the
  floor decides from findings, the reviewing agent's verdict is advisory,
  and malformed findings raise `GateError` (fail closed).
- **Guardrails**: unknown usage fails closed (pause). A *stale* reading
  with matching account provenance pauses only when it projects near a
  pause threshold (0.5 pp/min drift — self-capping); degrades never act on
  stale data. A guardrail pause **parks** the run — live and adoptable —
  it never fails it.
