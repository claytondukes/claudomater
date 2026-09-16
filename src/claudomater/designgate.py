"""Design gate: auto-detect asks that deserve a design session BEFORE any
implementation run.

Some requests read like features but are architecture changes in costume.
The measured failure mode: a story whose real scope was an ownership
redesign of a shared resource went through the normal single-run pipeline,
and the review loop then discovered the un-designed state machine one seam
per round - roughly thirty reviewer rounds, most of the final diff written
DURING review, cost paid on both sides (the agent's tokens and the
reviewer's). A one-page design session with the human first would have
found the defect FAMILIES before any code existed.

The gate is a prompt block (`design_gate_block()`) a driver composes into
its create/preflight AND dev phases - `phases.inject_design_gate` is the
seam, mirroring lessons and conventions. The block instructs the agent to
evaluate the ask against the trigger list below and, when any trigger
fires, STOP and return a design brief instead of proceeding; the driver
reads the structured result fields and routes the brief to the human.
"""

from __future__ import annotations

# The trigger list is data on purpose: humans and drivers can read exactly
# what fires the gate, and the block renders from it verbatim so the prompt
# can never drift from the documented set.
DESIGN_GATE_TRIGGERS: tuple[str, ...] = (
    'Lifetime extension: the ask makes X survive the death of Y ("survives",'
    ' "persists across", "resumes after", "keeps running when...").'
    " X's lifetime is implicitly tied to Y somewhere in the existing code,"
    " and untying it is an ownership redesign, not a feature.",
    "Shared mutable resource: the change touches state with multiple"
    " independent consumers (a shared connection or socket, a global store"
    " slice several views watch, a cache several writers fill). Every"
    " consumer pair is a potential race the ask never names.",
    "Projected size: the implementation is likely to exceed roughly 400"
    " changed source lines (tests excluded). Past that, a reviewer re-reads"
    " the whole diff every round and review cost compounds per round.",
    "Concept invention (fires MID-RUN too): the implementation needs a"
    " coordination concept the ask never named - ownership, claims, tokens,"
    " generations, epochs, leases, surrender/handover. Inventing one while"
    " implementing is the strongest signal the ask was mis-sized: stop and"
    " escalate rather than keep patching.",
)

# The structured-result vocabulary the block asks for. Only the boolean is
# wired into required_fields by the seam (the other two are meaningful only
# when the gate fires); the tuple exists so drivers and docs share one
# spelling.
DESIGN_GATE_RESULT_FIELDS: tuple[str, ...] = (
    "design_gate_triggered",
    "design_gate_triggers",
    "design_gate_brief",
)


def gate_result_failure(result: dict) -> str | None:
    """Validate the gate's slice of a structured result, fail-closed.

    `design_gate_triggered` must be a real JSON boolean - `null`, `0`, or a
    string is a refusal to answer, not an answer - and a TRIGGERED gate must
    carry its full payload: the fired trigger list and a non-empty brief.
    An escalation without the one-page agenda is exactly the empty ritual
    the gate exists to prevent (PR #27 review)."""
    value = result.get("design_gate_triggered")
    if not isinstance(value, bool):
        return f"design_gate_triggered must be a JSON boolean, got {value!r}"
    if value:
        triggers = result.get("design_gate_triggers")
        if (
            not isinstance(triggers, list)
            or not triggers
            or not all(isinstance(t, str) and t.strip() for t in triggers)
        ):
            return (
                "a triggered gate must name the fired trigger(s) in"
                " design_gate_triggers (non-empty list of non-empty strings)"
            )
        brief = result.get("design_gate_brief")
        if not isinstance(brief, str) or not brief.strip():
            return (
                "a triggered gate must carry a non-empty design_gate_brief"
                " (the design-session agenda)"
            )
    return None


def design_gate_block() -> str:
    """The framed prompt section. Triggers render verbatim from
    DESIGN_GATE_TRIGGERS, so what the agent receives is exactly what the
    documentation shows."""
    lines = "\n".join(f"- {t}" for t in DESIGN_GATE_TRIGGERS)
    return (
        "## Design gate (evaluate BEFORE implementing; re-evaluate WHILE"
        " implementing)\n"
        "Some asks read like features but are architecture changes in"
        " costume; implementing one in a single run leads to a long"
        " review-loop battle that costs tokens on both sides. Evaluate the"
        " ask against these triggers:\n"
        f"{lines}\n\n"
        "If NO trigger fires: proceed normally and set"
        " `design_gate_triggered: false` in your structured result.\n"
        "If ANY trigger fires: do NOT implement. Set"
        " `design_gate_triggered: true`, name the fired trigger(s) in"
        " `design_gate_triggers`, and put a design brief in"
        " `design_gate_brief`: the shared resources involved; every state a"
        " run or resource can be in, with its owner; the legal transitions;"
        " and what happens in each state on teardown (unmount, disconnect,"
        " feature disable). One page, families not instances - it is the"
        " agenda for a human design session, not an implementation plan."
        " End your run there; the driver escalates the brief to the human."
    )
