"""Merge-phase seams: description-claim re-verification on every push, the
review-round alarm as code, and the recorded-policy-bypass shape (all from
the Epic 9 verification run, 2026-08-30)."""

from __future__ import annotations

import pytest

from claudomater.merge import (
    DEFAULT_REVIEW_ROUND_ALARM,
    POLICY_BYPASS_EVENT,
    MergeSeamError,
    RoundAlarm,
    RoundAlarmExceeded,
    policy_bypass_detail,
    stale_numeric_claims,
)


class TestStaleNumericClaims:
    def test_the_pr5_incident_shape_is_caught(self):
        """The literal incident: the PR description said '398 passed' while
        the suite at head reported 486 — pushed three times without anyone
        re-verifying the claim."""
        body = "All five fixes shipped.\n\nFull suite: 398 passed."
        (msg,) = stale_numeric_claims(body, 486)
        assert '"398 passed"' in msg and "486" in msg

    def test_current_claims_are_clean(self):
        body = "Full suite: 557 passed (was 381 — suite 381 -> 557)."
        assert stale_numeric_claims(body, 557) == []

    def test_arrow_form_checks_the_current_side_only(self):
        """'suite 381 -> 557' claims 557 NOW; 381 is history, not a claim."""
        assert stale_numeric_claims("suite 381 -> 557", 557) == []
        (msg,) = stale_numeric_claims("suite 381 -> 557", 560)
        assert "560" in msg

    def test_every_distinct_stale_claim_is_reported(self):
        body = "Intro says 100 passed. The table repeats: suite 100."
        assert len(stale_numeric_claims(body, 200)) == 2

    def test_overlapping_patterns_report_a_span_once(self):
        """'suite 381 -> 557 passed' matches both the suite and the passed
        grammar over overlapping text — one claim, one report."""
        assert len(stale_numeric_claims("suite 381 -> 557 passed", 999)) == 1

    def test_comma_grouped_counts_parse(self):
        assert stale_numeric_claims("1,234 tests passed", 1234) == []
        (msg,) = stale_numeric_claims("1,234 tests passed", 1233)
        assert "1233" in msg

    def test_prose_without_a_count_is_not_a_claim(self):
        """A lag detector, not a prose parser: 'all tests passed' and bare
        numbers carry no count claim to verify."""
        assert stale_numeric_claims("all tests passed; CI green; 42 files", 7) == []

    def test_explicitly_historical_counts_are_not_claims(self):
        """Round-6 finding: '557 passed (was 381 passed)' cited its baseline
        the way the arrow grammar's left side does, but the generic matcher
        flagged it. History markers (was/were/previously/formerly/from)
        immediately before a count exempt it; quoted citations of old claims
        deliberately do NOT — a genuinely stale claim can sit in quotes."""
        text = "Full suite: 557 passed (was 381 passed)."
        assert stale_numeric_claims(text, 557) == []
        (msg,) = stale_numeric_claims(text, 560)
        assert '"557 passed"' in msg and "381" not in msg
        assert stale_numeric_claims(
            "previously 100 tests passed; now suite 200", 200
        ) == []

    def test_history_markers_survive_markdown_emphasis(self):
        """Round-11 finding: the count grammar deliberately supports bolded
        counts, so the history exemption must too — '(was **381 passed**)'
        is still a cited baseline. Quotes stay actionable (not emphasis)."""
        assert stale_numeric_claims(
            "Full suite: 557 passed (was **381 passed**).", 557
        ) == []
        assert stale_numeric_claims("previously **suite 100**; suite 200", 200) == []
        # a QUOTED old claim is still checked — quotes are not emphasis
        (msg,) = stale_numeric_claims('the PR said "398 passed" back then', 486)
        assert "398" in msg

    def test_history_markers_cover_the_suite_grammar_too(self):
        """Round-10 finding: the exemption only guarded the `passed`
        grammar — 'previously suite 100' was still reported as stale,
        against the function's own contract."""
        assert stale_numeric_claims("previously suite 100; now suite 200", 200) == []
        assert stale_numeric_claims("was suite 381, suite 381 -> 624", 624) == []
        (msg,) = stale_numeric_claims("previously suite 100; now suite 200", 300)
        assert '"suite 200"' in msg and "100" not in msg

    def test_malformed_comma_forms_are_not_claims(self):
        """Round-1 finding: '1,2 passed' read as a claim of 12, and partial
        matches inside malformed digit runs could claim their tail. A count
        token is plain digits or proper three-digit grouping, never a
        fragment of something else — malformed claim-like text is NOT a
        claim (documented policy), so no tests_passed value flags it."""
        for text in ("1,2 passed", "1, passed", "12,34 tests passed",
                     "1,2345 passed", "3.14 passed", "suite 1,2"):
            assert stale_numeric_claims(text, 999) == [], text
        # proper grouping still parses on both patterns
        assert stale_numeric_claims("1,234,567 passed", 1234567) == []
        (msg,) = stale_numeric_claims("suite 1,234", 5)
        assert "5" in msg

    def test_signed_or_alphanumeric_attached_counts_are_not_claims(self):
        """Round-9 finding: '-1 passed' read as a claim of 1 and '1e3
        passed' as a claim of 3. Count tokens must not be glued to signs or
        word characters — while markdown emphasis ('**620 passed**') keeps
        working, since PR bodies bold their counts."""
        for text in ("-1 passed", "+5 passed", "1e3 passed",
                     "x1 tests passed", "suite -1"):
            assert stale_numeric_claims(text, 999) == [], text
        assert stale_numeric_claims("**620 passed**", 620) == []
        (msg,) = stale_numeric_claims("**620 passed**", 621)
        assert "621" in msg

    def test_garbage_counts_fail_loudly(self):
        for bad in (True, -1, "486", None, 4.86):
            with pytest.raises(MergeSeamError):
                stale_numeric_claims("486 passed", bad)
        with pytest.raises(MergeSeamError):
            stale_numeric_claims(None, 486)


class TestRoundAlarm:
    def test_alarm_stops_the_sixteenth_round(self):
        """The 2026-08-30 order as code: past 15 rounds, STOP — the 16th
        round must never start."""
        alarm = RoundAlarm()
        for expected in range(1, 16):
            assert alarm.begin_round() == expected
        with pytest.raises(RoundAlarmExceeded) as exc:
            alarm.begin_round()
        assert exc.value.rounds_completed == 15 and exc.value.limit == 15
        assert "STOP" in str(exc.value) and "surface diagnosis" in str(exc.value)

    def test_default_budget_is_fifteen(self):
        assert DEFAULT_REVIEW_ROUND_ALARM == 15
        assert RoundAlarm().limit == 15

    def test_from_gates_reads_the_config_knob(self):
        assert RoundAlarm.from_gates({"review_round_alarm": 3}).limit == 3
        assert RoundAlarm.from_gates({}).limit == 15
        assert RoundAlarm.from_gates(None).limit == 15

    def test_from_gates_fails_loudly_on_garbage(self):
        """An alarm silently disarmed by a typo'd knob is the policy-only
        state this class exists to replace."""
        for bad in (0, -5, True, "15", 15.0):
            with pytest.raises(MergeSeamError):
                RoundAlarm.from_gates({"review_round_alarm": bad})


class TestPolicyBypassDetail:
    def test_canonical_shape(self):
        detail = policy_bypass_detail(
            unmet_rule="required_approving_review_count: 1",
            mechanism="gh pr merge --admin",
            authorized_by="Clay order 2026-08-30",
        )
        assert detail == {
            "unmet_rule": "required_approving_review_count: 1",
            "mechanism": "gh pr merge --admin",
            "authorized_by": "Clay order 2026-08-30",
        }
        # the event name is a stable, grep-able constant — audits key on it
        assert POLICY_BYPASS_EVENT == "merge-policy-bypass"

    def test_a_bypass_without_its_record_is_refused(self):
        """A bypass missing its unmet rule or authorization is
        indistinguishable from a rogue merge — the shape refuses it."""
        for hole in ("unmet_rule", "mechanism", "authorized_by"):
            kwargs = {
                "unmet_rule": "r",
                "mechanism": "m",
                "authorized_by": "a",
                hole: "  ",
            }
            with pytest.raises(MergeSeamError, match=hole):
                policy_bypass_detail(**kwargs)
