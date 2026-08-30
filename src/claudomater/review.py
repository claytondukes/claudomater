"""Review-floor gate: compute the blocking findings from a structured
review result. The FLOOR decides, never the reviewing agent's own verdict
(design §4) — an `approve` from the reviewer is advisory. This is the seam
every consumer had to hand-roll before (Phase 0.5 rough edge #4), the same
missing-seam shape the escalation rule had before it became first-class.

Fail-closed by construction: findings that cannot be parsed, an unknown
severity, or an unknown floor raise GateError — a review whose findings are
unreadable must never pass a merge gate, and silently skipping a malformed
finding would let exactly the finding most likely to matter fall through.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Ascending severity. These are the same strings DEPLOYMENT_POLICY uses for
# review_floor, kept in one place so the two can never drift.
SEVERITIES = ("NOTE", "SHOULD-FIX", "MUST-FIX", "CRITICAL")
SEVERITY_RANK = {name: rank for rank, name in enumerate(SEVERITIES, start=1)}


class GateError(Exception):
    """Malformed findings or an unknown floor. Callers treat this as a
    FAILED review phase (fail closed), never as a pass."""


@dataclass
class GateResult:
    floor: str
    findings: list[dict[str, Any]] = field(default_factory=list)
    blocking: list[dict[str, Any]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.blocking

    def as_event_detail(self) -> dict[str, Any]:
        """The run-log shape (matches what the Phase 0.5 driver logged):
        counts only — the full findings already live in the phase result."""
        return {
            "floor": self.floor,
            "findings": len(self.findings),
            "blocking": len(self.blocking),
            "gate": "pass" if self.passed else "block",
        }

    def blocking_reasons(self) -> list[str]:
        """Failure-reason strings ready for retry feedback / run_escalated."""
        reasons = []
        for f in self.blocking:
            # line is optional — "a.py:None" is not actionable feedback
            location = f["file"]
            if f.get("line") is not None:
                location += f":{f['line']}"
            reasons.append(
                f"sr-review {f['severity']} at {location}: {f['finding']}"
                + (f" — {f['why']}" if f.get("why") else "")
            )
        return reasons


def review_gate(findings: Any, floor: str) -> GateResult:
    """Apply the deployment type's review-severity floor to a findings list.

    `findings` is the reviewer's structured list: each entry a dict with at
    least `severity` (one of SEVERITIES), `finding`, and `file`. A finding at
    or above the floor blocks. An empty list passes — whether an empty list
    is *believable* is the caller's judgment, not this gate's.
    """
    # Type checks BEFORE membership/truthiness: an unhashable severity or
    # floor (a list decoded from malformed agent output) must raise
    # GateError, never a TypeError the fail-closed caller isn't catching.
    if not isinstance(floor, str) or floor not in SEVERITY_RANK:
        raise GateError(f"unknown review floor {floor!r} (known: {SEVERITIES})")
    if not isinstance(findings, list):
        raise GateError(f"findings must be a list, got {type(findings).__name__}")
    for i, f in enumerate(findings):
        if not isinstance(f, dict):
            raise GateError(f"finding {i} is not an object: {f!r}")
        severity = f.get("severity")
        if not isinstance(severity, str) or severity not in SEVERITY_RANK:
            raise GateError(
                f"finding {i} has unknown severity {severity!r} "
                f"(known: {SEVERITIES})"
            )
        for key in ("finding", "file"):
            # .strip(): a whitespace-only value is as unreadable as a
            # missing one — it must fail closed, not gate on garbage
            if not isinstance(f.get(key), str) or not f[key].strip():
                raise GateError(
                    f"finding {i}: {key!r} must be a non-empty string: {f!r}"
                )
    floor_rank = SEVERITY_RANK[floor]
    blocking = [f for f in findings if SEVERITY_RANK[f["severity"]] >= floor_rank]
    return GateResult(floor=floor, findings=list(findings), blocking=blocking)
