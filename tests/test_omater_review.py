"""Review-floor gate: the floor decides from findings, never the agent's
verdict; malformed findings fail CLOSED (Phase 0.5 rough edge #4)."""

from __future__ import annotations

import pytest

from claudomater.config import DEPLOYMENT_POLICY
from claudomater.review import (
    SEVERITIES,
    SEVERITY_RANK,
    GateError,
    review_gate,
)


def finding(severity, file="server/app.py", line=1, why="evidence"):
    return {
        "severity": severity,
        "file": file,
        "line": line,
        "finding": f"a {severity} problem",
        "why": why,
    }


class TestReviewGate:
    def test_floor_blocks_at_and_above_only(self):
        findings = [finding(s) for s in SEVERITIES]
        gate = review_gate(findings, "MUST-FIX")
        assert not gate.passed
        assert [f["severity"] for f in gate.blocking] == ["MUST-FIX", "CRITICAL"]
        # the exact Phase 0.5 shape: 4 NOTEs at a MUST-FIX floor -> pass
        assert review_gate([finding("NOTE")] * 4, "MUST-FIX").passed

    def test_note_floor_blocks_everything(self):
        """mission-critical: everything incl. NOTE blocks."""
        gate = review_gate([finding("NOTE")], "NOTE")
        assert not gate.passed

    def test_empty_findings_pass(self):
        gate = review_gate([], "NOTE")
        assert gate.passed
        assert gate.as_event_detail() == {
            "floor": "NOTE",
            "findings": 0,
            "blocking": 0,
            "gate": "pass",
        }

    def test_verdict_is_not_an_input(self):
        """The reviewer's own 'approve' must be unable to green the gate —
        review_gate takes findings and a floor, nothing else."""
        blocked = review_gate([finding("CRITICAL")], "SHOULD-FIX")
        assert not blocked.passed  # no verdict field could have said otherwise

    def test_malformed_findings_fail_closed(self):
        for bad in (
            "not a list",
            [{"severity": "HIGH", "file": "x", "finding": "y"}],  # unknown severity
            [{"severity": "NOTE", "file": "x"}],  # missing finding text
            [{"severity": "NOTE", "finding": "y"}],  # missing file
            ["not a dict"],
        ):
            with pytest.raises(GateError):
                review_gate(bad, "MUST-FIX")

    def test_unknown_floor_fails_closed(self):
        with pytest.raises(GateError, match="unknown review floor"):
            review_gate([], "BLOCKER")

    def test_unhashable_and_non_string_values_raise_gate_error_not_type_error(self):
        """Malformed agent output can decode to ANY JSON type. Every invalid
        shape must surface as GateError — a TypeError from dict membership
        would bypass the fail-closed catch at the call site."""
        with pytest.raises(GateError):
            review_gate([], ["MUST-FIX"])  # unhashable floor
        with pytest.raises(GateError):
            review_gate(
                [{"severity": [], "file": "x", "finding": "y"}], "MUST-FIX"
            )  # unhashable severity
        for bad_field in ({"file": 42}, {"finding": ["y"]}, {"file": True}):
            f = {"severity": "NOTE", "file": "x", "finding": "y", **bad_field}
            with pytest.raises(GateError, match="non-empty string"):
                review_gate([f], "MUST-FIX")

    def test_blocking_reasons_feed_retry_and_escalation(self):
        gate = review_gate(
            [finding("MUST-FIX", file="a.py", line=7)], "MUST-FIX"
        )
        (reason,) = gate.blocking_reasons()
        assert "MUST-FIX" in reason and "a.py:7" in reason and "evidence" in reason

    def test_blocking_reason_without_line_omits_the_suffix(self):
        """line is optional; 'a.py:None' is not actionable retry feedback."""
        f = finding("MUST-FIX", file="a.py")
        del f["line"]
        gate = review_gate([f], "MUST-FIX")
        (reason,) = gate.blocking_reasons()
        assert "None" not in reason
        assert "at a.py:" in reason  # the location, straight into the prose colon

    def test_every_policy_floor_is_a_valid_gate_floor(self):
        """DEPLOYMENT_POLICY.review_floor and the gate's severity vocabulary
        must never drift apart."""
        for dtype, policy in DEPLOYMENT_POLICY.items():
            assert policy["review_floor"] in SEVERITY_RANK, dtype
