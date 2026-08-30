"""Merge-phase core seams, extracted ahead of the Phase 1 orchestrator.

The Epic 9 verification run (2026-08-30) drove two PRs through a live,
session-driven merge phase and surfaced the seams that phase had to
hand-roll — the same missing-seam shape `review_gate` and the escalation
rule had before they became first-class. This module is those seams:

- `stale_numeric_claims` — a PR description's numeric test-count claims,
  re-verified against a freshly measured count on EVERY push. Encoded rule
  (2026-08-30 order): never let a description claim lag the diff — a
  claudomater PR carried "398 passed" for three pushes after the suite hit
  486, and a reviewer who trusts the description ships a stale claim.
- `RoundAlarm` — the 15-round review alarm as code, not policy. Past the
  budget the loop must STOP and page the operator with a surface diagnosis
  (which finding classes keep arriving, which convergence signals stay red)
  instead of continuing to grind. The Epic 9 order made this standing
  policy; a policy only the transcript remembers is not enforced.

Standing stance (decision, 2026-08-30): when forge policy demands an
approval the pipeline cannot produce — e.g. `required_approving_review_count`
wants a human, and a Copilot COMMENTED review does not satisfy it — an
admin-bypass merge is the ACCEPTED mechanism, recorded as a first-class run
event (`POLICY_BYPASS_EVENT`, shaped by `policy_bypass_detail`), never
buried in a merge event's detail or left implicit. A bypass without its
unmet rule and its authorization on the record is indistinguishable from a
rogue merge.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

DEFAULT_REVIEW_ROUND_ALARM = 15

# The canonical event name for a recorded merge-policy bypass. First-class
# on purpose: audits grep for this, not for prose inside a merge detail.
POLICY_BYPASS_EVENT = "merge-policy-bypass"


class MergeSeamError(Exception):
    """Invalid input to a merge seam (malformed count, bad config knob).
    Fail loudly at the call site — a merge gate running on garbage input
    must never look like a pass."""


class RoundAlarmExceeded(Exception):
    """The review loop hit its round budget. This is a STOP signal, not a
    retryable failure: the caller must halt the loop and page the operator
    with a surface diagnosis rather than keep grinding."""

    def __init__(self, rounds_completed: int, limit: int):
        self.rounds_completed = rounds_completed
        self.limit = limit
        super().__init__(
            f"review-round alarm: starting round {rounds_completed + 1} would "
            f"exceed the {limit}-round budget. STOP — page the operator with "
            "a surface diagnosis (which finding classes keep arriving, which "
            "convergence signals stay red) instead of continuing to grind."
        )


@dataclass
class RoundAlarm:
    """Round budget for one PR's review convergence loop. Call
    `begin_round()` before every round — including the first — and let
    `RoundAlarmExceeded` propagate to the loop's owner; catching it to
    continue would reduce the alarm back to the policy it replaces."""

    limit: int = DEFAULT_REVIEW_ROUND_ALARM
    rounds_completed: int = 0

    def begin_round(self) -> int:
        if self.rounds_completed >= self.limit:
            raise RoundAlarmExceeded(self.rounds_completed, self.limit)
        self.rounds_completed += 1
        return self.rounds_completed

    @classmethod
    def from_gates(cls, gates: Mapping[str, Any] | None) -> "RoundAlarm":
        """Build from a project config's `gates` mapping
        (`gates.review_round_alarm`, default 15). Distinct from
        `gates.copilot_max_rounds_kpi`: the KPI is a target the report
        scores, the alarm is a hard stop the loop enforces."""
        raw = (gates or {}).get("review_round_alarm", DEFAULT_REVIEW_ROUND_ALARM)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
            raise MergeSeamError(
                f"gates.review_round_alarm must be an int >= 1, got {raw!r}"
            )
        return cls(limit=raw)


# A test-count claim: "557 passed", "429 tests passed", "suite 557", and the
# arrow form "suite 381 -> 557" (the current claim is the arrow's right side).
# A count token is plain digits or PROPER three-digit grouping, neither
# preceded nor followed by more of a digit run: "1,2 passed" must not read
# as 12 (or as a claim of 2), "suite 1,2" must not read as a claim of 1,
# and "1, passed" / "3.14 passed" are not claims at all. Malformed
# claim-like text is deliberately NOT a claim — a lag detector that guessed
# at garbage would report noise instead of lag.
_COUNT = r"(?<![\d,.])(\d{1,3}(?:,\d{3})+|\d+)(?![.,]?\d)"
_PASSED_CLAIM = re.compile(_COUNT + r"\s+(?:tests?\s+)?passed\b", re.IGNORECASE)
_SUITE_CLAIM = re.compile(
    r"\bsuite\s+" + _COUNT + r"(?:\s*(?:->|→)\s*" + _COUNT + r")?", re.IGNORECASE
)
# An explicitly historical count is not a claim about NOW: "557 passed
# (was 381 passed)" cites its baseline the way the arrow grammar's left side
# does. History markers are matched immediately before the count; quoted
# citations of old claims are deliberately NOT exempted — a genuinely stale
# claim can sit in quotes too, so those stay with the caller's judgment.
_HISTORY_MARKER = re.compile(
    r"\b(?:was|were|previously|formerly|from)\s*[:(]?\s*$", re.IGNORECASE
)


def stale_numeric_claims(text: str, tests_passed: int) -> list[str]:
    """Every numeric test-count claim in `text` that no longer matches the
    freshly measured `tests_passed`. Call on EVERY push, with a count from a
    suite run at the current head — never a remembered one. Empty list =
    the description is current; each entry names the stale claim and the
    real count, ready to paste into the corrected description or a run
    event. The claim grammar is deliberately narrow (counts of passing
    tests); a number it does not recognize is simply not checked — this is
    a lag detector, not a parser of arbitrary prose."""
    if isinstance(tests_passed, bool) or not isinstance(tests_passed, int):
        raise MergeSeamError(
            f"tests_passed must be an int, got {type(tests_passed).__name__}"
        )
    if tests_passed < 0:
        raise MergeSeamError(f"tests_passed must be >= 0, got {tests_passed}")
    if not isinstance(text, str):
        raise MergeSeamError(f"text must be a string, got {type(text).__name__}")

    claims: list[tuple[int, int, str, int]] = []  # (start, end, quoted, claimed)
    for m in _PASSED_CLAIM.finditer(text):
        if _HISTORY_MARKER.search(text, 0, m.start()):
            continue  # "was 381 passed": a cited baseline, not a claim
        claims.append((m.start(), m.end(), m.group(0), int(m.group(1).replace(",", ""))))
    for m in _SUITE_CLAIM.finditer(text):
        current = m.group(2) or m.group(1)  # arrow form: the right side is current
        claims.append((m.start(), m.end(), m.group(0), int(current.replace(",", ""))))

    # "suite 381 -> 557 passed" matches both patterns over overlapping spans;
    # report each span once (first pattern wins) rather than twice.
    claims.sort()
    stale: list[str] = []
    covered_to = -1
    for start, end, quoted, claimed in claims:
        if start < covered_to:
            continue
        covered_to = end
        if claimed != tests_passed:
            stale.append(
                f'claims "{quoted}" but the suite at head reports '
                f"{tests_passed} passed"
            )
    return stale


def policy_bypass_detail(
    unmet_rule: str, mechanism: str, authorized_by: str
) -> dict[str, str]:
    """The canonical detail shape for a `POLICY_BYPASS_EVENT` run event.
    All three fields are mandatory and non-empty — see the standing stance
    in the module docstring."""
    fields = {
        "unmet_rule": unmet_rule,
        "mechanism": mechanism,
        "authorized_by": authorized_by,
    }
    for name, value in fields.items():
        if not isinstance(value, str) or not value.strip():
            raise MergeSeamError(
                f"policy bypass {name!r} must be a non-empty string: {value!r}"
            )
    return fields
