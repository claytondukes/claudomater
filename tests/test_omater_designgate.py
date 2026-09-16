"""Design gate: architecture-shaped asks escalate to a design session
instead of running as a single implementation pass."""

from __future__ import annotations

from claudomater.designgate import (
    DESIGN_GATE_RESULT_FIELDS,
    DESIGN_GATE_TRIGGERS,
    design_gate_block,
)
from claudomater.phases import PhaseSpec, inject_design_gate


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
