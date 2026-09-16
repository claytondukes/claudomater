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
        instructed triggered response must VERIFY so the driver receives
        the brief - not die on the deliverable contract."""
        outcome = run_gated_phase(
            tmp_path,
            [dict(TRIGGERED)],
            verifiers=[result_field("status", "complete")],
        )
        assert outcome.status == "verified"
        assert outcome.result is not None
        assert outcome.result["design_gate_brief"] == TRIGGERED["design_gate_brief"]

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
