"""Slice C: sprint tracking in the DB + byte-exact write-through export.

The acceptance question this file answers is narrow and hostile: given a
CURATED status file the tool did not author, does a status flip change
exactly one token and leave every other byte alone? Everything else here
exists to make that answer trustworthy on the shapes a real file takes.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from claudomater.cli import EXIT_ERROR, EXIT_OK, main
from claudomater.learnstore import LearnStore
from claudomater.sprint import (
    SprintDoc,
    SprintError,
    export,
    import_doc,
    import_path,
    round_trip_ok,
    set_status,
    statuses,
    stories,
    unknown_statuses,
)

FIXTURE = Path(__file__).parent / "fixtures" / "sprint-status-sample.yaml"

# The ui3 file the acceptance proof runs against. READ-ONLY, never
# committed, never quoted in this repo: the proof reports pass/fail, the
# fixture above carries the shapes.
UI3_SPRINT_STATUS = Path(
    os.environ.get(
        "OMATER_UI3_SPRINT_STATUS",
        Path.home()
        / "sourcecode/ui3/_bmad-output/implementation-artifacts/sprint-status.yaml",
    )
)


@pytest.fixture
def doc() -> SprintDoc:
    return SprintDoc.read(FIXTURE)


@pytest.fixture
def store(tmp_path) -> LearnStore:
    s = LearnStore.open(tmp_path / "learn.db", tmp_path / "export")
    yield s
    s.close()


@pytest.fixture
def workfile(tmp_path) -> Path:
    """A writable copy of the fixture - the committed one is never touched."""
    p = tmp_path / "sprint-status.yaml"
    p.write_bytes(FIXTURE.read_bytes())
    return p


class TestFixtureCarriesTheRequiredShapes:
    """The fixture is only worth what it deliberately covers. These assert
    the hazards are PRESENT, so a later 'tidy-up' of the fixture that
    removed one would fail here instead of silently weakening every proof
    below."""

    def test_structural_change_log_footer_is_present(self):
        text = FIXTURE.read_text(encoding="utf-8")
        assert "# STRUCTURAL CHANGE LOG" in text
        # ...and it sits AFTER the data block, which is what makes it a
        # footer the exporter has to walk past without touching
        assert text.index("# STRUCTURAL CHANGE LOG") > text.index("development_status:")
        # multi-line continuation shape (dated line + deep indent)
        assert "\n#             paperwork findings fixed at close." in text

    def test_legacy_optional_retro_line_is_present(self, doc):
        assert doc.entry("epic-3-retrospective").status == "optional"

    def test_fixture_carries_every_story_status_in_the_vocabulary(self, doc):
        seen = {e.status for e in doc.entries if e.kind == "story"}
        assert seen == {
            "backlog",
            "ready-for-dev",
            "in-progress",
            "review",
            "done",
            "deferred",
            "scrapped",
            "superseded",
        }

    def test_fixture_carries_both_inline_comment_gaps(self):
        text = FIXTURE.read_text(encoding="utf-8")
        assert ": backlog #single-space" in text  # one space
        assert ": done  # close review run" in text  # two spaces


class TestRoundTripIsByteExact:
    def test_parse_then_render_reproduces_the_fixture_bytes(self):
        assert round_trip_ok(FIXTURE)

    def test_render_preserves_a_missing_final_newline(self):
        text = "development_status:\n  epic-1: done"
        assert SprintDoc.parse(text).render() == text

    def test_render_preserves_crlf_line_endings(self):
        text = "development_status:\r\n  epic-1: done  # note\r\n"
        assert SprintDoc.parse(text).render() == text

    def test_render_preserves_a_blank_final_line(self):
        text = "development_status:\n  epic-1: done\n\n"
        assert SprintDoc.parse(text).render() == text

    def test_empty_document_round_trips(self):
        assert SprintDoc.parse("").render() == ""


class TestFlipTouchesExactlyOneToken:
    def test_only_the_flipped_line_differs(self, doc):
        before = doc.render().splitlines(keepends=True)
        after = doc.with_statuses({"4-3-being-worked": "review"}).render().splitlines(
            keepends=True
        )
        assert len(before) == len(after)
        differing = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
        assert len(differing) == 1
        assert before[differing[0]].strip() == "4-3-being-worked: in-progress"
        assert after[differing[0]].strip() == "4-3-being-worked: review"

    def test_a_trailing_comment_and_its_exact_spacing_survive_a_flip(self, doc):
        line = _line_for(doc.with_statuses({"epic-4-retrospective": "done"}), "epic-4-retrospective")
        assert line == "  epic-4-retrospective: done\n"
        # the one-space-gap line is the one a naive re-render would
        # normalize to two spaces
        flipped = doc.with_statuses({"4-6-tight-comment-gap": "done"})
        assert _line_for(flipped, "4-6-tight-comment-gap") == (
            "  4-6-tight-comment-gap: done #single-space gap before the comment, "
            "unlike the two-space lines\n"
        )

    def test_a_longer_status_does_not_disturb_the_comment(self, doc):
        flipped = doc.with_statuses({"4-5-not-yet-drafted": "ready-for-dev"})
        assert _line_for(flipped, "4-5-not-yet-drafted") == (
            "  4-5-not-yet-drafted: ready-for-dev\n"
        )
        line = _line_for(
            doc.with_statuses({"1-1-scaffold-the-widget-registry": "in-progress"}),
            "1-1-scaffold-the-widget-registry",
        )
        assert line == "  1-1-scaffold-the-widget-registry: in-progress\n"

    def test_flipping_to_the_same_value_is_a_no_op(self, doc):
        assert doc.with_statuses({"epic-1": "done"}).render() == doc.render()

    def test_no_updates_returns_the_same_document(self, doc):
        assert doc.with_statuses({}).render() == doc.render()

    def test_the_change_log_footer_is_untouched_by_a_flip(self, doc):
        flipped = doc.with_statuses({"4-3-being-worked": "done"})
        footer = flipped.render().split("# STRUCTURAL CHANGE LOG")[1]
        original = doc.render().split("# STRUCTURAL CHANGE LOG")[1]
        assert footer == original

    def test_flipping_an_unknown_key_fails_loudly(self, doc):
        with pytest.raises(SprintError, match="no such key"):
            doc.with_statuses({"9-9-never-existed": "done"})


class TestLegacyValuesAreNeverCorrected:
    """Clay's rider: `optional` is banned for new epics but historical
    lines are audit records. Reading never validates; only writes do."""

    def test_an_optional_retro_survives_a_full_export(self, store, workfile):
        import_path(store, "sample", workfile)
        set_status(store, "sample", "4-3-being-worked", "review", workfile)
        assert SprintDoc.read(workfile).entry("epic-3-retrospective").status == "optional"

    def test_optional_imports_into_the_db_verbatim(self, store, doc):
        import_doc(store, "sample", doc)
        assert statuses(store, "sample")["epic-3-retrospective"] == "optional"

    def test_optional_is_reported_as_unknown_but_not_corrected(self, doc):
        flagged = {e.key for e in unknown_statuses(doc)}
        assert flagged == {"epic-3-retrospective"}

    def test_writing_optional_is_refused(self, doc):
        with pytest.raises(SprintError, match="not a writable retro status"):
            doc.with_statuses({"epic-4-retrospective": "optional"})


class TestWriteVocabularyIsEnforcedPerKind:
    def test_a_story_status_is_refused_on_an_epic_line(self, doc):
        with pytest.raises(SprintError, match="not a writable epic status"):
            doc.with_statuses({"epic-4": "review"})

    def test_an_epic_cannot_take_a_retro_status(self, doc):
        with pytest.raises(SprintError, match="not a writable epic status"):
            doc.with_statuses({"epic-4": "fable-review-required"})

    def test_a_retro_cannot_take_a_story_status(self, doc):
        with pytest.raises(SprintError, match="not a writable retro status"):
            doc.with_statuses({"epic-4-retrospective": "in-progress"})

    def test_a_typo_is_refused(self, doc):
        with pytest.raises(SprintError, match="not a writable story status"):
            doc.with_statuses({"4-3-being-worked": "in_progress"})


class TestEpicAttribution:
    """A story key cannot be parsed into an epic - `2-3-1-x` is ambiguous
    between epic 2 and epic 2-3 - so attribution is positional."""

    def test_a_sub_epics_stories_belong_to_the_sub_epic(self, doc):
        assert doc.entry("2-3-1-sub-epic-first-story").epic == "2-3"
        assert doc.entry("2-3-2-1-a-four-segment-story-key").epic == "2-3"

    def test_a_non_numeric_story_segment_still_attributes(self, doc):
        assert doc.entry("3-T-truth-up-pass-with-a-letter-segment").epic == "3"

    def test_epic_and_retro_lines_take_their_epic_from_the_key(self, doc):
        assert doc.entry("epic-2-3").epic == "2-3"
        assert doc.entry("epic-2-3-retrospective").epic == "2-3"
        assert doc.entry("epic-2-3-retrospective").kind == "retro"

    def test_the_project_retro_is_not_swept_into_the_last_epic(self, doc):
        entry = doc.entry("project-retrospective")
        assert entry.epic == ""
        assert entry.kind == "retro"

    def test_kinds_are_classified(self, doc):
        assert doc.entry("epic-4").kind == "epic"
        assert doc.entry("4-1-already-shipped").kind == "story"
        assert doc.entry("epic-1-retrospective").kind == "retro"


class TestParsingFailsLoudly:
    def test_a_duplicate_key_is_refused(self):
        text = "development_status:\n  epic-1: done\n  epic-1: backlog\n"
        with pytest.raises(SprintError, match="duplicate key 'epic-1'"):
            SprintDoc.parse(text)

    def test_an_unparseable_entry_inside_the_block_is_refused(self):
        # a value carrying whitespace is not a status token; passing it
        # through silently would put the file and the DB out of sync with
        # nothing to show for it
        text = "development_status:\n  epic-1: done and also dusted\n"
        with pytest.raises(SprintError, match="unparseable entry"):
            SprintDoc.parse(text)

    def test_the_error_names_the_source_line(self):
        text = "development_status:\n  epic-1: done\n  broken here\n"
        with pytest.raises(SprintError, match="line 3"):
            SprintDoc.parse(text)

    def test_content_outside_the_data_block_is_never_parsed_as_an_entry(self):
        text = (
            "project: sample\n"
            "story_location: some/path\n"
            "development_status:\n"
            "  epic-1: done\n"
        )
        assert [e.key for e in SprintDoc.parse(text).entries] == ["epic-1"]

    def test_a_dedent_ends_the_data_block(self):
        text = (
            "development_status:\n"
            "  epic-1: done\n"
            "other_map:\n"
            "  this line: has spaces and is not an entry\n"
        )
        assert [e.key for e in SprintDoc.parse(text).entries] == ["epic-1"]

    def test_a_file_without_a_data_block_has_no_entries(self):
        assert SprintDoc.parse("project: sample\n# nothing here\n").entries == ()


class TestDatabaseRoundTrip:
    def test_import_then_export_changes_nothing(self, store, workfile):
        before = workfile.read_bytes()
        import_path(store, "sample", workfile)
        assert export(store, "sample", workfile) is False
        assert workfile.read_bytes() == before

    def test_a_db_flip_writes_through_to_the_file(self, store, workfile):
        import_path(store, "sample", workfile)
        assert set_status(store, "sample", "4-3-being-worked", "review", workfile) is True
        assert SprintDoc.read(workfile).entry("4-3-being-worked").status == "review"
        assert statuses(store, "sample")["4-3-being-worked"] == "review"

    def test_write_through_touches_only_the_flipped_line(self, store, workfile):
        before = workfile.read_text(encoding="utf-8").splitlines(keepends=True)
        import_path(store, "sample", workfile)
        set_status(store, "sample", "4-2-awaiting-review", "done", workfile)
        after = workfile.read_text(encoding="utf-8").splitlines(keepends=True)
        differing = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
        assert len(before) == len(after) and len(differing) == 1

    def test_a_rejected_status_never_reaches_the_db_or_the_file(self, store, workfile):
        import_path(store, "sample", workfile)
        before = workfile.read_bytes()
        with pytest.raises(SprintError, match="not a writable story status"):
            set_status(store, "sample", "4-3-being-worked", "shipped", workfile)
        assert workfile.read_bytes() == before
        assert statuses(store, "sample")["4-3-being-worked"] == "in-progress"

    def test_setting_an_untracked_key_is_refused(self, store, workfile):
        import_path(store, "sample", workfile)
        with pytest.raises(SprintError, match="no such key"):
            set_status(store, "sample", "9-9-invented", "done", workfile)

    def test_exporting_with_an_empty_db_is_refused(self, store, workfile):
        with pytest.raises(SprintError, match="no tracked stories"):
            export(store, "sample", workfile)

    def test_a_tracked_key_absent_from_the_file_is_loud_not_appended(
        self, store, workfile
    ):
        import_path(store, "sample", workfile)
        with store.conn:
            store.conn.execute(
                "INSERT INTO story(project, key, epic, status, updated_at) "
                "VALUES('sample','4-7-added-by-planning','4','backlog','t')"
            )
        with pytest.raises(SprintError, match="never adds lines"):
            export(store, "sample", workfile)

    def test_projects_are_isolated(self, store, workfile):
        import_path(store, "sample", workfile)
        import_path(store, "other", workfile)
        set_status(store, "sample", "4-3-being-worked", "review", workfile)
        assert statuses(store, "other")["4-3-being-worked"] == "in-progress"

    def test_reimporting_adopts_a_hand_edit(self, store, workfile):
        import_path(store, "sample", workfile)
        text = workfile.read_text(encoding="utf-8").replace(
            "4-1-already-shipped: done", "4-1-already-shipped: review"
        )
        workfile.write_text(text, encoding="utf-8")
        import_path(store, "sample", workfile)
        assert statuses(store, "sample")["4-1-already-shipped"] == "review"

    def test_the_status_view_reads_from_the_tables(self, store, workfile):
        import_path(store, "sample", workfile)
        epic4 = stories(store, "sample", epic="4")
        keys = [s["key"] for s in epic4]
        assert "epic-4" in keys and "4-3-being-worked" in keys
        assert "project-retrospective" not in keys
        assert all(s["epic"] == "4" for s in epic4)

    def test_the_status_view_is_ordered_deterministically(self, store, workfile):
        import_path(store, "sample", workfile)
        keys = [s["key"] for s in stories(store, "sample")]
        assert keys == sorted(keys)


class TestUi3AcceptanceProof:
    """The slice C acceptance: byte-exact round-trip against the REAL
    ui3 sprint-status.yaml. READ-ONLY - this never writes to it, and its
    content is never copied into this repo. Skipped where the file is not
    present, which is every machine but the operator's."""

    @pytest.mark.skipif(
        not UI3_SPRINT_STATUS.is_file(), reason="ui3 sprint-status.yaml not present"
    )
    def test_the_real_file_round_trips_byte_exactly(self):
        assert round_trip_ok(UI3_SPRINT_STATUS)

    @pytest.mark.skipif(
        not UI3_SPRINT_STATUS.is_file(), reason="ui3 sprint-status.yaml not present"
    )
    def test_a_flip_on_a_copy_of_the_real_file_touches_one_line(self, tmp_path):
        copy = tmp_path / "sprint-status.yaml"
        copy.write_bytes(UI3_SPRINT_STATUS.read_bytes())
        doc = SprintDoc.read(copy)
        target = next(e for e in doc.entries if e.kind == "story" and e.status != "done")
        before = doc.render().splitlines(keepends=True)
        after = doc.with_statuses({target.key: "done"}).render().splitlines(keepends=True)
        differing = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
        assert len(before) == len(after) and len(differing) == 1


class TestTheWriterAndItsExportNeverDisagree:
    """Write-through means the file write is PART of the operation: if it
    cannot land, the DB write must not survive it either."""

    def test_a_failed_file_write_rolls_the_db_write_back(
        self, store, workfile, monkeypatch
    ):
        import claudomater.sprint as sprint_mod

        import_path(store, "sample", workfile)
        before = workfile.read_bytes()

        def boom(path, text):
            raise OSError("disk full")

        monkeypatch.setattr(sprint_mod, "_write_atomically", boom)
        with pytest.raises(OSError, match="disk full"):
            set_status(store, "sample", "4-3-being-worked", "review", workfile)
        assert workfile.read_bytes() == before
        # the DB must still say what the FILE says, not what the failed
        # write intended
        assert statuses(store, "sample")["4-3-being-worked"] == "in-progress"

    def test_a_partial_write_never_replaces_the_file(self, store, workfile, monkeypatch):
        """The temp file is cleaned up and the target keeps its content."""
        import claudomater.sprint as sprint_mod

        import_path(store, "sample", workfile)
        before = workfile.read_bytes()

        def fail_replace(src, dst):
            raise OSError("rename failed")

        monkeypatch.setattr(sprint_mod.os, "replace", fail_replace)
        with pytest.raises(OSError, match="rename failed"):
            set_status(store, "sample", "4-3-being-worked", "review", workfile)
        assert workfile.read_bytes() == before
        leftovers = [p.name for p in workfile.parent.iterdir() if "omater-tmp" in p.name]
        assert leftovers == []
        assert statuses(store, "sample")["4-3-being-worked"] == "in-progress"

    def test_the_files_mode_survives_a_rewrite(self, store, workfile):
        import_path(store, "sample", workfile)
        os.chmod(workfile, 0o640)
        set_status(store, "sample", "4-3-being-worked", "review", workfile)
        assert stat.S_IMODE(workfile.stat().st_mode) == 0o640


class TestSprintCli:
    def _args(self, tmp_path, *rest):
        return [
            "sprint", *rest,
            "--user-config", str(tmp_path / "missing.yaml"),
            "--db", str(tmp_path / "cli.db"),
            "--export-dir", str(tmp_path / "cli-lessons"),
            "--sprint-project", "sample",
        ]

    def test_import_status_set_wiring(self, tmp_path, workfile, capsys):
        assert main(self._args(tmp_path, "import", str(workfile))) == EXIT_OK
        assert "imported" in capsys.readouterr().out
        assert main(self._args(tmp_path, "status", "--epic", "4")) == EXIT_OK
        out = capsys.readouterr().out
        assert "4-3-being-worked: in-progress" in out
        assert main(
            self._args(tmp_path, "set", "4-3-being-worked", "review", str(workfile))
        ) == EXIT_OK
        assert SprintDoc.read(workfile).entry("4-3-being-worked").status == "review"

    def test_import_reports_legacy_values_without_correcting_them(
        self, tmp_path, workfile, capsys
    ):
        assert main(self._args(tmp_path, "import", str(workfile))) == EXIT_OK
        out = capsys.readouterr().out
        assert "legacy value (left untouched)" in out
        assert "epic-3-retrospective: optional" in out
        assert SprintDoc.read(workfile).entry("epic-3-retrospective").status == "optional"

    def test_a_bad_status_is_a_cli_error_not_a_traceback(
        self, tmp_path, workfile, capsys
    ):
        assert main(self._args(tmp_path, "import", str(workfile))) == EXIT_OK
        capsys.readouterr()
        rc = main(
            self._args(tmp_path, "set", "4-3-being-worked", "shipped", str(workfile))
        )
        assert rc == EXIT_ERROR
        assert "not a writable story status" in capsys.readouterr().err

    def test_a_missing_file_is_a_cli_error_not_a_traceback(self, tmp_path, capsys):
        rc = main(self._args(tmp_path, "import", str(tmp_path / "nope.yaml")))
        assert rc == EXIT_ERROR
        assert "cannot read the status file" in capsys.readouterr().err

    def test_export_reports_already_in_sync(self, tmp_path, workfile, capsys):
        assert main(self._args(tmp_path, "import", str(workfile))) == EXIT_OK
        capsys.readouterr()
        assert main(self._args(tmp_path, "export", str(workfile))) == EXIT_OK
        assert "already in sync" in capsys.readouterr().out

    def test_status_json_is_machine_readable(self, tmp_path, workfile, capsys):
        assert main(self._args(tmp_path, "import", str(workfile))) == EXIT_OK
        capsys.readouterr()
        assert main(self._args(tmp_path, "status", "--json")) == EXIT_OK
        rows = json.loads(capsys.readouterr().out)
        assert {"key", "epic", "status", "updated_at"} == set(rows[0])


def _line_for(doc: SprintDoc, key: str) -> str:
    """The rendered line carrying `key`, with its line ending."""
    for line in doc.render().splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith(f"{key}:"):
            return line
    raise AssertionError(f"no rendered line for {key!r}")
