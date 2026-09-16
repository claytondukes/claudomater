# The design gate

Auto-detect asks that deserve a **design session with the human** before
any implementation run - instead of discovering, twenty reviewer rounds
later, that the "small feature" was an architecture change in costume.

## The failure mode this closes

A story asked for a capability of the shape "X keeps working when the user
navigates away". It read like a feature; it was an ownership redesign of a
shared resource (one connection, one global store, several views). The
normal single-run pipeline implemented it, opened a PR, and the review
loop then discovered the un-designed state machine one seam per round:
reentrancy, stale closures, unmount continuations, cross-consumer races.
The PR converged after roughly thirty review rounds, with most of the
final diff written DURING review. Every round cost tokens on both sides -
the implementing agent's and the reviewer's.

A one-page design session first would have surfaced the defect *families*
(what happens to each state on unmount / disconnect / disable) before any
code existed. That is the trade the gate buys: one short human
conversation instead of a long machine argument.

## The triggers

The canonical list lives in `claudomater.designgate.DESIGN_GATE_TRIGGERS`.
The prompt block renders it verbatim, so the prompt and the code cannot
drift; the numbered list below is a readable summary of the same four
triggers - the tuple is authoritative if they ever disagree:

1. **Lifetime extension** - the ask makes X survive the death of Y
   ("survives", "persists across", "resumes after", "keeps running
   when..."). X's lifetime is implicitly tied to Y somewhere, and untying
   it is an ownership redesign, not a feature.
2. **Shared mutable resource** - the change touches state with multiple
   independent consumers (a shared connection or socket, a global store
   slice several views watch, a cache several writers fill). Every
   consumer pair is a potential race the ask never names.
3. **Projected size** - the implementation is likely to exceed roughly
   400 changed source lines (tests excluded). Past that, a reviewer
   re-reads the whole diff every round and review cost compounds.
4. **Concept invention** (fires MID-RUN too) - the implementation needs a
   coordination concept the ask never named: ownership, claims, tokens,
   generations, epochs, leases, surrender/handover. Inventing one while
   implementing is the strongest signal the ask was mis-sized - the agent
   stops and escalates rather than keeps patching.

## What the gate produces

When a trigger fires, the agent does **not** implement. It returns a
structured result with:

- `design_gate_triggered: true`
- `design_gate_triggers`: which trigger(s) fired
- `design_gate_brief`: a one-page design brief - the shared resources
  involved; every state a run/resource can be in, with its **owner**; the
  legal transitions; and what happens in each state on teardown (unmount,
  disconnect, feature disable). Families, not instances.

The brief is the agenda for the human design session. After the human
signs off on the state machine, the story is re-scoped (often split) and
implementation proceeds against a design instead of toward one.

When no trigger fires, the agent sets `design_gate_triggered: false` and
proceeds - the gate always gets an explicit answer, so silence never
passes it.

## Wiring it into a driver

`phases.inject_design_gate` is a composable seam like lessons and
conventions:

```python
spec = PhaseSpec(name="create", model=..., prompt=..., required_fields=(...))
spec = inject_lessons(spec, store, scopes, domains)
spec = inject_conventions(spec, cfg)
spec = inject_design_gate(spec)   # create/preflight AND dev phases
outcome = runner.run_phase(spec)
if outcome.status == "gated":
    # do not run the next phase - escalate the brief to the human
    # (outcome.result is guaranteed non-None for a gated outcome)
    notify_human(outcome.result["design_gate_brief"])
elif outcome.status != "verified":
    ...  # escalated/paused handling, as in docs/phases.md
```

The seam appends `design_gate_triggered` to the spec's `required_fields`,
so a gated phase cannot end without answering the gate. `PhaseRunner`
validates the gate's slice of the result fail-closed
(`designgate.gate_result_failure`): the boolean must be a real JSON
boolean, and a triggered gate must carry a non-empty list of non-empty
trigger strings and a non-empty brief. A validated TRIGGERED result then
**replaces** the phase's normal contract - the runner skips the remaining
`required_fields` and the deliverable verifiers (the agent implemented
nothing, by design), logs a `design-gate-triggered` event (trigger strings
scrubbed like any other retained agent output), and returns the result
under the DISTINCT outcome status **`gated`** - never `verified`, so
progression logic that requires a verified phase cannot advance on what is
ultimately an agent claim.

Inject it into **create/preflight** phases (catch the ask before any code)
and into **dev** phases (trigger 4 fires mid-implementation - the moment
the agent starts inventing coordination concepts, stopping is cheaper than
another review round).

## Limits - detection, not authority

The gate's `false` answer is an agent claim, like every structured-result
field: prompt text cannot force a confused agent to stop, and no verifier
can mechanically prove "this ask needed a design session". What the runner
enforces is that the question is always ANSWERED and that a triggered
answer carries a complete brief. Phase progression stays driver- and
human-owned: the driver decides what a triggered gate halts, and the
backstops for a false negative are the review-round alarm
(`gates.review_round_alarm`) and the human-side verbs below.

## The human-side verbs

Two phrases worth adopting with your assistant, whatever pipeline you run:

- **"design gate"** - force the gate on any ask: produce the state-machine
  brief and stop for the human's call.
- **"patch-scope only, no new concepts"** - a hard boundary: the moment
  the implementation disagrees with that sizing, the agent must stop and
  say so instead of quietly growing the change.
