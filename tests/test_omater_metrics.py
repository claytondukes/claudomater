"""Run-metrics store (Clay, epic-47 close follow-up): one JSONL row per
finished story, idempotent by story_id, rendered by `omater report`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claudomater.metrics import (
    MetricsError,
    append_row,
    compose_row,
    epic_rows,
    load_rows,
    render_epic_table,
    render_trends,
)

FACTS = {
    "pr": 386,
    "merge_sha": "901fcace865b0a987e60dd38ca94d84b5c7aa522",
    "converge_rounds": 2,
    "threads_fixed": 2,
    "threads_dismissed": 0,
    "suppressed_fixed": 0,
    "suppressed_dismissed": 0,
    "wall_minutes": 82,
    "cost_usd": 42.72,
    "parks": 0,
    "merge_bypass": True,
}
FINISH_SURFACE = {"surface_touching": True, "step_key": "47-1-01", "section_id": 39}
FINISH_WAIVER = {"surface_touching": False}


class TestComposeRow:
    def test_a_surface_story_records_its_step_and_gate(self):
        row = compose_row("47-1", "47", FINISH_SURFACE, FACTS)
        assert row["outcome"] == {
            "kind": "step+gate", "step_key": "47-1-01", "section_id": 39,
        }
        assert row["pr"] == 386 and row["epic"] == "47"

    def test_a_waiver_story_records_the_waiver(self):
        row = compose_row("47-3", "47", FINISH_WAIVER, {**FACTS, "pr": 389})
        assert row["outcome"] == {"kind": "waiver"}

    def test_a_missing_fact_is_refused(self):
        facts = {k: v for k, v in FACTS.items() if k != "cost_usd"}
        with pytest.raises(MetricsError, match="missing driver fact.*cost_usd"):
            compose_row("47-1", "47", FINISH_SURFACE, facts)

    def test_an_unknown_fact_is_refused(self):
        with pytest.raises(MetricsError, match="unknown driver fact"):
            compose_row("47-1", "47", FINISH_SURFACE, {**FACTS, "vibes": 10})


class TestAppendIsIdempotent:
    def test_identical_retry_converges(self, tmp_path):
        p = tmp_path / "stories.jsonl"
        row = compose_row("47-1", "47", FINISH_SURFACE, FACTS)
        assert append_row(p, row) is True
        assert append_row(p, row) is False  # crash-retry: no second line
        assert len(load_rows(p)) == 1

    def test_a_conflicting_row_for_the_same_story_refuses(self, tmp_path):
        p = tmp_path / "stories.jsonl"
        append_row(p, compose_row("47-1", "47", FINISH_SURFACE, FACTS))
        other = compose_row("47-1", "47", FINISH_SURFACE, {**FACTS, "cost_usd": 1.0})
        with pytest.raises(MetricsError, match="DIFFERENT row"):
            append_row(p, other)

    def test_a_malformed_line_poisons_loudly(self, tmp_path):
        p = tmp_path / "stories.jsonl"
        p.write_text('{"story_id": "x"}\nnot json\n')
        with pytest.raises(MetricsError, match="not valid JSON"):
            load_rows(p)


def _epic47_rows():
    """The four epic-47 rows with the REPORT TABLE's exact cell values."""
    mk = compose_row
    return [
        mk("47-1", "47", {"surface_touching": True, "step_key": "47-1-01",
                          "section_id": 39}, FACTS),
        mk("47-2", "47", {"surface_touching": True, "step_key": "47-2-01",
                          "section_id": 39},
           {**FACTS, "pr": 388,
            "merge_sha": "9357b54ac6fd94678d81232d860bb29ea772e4d5",
            "converge_rounds": 1, "threads_fixed": 0, "wall_minutes": 107,
            "cost_usd": 46.69, "parks": 1}),
        mk("47-3", "47", {"surface_touching": False},
           {**FACTS, "pr": 389,
            "merge_sha": "fcbb5ec10a3ba7315d390c3d341e0a4172e365c3",
            "converge_rounds": 2, "threads_fixed": 0, "wall_minutes": 100,
            "cost_usd": 20.23, "parks": 1}),
        mk("47-4", "47", {"surface_touching": False},
           {**FACTS, "pr": 390,
            "merge_sha": "48e5c535d9edefbe83e4620d62f65f56c9cc3ada",
            "converge_rounds": 3, "threads_fixed": 2, "wall_minutes": 89,
            "cost_usd": 27.99, "parks": 0}),
    ]


class TestRendering:
    def test_the_epic_table_reproduces_every_cell_value(self, tmp_path):
        """The acceptance: every cell VALUE of the epic-47 report table."""
        p = tmp_path / "stories.jsonl"
        for row in _epic47_rows():
            append_row(p, row)
        table = render_epic_table(epic_rows(load_rows(p), "47"))
        for cell in [
            "47-1 | #386 | 901fcace | 2 | 2/0", "82 | 42.72 | 0 | step 47-1-01+gate | yes",
            "47-2 | #388 | 9357b54a | 1 | 0/0", "107 | 46.69 | 1 | step 47-2-01+gate | yes",
            "47-3 | #389 | fcbb5ec1 | 2 | 0/0", "100 | 20.23 | 1 | waiver | yes",
            "47-4 | #390 | 48e5c535 | 3 | 2/0", "89 | 27.99 | 0 | waiver | yes",
            "TOTAL: 4 stories, 378 wall_min, $137.63",
        ]:
            assert cell in table, f"missing cell: {cell}\n{table}"

    def test_an_unknown_epic_is_a_loud_error(self):
        with pytest.raises(MetricsError, match="no metrics rows for epic"):
            epic_rows(_epic47_rows(), "99")

    def test_trends_aggregate_per_epic(self):
        out = render_trends(_epic47_rows())
        assert "47 | 4 | 2.0 | $34.41 | 0/4" in out
