"""Design gate: architecture-shaped asks escalate to a design session
instead of running as a single implementation pass."""

from __future__ import annotations

import json

import pytest

from claudomater.designgate import (
    DESIGN_GATE_RESULT_FIELDS,
    DESIGN_GATE_TRIGGERS,
    design_gate_block,
    gate_result_failure,
)
from claudomater.phases import (
    ExecutionResult,
    PhaseRunner,
    PhaseSpec,
    inject_design_gate,
)
from claudomater.runlog import RunLog
from claudomater.verifiers import result_field


class FakeExecutor:
    def __init__(self, outputs):
        self.outputs = list(outputs)

    def run(self, spec, model):
        return ExecutionResult(text=self.outputs.pop(0))


def run_gated_phase(tmp_path, results, *, verifiers=None, retries=0):
    """One gated create-shaped phase against queued JSON results."""
    outputs = [
        f"work\n```json\n{json.dumps(r)}\n```\n" if isinstance(r, dict) else r
        for r in results
    ]
    log = RunLog.create(tmp_path)
    runner = PhaseRunner(tmp_path, log, FakeExecutor(outputs), project="demo")
    spec = inject_design_gate(
        PhaseSpec(
            name="create",
            model="m",
            prompt="Draft the story.",
            required_fields=("story_file",),
            verifiers=list(verifiers or []),
            retries=retries,
        )
    )
    return runner.run_phase(spec)


class TestBlock:
    def test_block_carries_every_trigger_verbatim(self):
        """The prompt can never drift from the documented trigger set."""
        block = design_gate_block()
        for trigger in DESIGN_GATE_TRIGGERS:
            assert trigger in block

    def test_block_instructs_stop_over_implement(self):
        block = design_gate_block()
        assert "do NOT implement" in block
        assert "design_gate_brief" in block

    def test_block_asks_for_the_boolean_on_both_outcomes(self):
        """A gated agent always answers the gate - silence is not a pass."""
        block = design_gate_block()
        assert "design_gate_triggered: false" in block
        assert "design_gate_triggered: true" in block

    def test_result_vocabulary_is_shared(self):
        block = design_gate_block()
        for name in DESIGN_GATE_RESULT_FIELDS:
            assert name in block


class TestInjection:
    def test_prompt_gains_block_and_required_field(self):
        spec = PhaseSpec(
            name="create",
            model="m",
            prompt="Draft the story.",
            required_fields=("story_file",),
        )
        out = inject_design_gate(spec)
        assert out.prompt.startswith("Draft the story.")
        assert "## Design gate" in out.prompt
        assert out.required_fields == ("story_file", "design_gate_triggered")

    def test_required_field_is_not_duplicated(self):
        spec = PhaseSpec(
            name="dev",
            model="m",
            prompt="p",
            required_fields=("design_gate_triggered",),
        )
        out = inject_design_gate(spec)
        assert out.required_fields.count("design_gate_triggered") == 1

    def test_original_spec_is_unchanged(self):
        spec = PhaseSpec(name="create", model="m", prompt="p")
        inject_design_gate(spec)
        assert spec.prompt == "p"
        assert spec.required_fields == ()


TRIGGERED = {
    "design_gate_triggered": True,
    "design_gate_triggers": ["lifetime extension"],
    "design_gate_brief": "States, owners, transitions, teardown per state.",
}


class TestGateResultFailure:
    def test_false_boolean_is_valid(self):
        assert gate_result_failure({"design_gate_triggered": False}) is None

    def test_full_triggered_payload_is_valid(self):
        assert gate_result_failure(dict(TRIGGERED)) is None

    @pytest.mark.parametrize("bad", [None, 0, 1, "true", "false", [], {}])
    def test_non_boolean_is_refused(self, bad):
        failure = gate_result_failure({"design_gate_triggered": bad})
        assert failure is not None
        assert "JSON boolean" in failure

    @pytest.mark.parametrize("triggers", [None, [], "lifetime", {}])
    def test_triggered_without_trigger_list_is_refused(self, triggers):
        failure = gate_result_failure(
            {**TRIGGERED, "design_gate_triggers": triggers}
        )
        assert failure is not None
        assert "design_gate_triggers" in failure

    @pytest.mark.parametrize("brief", [None, "", "   ", 7])
    def test_triggered_without_brief_is_refused(self, brief):
        failure = gate_result_failure({**TRIGGERED, "design_gate_brief": brief})
        assert failure is not None
        assert "design_gate_brief" in failure


class TestRunPhaseGatePath:
    def test_triggered_gate_replaces_deliverables_and_skips_verifiers(self, tmp_path):
        """The exact review scenario: a create spec requiring story_file,
        plus a verifier only an implemented phase could satisfy. The
        instructed triggered response must reach the driver as a GATED
        outcome - not die on the deliverable contract."""
        outcome = run_gated_phase(
            tmp_path,
            [dict(TRIGGERED)],
            verifiers=[result_field("status", "complete")],
        )
        # DISTINCT status: gated, never verified - the gate payload is an
        # agent claim, so progression logic requiring "verified" cannot
        # advance on it (PR #27 review)
        assert outcome.status == "gated"
        assert outcome.result is not None
        assert outcome.result["design_gate_brief"] == TRIGGERED["design_gate_brief"]
        # The outcome invariant: every non-verified, non-skipped outcome
        # names why it stopped (PR #27 r3)
        assert outcome.failure_reasons == [
            "design-gate-triggered: escalate the design brief to a human"
        ]

    def test_untriggered_gate_with_deliverables_verifies_normally(self, tmp_path):
        # The happy false path: a complete non-gated result must reach the
        # normal verifiers and return verified (PR #27 r5)
        outcome = run_gated_phase(
            tmp_path,
            [{"design_gate_triggered": False, "story_file": "s.md"}],
            verifiers=[result_field("story_file", "s.md")],
        )
        assert outcome.status == "verified"
        assert outcome.result is not None
        assert outcome.result["story_file"] == "s.md"

    def test_gated_result_is_a_whitelist_of_the_gate_payload(self, tmp_path):
        # A triggered response must not smuggle arbitrary agent-authored
        # fields into the driver-visible outcome (PR #27 r5)
        outcome = run_gated_phase(
            tmp_path, [{**TRIGGERED, "exfil": "sk-ant-abcdef12345678"}]
        )
        assert outcome.status == "gated"
        assert outcome.result is not None
        assert set(outcome.result.keys()) == {
            "design_gate_triggered", "design_gate_triggers", "design_gate_brief",
        }

    def test_untriggered_gate_still_enforces_deliverables(self, tmp_path):
        outcome = run_gated_phase(
            tmp_path, [{"design_gate_triggered": False}] * 2, retries=1
        )
        assert outcome.status != "verified"
        assert any("story_file" in r for r in outcome.failure_reasons)

    def test_non_boolean_gate_answer_fails_the_phase(self, tmp_path):
        outcome = run_gated_phase(
            tmp_path, [{"design_gate_triggered": "true", "story_file": "s.md"}] * 2,
            retries=1,
        )
        assert outcome.status != "verified"
        assert any("design-gate result invalid" in r for r in outcome.failure_reasons)

    def test_triggered_gate_with_empty_brief_fails_the_phase(self, tmp_path):
        outcome = run_gated_phase(
            tmp_path, [{**TRIGGERED, "design_gate_brief": " "}] * 2, retries=1
        )
        assert outcome.status != "verified"
        assert any("design_gate_brief" in r for r in outcome.failure_reasons)

    def test_malformed_trigger_entries_fail_the_phase(self, tmp_path):
        # [None], [{}], [""] must not bypass the phase contract (PR #27 r2)
        outcome = run_gated_phase(
            tmp_path, [{**TRIGGERED, "design_gate_triggers": [""]}] * 2, retries=1
        )
        assert outcome.status not in ("gated", "verified")
        assert any("design_gate_triggers" in r for r in outcome.failure_reasons)

    def test_gated_outcome_clears_earlier_attempts_verdicts(self, tmp_path):
        # A retry chain: attempt 1 fails its verifier, attempt 2 returns a
        # valid triggered gate. The abandoned attempt(s) failed verdicts
        # must not ride along on the gated outcome (PR #27 r2).
        outcome = run_gated_phase(
            tmp_path,
            [
                {"design_gate_triggered": False, "story_file": "s.md"},
                dict(TRIGGERED),
            ],
            verifiers=[result_field("status", "complete")],
            retries=1,
        )
        assert outcome.status == "gated"
        assert outcome.verdicts == []
        # The abandoned attempt's failure reasons are replaced by the
        # stable gate entry, not carried as misleading diagnostics
        assert outcome.failure_reasons == [
            "design-gate-triggered: escalate the design brief to a human"
        ]

    def test_gate_event_scrubs_agent_controlled_trigger_strings(self, tmp_path):
        # A trigger echoing a credential shape must reach the retained run
        # log redacted - RunLog.event persists details without its own
        # scrub, so the gate branch owns it (PR #27 r3)
        leaked = {**TRIGGERED, "design_gate_triggers": ["found sk-ant-abcdef12345678 in config"]}
        outputs = ["work\n```json\n" + json.dumps(leaked) + "\n```\n"]
        log = RunLog.create(tmp_path)
        runner = PhaseRunner(tmp_path, log, FakeExecutor(outputs), project="demo")
        spec = inject_design_gate(
            PhaseSpec(name="create", model="m", prompt="p", required_fields=("story_file",))
        )
        outcome = runner.run_phase(spec)
        assert outcome.status == "gated"
        raw = (log.run_dir / "events.jsonl").read_text(encoding="utf-8")
        assert "design-gate-triggered" in raw
        assert "sk-ant-abcdef12345678" not in raw

    def test_mid_run_gate_salvages_dirty_worktree(self, tmp_path):
        # Trigger 4 fires MID-RUN: an agent that edited files before gating
        # must leave a clean worktree behind (exploratory edits committed as
        # salvage), so a re-drive does not collide with them (PR #27 r3)
        import subprocess

        def git(*args):
            subprocess.run(
                ["git", "-C", str(tmp_path), *args], check=True, capture_output=True
            )

        git("init", "-q")
        git("config", "user.email", "t@t")
        git("config", "user.name", "t")
        (tmp_path / ".gitignore").write_text(".omater/\n", encoding="utf-8")
        (tmp_path / "a.txt").write_text("a", encoding="utf-8")
        git("add", "-A")
        git("commit", "-q", "-m", "init")

        class DirtyThenGate:
            def run(self, spec, model):
                (tmp_path / "explore.txt").write_text("exploratory edit", encoding="utf-8")
                return ExecutionResult(
                    text="work\n```json\n" + json.dumps(TRIGGERED) + "\n```\n"
                )

        log = RunLog.create(tmp_path)
        runner = PhaseRunner(tmp_path, log, DirtyThenGate(), project="demo")
        outcome = runner.run_phase(
            inject_design_gate(PhaseSpec(name="dev", model="m", prompt="p"))
        )
        assert outcome.status == "gated"
        status = subprocess.run(
            ["git", "-C", str(tmp_path), "status", "--porcelain"],
            check=True, capture_output=True, text=True,
        ).stdout
        assert status.strip() == ""

    def test_gate_result_brief_is_scrubbed_before_reaching_the_driver(self, tmp_path):
        # The driver forwards the brief to a human - a credential shape in
        # it must be redacted like every other outbound agent output
        # (PR #27 r4)
        leaked = {
            **TRIGGERED,
            "design_gate_brief": "State machine. Note: found sk-ant-abcdef12345678 in config.",
        }
        outputs = ["work\n```json\n" + json.dumps(leaked) + "\n```\n"]
        log = RunLog.create(tmp_path)
        runner = PhaseRunner(tmp_path, log, FakeExecutor(outputs), project="demo")
        outcome = runner.run_phase(
            inject_design_gate(PhaseSpec(name="create", model="m", prompt="p"))
        )
        assert outcome.status == "gated"
        assert outcome.result is not None
        assert "sk-ant-abcdef12345678" not in outcome.result["design_gate_brief"]
        assert "State machine." in outcome.result["design_gate_brief"]

    def test_gated_outcome_mints_no_lesson_credit(self, tmp_path):
        # record_applied accounting is reserved for VERIFIED phases - a
        # gated escalation must not increment lesson usage even when the
        # agent claims lessons_applied (PR #27 r4)
        from claudomater.learnstore import LearnStore

        store = LearnStore.open(tmp_path / "learning.db")
        try:
            lid = store.add("global", "review", "k", "rule", "why")
            payload = {**TRIGGERED, "lessons_applied": [lid]}
            outputs = ["work\n```json\n" + json.dumps(payload) + "\n```\n"]
            log = RunLog.create(tmp_path)
            runner = PhaseRunner(
                tmp_path, log, FakeExecutor(outputs), project="demo", learn_store=store
            )
            outcome = runner.run_phase(
                inject_design_gate(
                    PhaseSpec(name="dev", model="m", prompt="p", injected_lessons=(lid,))
                )
            )
            assert outcome.status == "gated"
            assert not [e for e in log.events() if e["event"] == "lessons-applied"]
            assert store.conn.execute("SELECT refs FROM lesson").fetchone()["refs"] == 0
        finally:
            store.close()

    def test_gate_event_is_terminal_for_orphan_detection(self):
        # A PID-reporting executor gated attempt must not read as an orphan
        # for recovery to reap (PR #27 r2)
        from claudomater.phases import orphaned_agent_pids

        events = [
            {
                "event": "phase-agent-pid",
                "phase": "create",
                "story_key": "1-1",
                "detail": {"pid": 4242, "attempt": 1},
            },
            {
                "event": "design-gate-triggered",
                "phase": "create",
                "story_key": "1-1",
                "detail": {"attempt": 1, "triggers": ["lifetime extension"]},
            },
        ]
        assert orphaned_agent_pids(events) == []
