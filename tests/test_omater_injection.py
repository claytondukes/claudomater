"""Lesson injection + lessons_applied provenance (design §6, slice B).

Closes the Epic 9 "lessons-corpus-is-write-only" lesson: prior lessons feed
phase prompts as FRAMED DATA with ids, what was injected is on the run log
before the agent exists, the self-reported `lessons_applied` is validated
against exactly that set (an id never injected mints no credit), and the
counters that drive promotion candidacy move only for verified phases.
Promotion itself stays HUMAN-gated: the tool surfaces candidates and never
self-promotes.
"""

from __future__ import annotations

import itertools

import pytest

from claudomater.cli import EXIT_ERROR, EXIT_OK, main
from claudomater.learnstore import (
    INJECTION_FRAME,
    INJECTION_HEADER,
    PROMOTED_RULE_LINE_BUDGET,
    LearnStore,
    LearnStoreError,
    injection_block,
)
from claudomater.phases import ExecutionResult, PhaseRunner, PhaseSpec
from claudomater.runlog import RunLog


def ticking_now():
    counter = itertools.count()

    def now():
        t = next(counter)
        return f"2026-08-30T{t // 3600:02d}:{(t // 60) % 60:02d}:{t % 60:02d}.000000Z"

    return now


@pytest.fixture
def store(tmp_path):
    s = LearnStore.open(tmp_path / "learning.db", now=ticking_now())
    yield s
    s.close()


def lesson(s, topic, scope="global", domain="review", rule=None):
    return s.add(scope, domain, topic, rule or f"rule for {topic}", f"why {topic}")


class TestLessonsForPhase:
    def test_promoted_first_then_domain_matches_then_fts(self, store):
        promoted = lesson(store, "always-on")
        store.promote("global", "review", "always-on")
        by_domain = lesson(store, "domain-hit", domain="charts")
        by_text = lesson(store, "text-hit", domain="misc",
                         rule="clicking charts must select the bucket")
        lesson(store, "unrelated", domain="misc", rule="nothing relevant here")
        rows = store.lessons_for_phase(["global"], domains=["charts"])
        ids = [r["id"] for r in rows]
        assert ids[:2] == [promoted, by_domain]
        assert by_text in ids
        assert len(ids) == 3  # 'unrelated' matched nothing

    def test_budget_truncates_after_ranking(self, store):
        for i in range(6):
            lesson(store, f"t{i}", domain="charts")
        store.conn.execute("UPDATE lesson SET refs=9 WHERE topic='t5'")
        store.conn.commit()
        rows = store.lessons_for_phase(["global"], domains=["charts"], budget=3)
        assert len(rows) == 3
        assert rows[0]["topic"] == "t5"  # refs-ranked within the tier

    def test_superseded_and_out_of_scope_never_surface(self, store):
        lesson(store, "k", domain="charts")
        store.supersede("global", "charts", "k", "new judgment", "w")
        lesson(store, "other-scope", scope="elsewhere", domain="charts")
        rows = store.lessons_for_phase(["global"], domains=["charts"])
        assert [r["rule"] for r in rows] == ["new judgment"]

    def test_no_scopes_or_zero_budget_is_empty(self, store):
        lesson(store, "k")
        assert store.lessons_for_phase([], domains=["review"]) == []
        assert store.lessons_for_phase(["global"], budget=0) == []


class TestInjectionBlock:
    def test_framed_as_data_with_ids(self, store):
        lid = lesson(store, "k", rule="first line\nsecond line")
        block = injection_block(store.lessons_for_phase(["global"], ["review"]))
        assert INJECTION_HEADER in block and INJECTION_FRAME in block
        assert f"[L{lid}]" in block
        # every content line is blockquoted — same anti-injection discipline
        # as retry feedback: the corpus is past-run text, injection-shaped
        assert "> first line" in block and "  > second line" in block
        assert "lessons_applied" in INJECTION_FRAME

    def test_empty_retrieval_renders_nothing(self):
        assert injection_block([]) == ""


GOOD_APPLYING = (
    'done\n```json\n{"status": "complete", "lessons_applied": [%s]}\n```\n'
)


class TestProvenance:
    def _run(self, tmp_path, store, injected, applied_json):
        log = RunLog.create(tmp_path)

        class OneShot:
            def run(self, spec, model):
                return ExecutionResult(text=GOOD_APPLYING % applied_json)

        runner = PhaseRunner(tmp_path, log, OneShot(), learn_store=store)
        spec = PhaseSpec("dev", "m", "p", injected_lessons=injected)
        return runner.run_phase(spec), log

    def test_injected_event_precedes_the_spawn(self, tmp_path, store):
        lid = lesson(store, "k")
        outcome, log = self._run(tmp_path, store, (lid,), str(lid))
        events = [e["event"] for e in log.events()]
        assert events.index("lessons-injected") < events.index("phase-spawn")
        (inj,) = [e for e in log.events() if e["event"] == "lessons-injected"]
        assert inj["detail"]["ids"] == [lid]

    def test_applied_ids_are_validated_against_the_injected_set(
        self, tmp_path, store
    ):
        lid = lesson(store, "k")
        outcome, log = self._run(
            tmp_path, store, (lid,), f'{lid}, 9999, "L{lid}", {lid}'
        )
        assert outcome.status == "verified"
        (applied_ev,) = [e for e in log.events() if e["event"] == "lessons-applied"]
        # the never-injected id and the malformed string are rejected; the
        # duplicate collapses
        assert applied_ev["detail"]["applied"] == [lid]
        assert applied_ev["detail"]["rejected"] == [9999, f"L{lid}"]
        row = store.conn.execute("SELECT refs, sessions FROM lesson").fetchone()
        assert (row["refs"], row["sessions"]) == (1, 2)  # schema default sessions=1

    def test_nothing_injected_mints_nothing(self, tmp_path, store):
        lid = lesson(store, "k")
        outcome, log = self._run(tmp_path, store, (), str(lid))
        assert outcome.status == "verified"
        (applied_ev,) = [e for e in log.events() if e["event"] == "lessons-applied"]
        assert applied_ev["detail"]["applied"] == []
        assert applied_ev["detail"]["rejected"] == [lid]
        row = store.conn.execute("SELECT refs FROM lesson").fetchone()
        assert row["refs"] == 0

    def test_failed_phase_claims_mint_nothing(self, tmp_path, store):
        lid = lesson(store, "k")
        log = RunLog.create(tmp_path)

        class Failing:
            def run(self, spec, model):
                return ExecutionResult(
                    text=GOOD_APPLYING % lid, returncode=1
                )

        runner = PhaseRunner(tmp_path, log, Failing(), learn_store=store)
        outcome = runner.run_phase(
            PhaseSpec("dev", "m", "p", injected_lessons=(lid,), retries=0)
        )
        assert outcome.status == "escalated"
        assert not [e for e in log.events() if e["event"] == "lessons-applied"]
        assert store.conn.execute("SELECT refs FROM lesson").fetchone()["refs"] == 0

    def test_sessions_count_distinct_runs_not_uses(self, tmp_path, store):
        lid = lesson(store, "k")
        store.record_applied([lid], "run-1")
        store.record_applied([lid], "run-1")
        store.record_applied([lid], "run-2")
        row = store.conn.execute("SELECT refs, sessions FROM lesson").fetchone()
        # refs: every use; sessions: schema default 1 + first use in each run
        assert (row["refs"], row["sessions"]) == (3, 3)

    def test_store_failure_never_fails_a_verified_phase(self, tmp_path, store):
        lid = lesson(store, "k")
        store.close()  # closed store: record_applied will raise
        outcome, log = self._run(tmp_path, store, (lid,), str(lid))
        assert outcome.status == "verified"
        assert [e for e in log.events() if e["event"] == "lessons-applied-recording-failed"]


class TestHumanGatedPromotion:
    def test_candidates_threshold_is_3_uses_across_2_runs(self, store):
        a = lesson(store, "hot")
        b = lesson(store, "warm")
        store.record_applied([a], "r1")
        store.record_applied([a], "r2")
        store.record_applied([a], "r2")
        store.record_applied([b], "r1")  # 1 use, 1 run: not a candidate
        assert [c["topic"] for c in store.candidates()] == ["hot"]

    def test_promote_is_a_lifecycle_change_that_exports(self, store, tmp_path):
        store.export_dir = tmp_path / "lessons"
        lesson(store, "k")
        store.promote("global", "review", "k")
        (row,) = store.lessons(["global"])
        assert row["status"] == "promoted"
        exported = (tmp_path / "lessons" / "global.jsonl").read_text(encoding="utf-8")
        assert '"status":"promoted"' in exported
        with pytest.raises(LearnStoreError, match="already promoted"):
            store.promote("global", "review", "k")

    def test_promotion_budget_forces_a_demotion_review(self, store):
        big_rule = "\n".join("line" for _ in range(PROMOTED_RULE_LINE_BUDGET))
        lesson(store, "big", rule=big_rule)
        store.promote("global", "review", "big")
        lesson(store, "one-more")
        with pytest.raises(LearnStoreError, match="demotion review"):
            store.promote("global", "review", "one-more")

    def test_nothing_in_the_write_or_use_paths_promotes(self, store):
        """The no-self-promotion invariant: injection retrieval, use
        accounting, refine, and import never change status to promoted."""
        lid = lesson(store, "k")
        for _ in range(5):
            store.record_applied([lid], f"run-{_}")
        store.lessons_for_phase(["global"], ["review"])
        store.refine("global", "review", "k", rule="refined")
        (row,) = store.lessons(["global"])
        assert row["status"] == "active"
        assert store.candidates()  # a candidate, still not promoted

    def test_cli_candidates_and_promote(self, tmp_path, capsys):
        args = lambda *rest: [
            "learn", *rest,
            "--user-config", str(tmp_path / "missing.yaml"),
            "--db", str(tmp_path / "cli.db"),
            "--export-dir", str(tmp_path / "cli-lessons"),
        ]
        assert main(args("add", "--scope", "global", "--domain", "ci",
                         "--topic", "t", "--rule", "r", "--why", "w")) == EXIT_OK
        assert main(args("candidates")) == EXIT_OK
        assert "0 promotion candidate(s)" in capsys.readouterr().out
        assert main(args("promote", "--scope", "global", "--domain", "ci",
                         "--topic", "t")) == EXIT_OK
        assert "promoted: lesson" in capsys.readouterr().out
        assert main(args("promote", "--scope", "global", "--domain", "ci",
                         "--topic", "t")) == EXIT_ERROR


class TestRound1Hardening:
    """PR #12 round 1."""

    def test_every_content_line_is_a_true_blockquote(self, store):
        """Mid-line '> ' renders as plain text - lesson prose would read as
        normal instructions and defeat the framing. Metadata gets its own
        line; every rule/why line starts a real blockquote."""
        lesson(store, "k", rule="first line\nsecond line")
        block = injection_block(store.lessons_for_phase(["global"], ["review"]))
        content_lines = [
            l for l in block.splitlines()
            if l and not l.startswith(("#", "- [L"))
            and l not in INJECTION_FRAME.splitlines()
        ]
        body = [l for l in content_lines if l.strip() and "reference data" not in l]
        assert body, block
        assert all(l.startswith("  > ") for l in body), body
        # rule text never shares a line with metadata
        meta_lines = [l for l in block.splitlines() if l.startswith("- [L")]
        assert all("first line" not in l for l in meta_lines)

    def test_quote_bearing_domain_cannot_break_the_fts_query(self, store):
        lesson(store, "k", domain='we"ird')
        rows = store.lessons_for_phase(["global"], domains=['we"ird'])
        assert [r["topic"] for r in rows] == ["k"]

    def test_promoted_matches_cannot_crowd_active_out_of_the_fts_tier(self, store):
        """Tier 3 asks for active-only: promoted rows are already chosen,
        and letting them occupy the FTS LIMIT starved fresh matches."""
        for i in range(3):
            lesson(store, f"p{i}", rule="charts guidance everywhere")
            store.promote("global", "review", f"p{i}")
        active = lesson(store, "fresh", domain="misc",
                        rule="charts need a click handler")
        rows = store.lessons_for_phase(["global"], domains=["charts"], budget=4)
        assert active in [r["id"] for r in rows]

    def test_null_entries_land_in_rejected_and_reporting_is_flagged(
        self, tmp_path, store
    ):
        lid = lesson(store, "k")
        log = RunLog.create(tmp_path)

        class OneShot:
            def run(self, spec, model):
                return ExecutionResult(
                    text='x\n```json\n{"status": "ok", "lessons_applied": [null]}\n```'
                )

        PhaseRunner(tmp_path, log, OneShot(), learn_store=store).run_phase(
            PhaseSpec("dev", "m", "p", injected_lessons=(lid,))
        )
        (ev,) = [e for e in log.events() if e["event"] == "lessons-applied"]
        assert ev["detail"]["rejected"] == [None]
        assert ev["detail"]["reported"] is True
        # absent field with injections: an honest "no report", not malformed
        log2 = RunLog.create(tmp_path / "r2")

        class Silent:
            def run(self, spec, model):
                return ExecutionResult(text='x\n```json\n{"status": "ok"}\n```')

        PhaseRunner(tmp_path / "r2", log2, Silent(), learn_store=store).run_phase(
            PhaseSpec("dev", "m", "p", injected_lessons=(lid,))
        )
        (ev2,) = [e for e in log2.events() if e["event"] == "lessons-applied"]
        assert ev2["detail"] == {"applied": [], "rejected": [], "reported": False}


class TestRound2Hardening:
    """PR #12 round 2."""

    def test_unknown_search_statuses_fail_loudly(self, store):
        lesson(store, "k")
        for bad in (("superseded",), ("active", "bogus"), ()):
            with pytest.raises(LearnStoreError, match="search statuses"):
                store.search("rule", ["global"], statuses=bad)

    def test_record_applied_failure_rolls_back_the_whole_batch(self, store):
        """The caller swallows accounting failures, so a mid-batch raise
        must leave no partial state and no open transaction holding the
        write lock."""
        import sqlite3 as sq

        lid = lesson(store, "k")
        with pytest.raises(sq.IntegrityError):
            store.record_applied([lid, 999999], "run-x")  # second id: FK violation
        assert store.conn.in_transaction is False
        row = store.conn.execute("SELECT refs FROM lesson WHERE id=?", (lid,)).fetchone()
        assert row["refs"] == 0  # the first increment rolled back with the batch
        assert store.conn.execute("SELECT COUNT(*) FROM lesson_use").fetchone()[0] == 0
