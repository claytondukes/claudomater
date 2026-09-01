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

    def test_a_surface_outcome_without_its_step_is_refused(self):
        """Copilot: surface_touching with no step_key/section_id would
        write a step+gate row full of None that reads as complete."""
        with pytest.raises(MetricsError, match="step_key and section_id"):
            compose_row("47-1", "47", {"surface_touching": True}, FACTS)
        with pytest.raises(MetricsError, match="step_key and section_id"):
            compose_row(
                "47-1", "47",
                {"surface_touching": True, "step_key": "  ", "section_id": 39},
                FACTS,
            )


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
        good = json.dumps(compose_row("47-1", "47", FINISH_SURFACE, FACTS))
        p.write_text(f"{good}\nnot json\n")
        with pytest.raises(MetricsError, match="not valid JSON"):
            load_rows(p)

    def test_a_schema_broken_row_is_refused_at_load(self, tmp_path):
        """Copilot: a JSON-valid row missing a required field slipped
        through load and crashed the renderers with a raw KeyError - the
        CLI printed a traceback instead of its error: contract."""
        p = tmp_path / "stories.jsonl"
        row = compose_row("47-1", "47", FINISH_SURFACE, FACTS)
        del row["cost_usd"]
        p.write_text(json.dumps(row) + "\n")
        with pytest.raises(MetricsError, match="missing field.*cost_usd"):
            load_rows(p)

    def test_an_unknown_field_is_refused_at_load(self, tmp_path):
        """Only compose_row writes this file - an unexpected field means
        something else did."""
        p = tmp_path / "stories.jsonl"
        row = compose_row("47-1", "47", FINISH_SURFACE, FACTS)
        row["vibes"] = 10
        p.write_text(json.dumps(row) + "\n")
        with pytest.raises(MetricsError, match="unknown field"):
            load_rows(p)

    def test_a_malformed_outcome_is_refused_at_load(self, tmp_path):
        p = tmp_path / "stories.jsonl"
        row = compose_row("47-1", "47", FINISH_SURFACE, FACTS)
        row["outcome"] = {"kind": "vibes"}
        p.write_text(json.dumps(row) + "\n")
        with pytest.raises(MetricsError, match="malformed outcome"):
            load_rows(p)

    def test_a_step_gate_outcome_without_its_fields_is_refused_at_load(
        self, tmp_path
    ):
        """Copilot round 2: {'kind': 'step+gate'} alone passed validation
        and rendered as 'step None+gate' - silent data corruption."""
        p = tmp_path / "stories.jsonl"
        row = compose_row("47-1", "47", FINISH_SURFACE, FACTS)
        row["outcome"] = {"kind": "step+gate"}
        p.write_text(json.dumps(row) + "\n")
        with pytest.raises(MetricsError, match="malformed outcome"):
            load_rows(p)

    def test_an_outcome_with_unknown_keys_is_refused_at_load(self, tmp_path):
        p = tmp_path / "stories.jsonl"
        row = compose_row("47-1", "47", FINISH_SURFACE, FACTS)
        row["outcome"] = {"kind": "waiver", "extra": 1}
        p.write_text(json.dumps(row) + "\n")
        with pytest.raises(MetricsError, match="malformed outcome"):
            load_rows(p)

    @pytest.mark.parametrize(
        "field,bad",
        [
            ("cost_usd", "20.23"),
            ("cost_usd", float("nan")),
            ("cost_usd", float("inf")),
            ("wall_minutes", "82"),
            ("merge_bypass", "yes"),
            ("pr", 386.0),
            ("merge_sha", ""),
        ],
    )
    def test_a_mistyped_field_is_refused_at_load(self, tmp_path, field, bad):
        """Copilot round 3: '20.23' as a string passed load and crashed
        the renderer with a TypeError - a traceback, not the CLI's
        error: contract."""
        p = tmp_path / "stories.jsonl"
        row = compose_row("47-1", "47", FINISH_SURFACE, FACTS)
        row[field] = bad
        p.write_text(json.dumps(row) + "\n")
        with pytest.raises(MetricsError, match=field):
            load_rows(p)

    def test_a_mistyped_fact_is_refused_at_compose(self):
        """The same validator runs at WRITE time - a mistyped row must
        never land in the store and poison every later read."""
        with pytest.raises(MetricsError, match="cost_usd"):
            compose_row(
                "47-1", "47", FINISH_SURFACE, {**FACTS, "cost_usd": "42.72"}
            )

    def test_a_mistyped_section_id_is_refused_at_load(self, tmp_path):
        """Copilot round 4: section_id comes from the board as an int -
        a string or bool would silently corrupt the persisted schema."""
        p = tmp_path / "stories.jsonl"
        row = compose_row("47-1", "47", FINISH_SURFACE, FACTS)
        row["outcome"] = {"kind": "step+gate", "step_key": "47-1-01",
                          "section_id": "39"}
        p.write_text(json.dumps(row) + "\n")
        with pytest.raises(MetricsError, match="malformed outcome"):
            load_rows(p)

    def test_append_validates_its_row_argument(self, tmp_path):
        """Copilot round 4: append_row must not trust its caller -
        backfill tooling handing it a hand-built dict would poison the
        store and only surface at the next load."""
        with pytest.raises(MetricsError, match="missing field"):
            append_row(tmp_path / "stories.jsonl", {"story_id": "47-1"})

    def test_a_write_io_failure_is_a_typed_error(self, tmp_path):
        """Copilot round 4: an OSError on the write path (here: a
        read-only store directory) must surface as MetricsError, matching
        load_rows - callers catch MetricsError, not raw OSError."""
        p = tmp_path / "run-metrics" / "stories.jsonl"
        p.parent.mkdir()
        p.parent.chmod(0o555)
        row = compose_row("47-1", "47", FINISH_SURFACE, FACTS)
        try:
            with pytest.raises(MetricsError, match="cannot write"):
                append_row(p, row)
        finally:
            p.parent.chmod(0o755)

    def test_append_serializes_under_the_store_lock(self, tmp_path):
        """Copilot round 2: check-then-append needs the inter-process
        lock (the RunLog._append_lock discipline) - a backfill racing a
        finish flow could interleave or double-write."""
        import fcntl
        import threading

        p = tmp_path / "stories.jsonl"
        row = compose_row("47-1", "47", FINISH_SURFACE, FACTS)
        lock = tmp_path / "stories.jsonl.lock"
        holder = open(lock, "w")
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        done: list[bool] = []
        t = threading.Thread(target=lambda: done.append(append_row(p, row)))
        t.start()
        t.join(0.3)
        try:
            assert t.is_alive(), "append_row must block while the store lock is held"
        finally:
            fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
            holder.close()
        t.join(5)
        assert not t.is_alive() and done == [True]
        assert load_rows(p)[0]["story_id"] == "47-1"


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

    def test_the_epic_table_orders_stories_numerically(self):
        """Copilot round 2: lexicographic sort puts 47-10 before 47-2."""
        rows = [
            compose_row("47-10", "47", FINISH_WAIVER, {**FACTS, "pr": 400}),
            compose_row("47-2", "47", FINISH_WAIVER, {**FACTS, "pr": 388}),
        ]
        table = render_epic_table(rows)
        assert table.index("47-2 |") < table.index("47-10 |")

    def test_a_malformed_story_id_renders_a_typed_error(self):
        rows = [compose_row("47-x", "47", FINISH_WAIVER, FACTS)]
        with pytest.raises(MetricsError, match="malformed story_id"):
            render_epic_table(rows)

    def test_a_malformed_epic_id_in_trends_is_a_typed_error(self):
        row = compose_row("47-1", "oops", FINISH_WAIVER, FACTS)
        with pytest.raises(MetricsError, match="malformed epic"):
            render_trends([row])

    def test_trends_aggregate_per_epic(self):
        out = render_trends(_epic47_rows())
        assert "47 | 4 | 2.0 | $34.41 | 0/4" in out
