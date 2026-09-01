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
    require_dod,
    set_story_file_status,
    import_doc,
    import_path,
    orphaned_keys,
    round_trip_ok,
    set_status,
    statuses,
    stories,
    unknown_statuses,
)

FIXTURE = Path(__file__).parent / "fixtures" / "sprint-status-sample.yaml"

# The real consumer sprint file the acceptance proofs replay against.
# READ-ONLY, never committed, never quoted in this repo: the proof
# reports pass/fail, the fixture above carries the shapes. OPT-IN: point
# OMATER_PARITY_SPRINT_STATUS at the file to run them; unset skips.
PARITY_SPRINT_STATUS = Path(
    os.environ.get("OMATER_PARITY_SPRINT_STATUS") or "/nonexistent"
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
    """Operator rider: `optional` is banned for new epics but historical
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


class TestParityAcceptanceProof:
    """The slice C acceptance: byte-exact round-trip against the REAL
    consumer sprint-status.yaml. READ-ONLY - this never writes to it, and its
    content is never copied into this repo. Skipped where the file is not
    present, which is every machine but the operator's."""

    @pytest.mark.skipif(
        not PARITY_SPRINT_STATUS.is_file(), reason="parity sprint file not configured (OMATER_PARITY_SPRINT_STATUS)"
    )
    def test_the_real_file_round_trips_byte_exactly(self):
        assert round_trip_ok(PARITY_SPRINT_STATUS)

    @pytest.mark.skipif(
        not PARITY_SPRINT_STATUS.is_file(), reason="parity sprint file not configured (OMATER_PARITY_SPRINT_STATUS)"
    )
    def test_a_flip_on_a_copy_of_the_real_file_touches_one_line(self, tmp_path):
        copy = tmp_path / "sprint-status.yaml"
        copy.write_bytes(PARITY_SPRINT_STATUS.read_bytes())
        doc = SprintDoc.read(copy)
        # pick ANY story and flip it to a status it does not already hold:
        # requiring a non-done story would raise StopIteration - failing an
        # operator-only proof for a reason that has nothing to do with the
        # exporter - on the day the real sprint is fully closed out
        stories_in_file = [e for e in doc.entries if e.kind == "story"]
        assert stories_in_file, "the real file carries no story lines to flip"
        target = stories_in_file[0]
        new_status = "review" if target.status != "review" else "done"
        before = doc.render().splitlines(keepends=True)
        after = doc.with_statuses({target.key: new_status}).render().splitlines(
            keepends=True
        )
        differing = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
        assert len(before) == len(after) and len(differing) == 1


class TestOnDiskBytesSurviveExactly:
    """PR #13 round 1. The in-memory CRLF test proved nothing about FILES:
    `Path.read_text()` uses universal newlines, so a CRLF file was
    normalized to LF on read and rewritten with different bytes - and
    `round_trip_ok()` returned True while doing it, which is a proof
    function that a live defect could satisfy. Every check here goes
    through the filesystem."""

    CRLF = (
        b"# a curated header\r\n"
        b"development_status:\r\n"
        b"  epic-1: in-progress\r\n"
        b"  1-1-a-story: backlog  # with a trailing comment\r\n"
        b"# STRUCTURAL CHANGE LOG\r\n"
        b"# 2026-01-01: something structural happened\r\n"
    )

    def test_round_trip_ok_verdict_is_backed_by_the_actual_bytes(self, tmp_path):
        """Asserting `round_trip_ok(p) is True` would pass on the BUG -
        it returned True by comparing normalized text to normalized text.
        The verdict has to agree with a byte-level comparison, which is
        the thing it claims to be reporting."""
        p = tmp_path / "crlf.yaml"
        p.write_bytes(self.CRLF)
        truly_exact = SprintDoc.read(p).render().encode("utf-8") == p.read_bytes()
        assert round_trip_ok(p) == truly_exact
        assert truly_exact
        assert p.read_bytes() == self.CRLF  # the check itself must not rewrite

    def test_reading_a_crlf_file_preserves_its_line_endings(self, tmp_path):
        p = tmp_path / "crlf.yaml"
        p.write_bytes(self.CRLF)
        assert SprintDoc.read(p).render().encode("utf-8") == self.CRLF

    def test_a_noop_export_leaves_crlf_bytes_untouched(self, store, tmp_path):
        p = tmp_path / "crlf.yaml"
        p.write_bytes(self.CRLF)
        import_path(store, "sample", p)
        assert export(store, "sample", p) is False
        assert p.read_bytes() == self.CRLF

    def test_a_flip_on_a_crlf_file_changes_only_the_status_token(
        self, store, tmp_path
    ):
        p = tmp_path / "crlf.yaml"
        p.write_bytes(self.CRLF)
        import_path(store, "sample", p)
        set_status(store, "sample", "1-1-a-story", "done", p)
        assert p.read_bytes() == self.CRLF.replace(
            b"1-1-a-story: backlog", b"1-1-a-story: done"
        )

    @pytest.mark.parametrize(
        "sep",
        [
            b"\r",
            b"\x0b",
            b"\x0c",
            # ESCAPED, never a literal: an invisible separator sitting in
            # source is unreviewable and editors/formatters silently eat it
            "\u2028".encode("utf-8"),
            "\u2029".encode("utf-8"),
            "\x85".encode("utf-8"),
        ],
    )
    def test_an_embedded_line_separator_is_refused_not_silently_split(
        self, tmp_path, sep
    ):
        """PR #13 round 2. `splitlines()` breaks on \\r, \\x0b, \\x0c and
        U+2028 as well as \\n. A status value carrying one was split in
        two and the leading fragment parsed as the WHOLE value - so
        `epic-1: do\\rne` recorded status 'do', and a later flip rewrote
        that span to produce `epic-1: done\\rne`. Silent corruption of a
        curated document, which is the one thing this module must never
        do. The previous version of this test used \\x0b, the one
        separator the regex already refused, so it never covered the
        dangerous case."""
        p = tmp_path / "odd.yaml"
        p.write_bytes(b"development_status:\n  epic-1: do" + sep + b"ne\n")
        with pytest.raises(SprintError, match="unparseable entry"):
            SprintDoc.read(p)

    def test_a_crlf_blank_line_does_not_end_the_data_block(self, tmp_path):
        """A blank line is `\\r\\n` in a CRLF file, and treating it as a
        dedent silently DROPS every entry after it - the stories would
        never reach the DB and nothing would say so."""
        p = tmp_path / "crlf-gap.yaml"
        p.write_bytes(
            b"development_status:\r\n"
            b"  epic-1: done\r\n"
            b"\r\n"
            b"  1-1-after-the-gap: backlog\r\n"
        )
        assert [e.key for e in SprintDoc.read(p).entries] == [
            "epic-1",
            "1-1-after-the-gap",
        ]

    @pytest.mark.parametrize(
        "line",
        [
            "  # Epic 2: a section header with text\n",
            "#\n",
            "    #deeply indented, no space after the hash\n",
            "\n",
            "   \n",
        ],
    )
    def test_comment_and_blank_shapes_inside_the_block_are_passed_through(
        self, tmp_path, line
    ):
        """PR #13 round 6 raised this as a defect (claiming only a bare
        `#` matches). It does not reproduce - the pattern is applied with
        match(), not fullmatch(), so `#` matching the first character is
        enough - but the real file carries 298 such lines inside its data
        block, so the behaviour is worth pinning rather than arguing."""
        p = tmp_path / "c.yaml"
        raw = ("development_status:\n  epic-1: done\n" + line + "  epic-2: backlog\n")
        p.write_text(raw, encoding="utf-8")
        doc = SprintDoc.read(p)
        assert [e.key for e in doc.entries] == ["epic-1", "epic-2"]
        assert doc.render() == raw

    def test_a_crlf_comment_line_inside_the_block_is_passed_through(self, tmp_path):
        p = tmp_path / "crlf-comment.yaml"
        raw = (
            b"development_status:\r\n"
            b"  epic-1: done\r\n"
            b"  # Epic 2: the next one\r\n"
            b"  epic-2: backlog\r\n"
        )
        p.write_bytes(raw)
        doc = SprintDoc.read(p)
        assert [e.key for e in doc.entries] == ["epic-1", "epic-2"]
        assert doc.render().encode("utf-8") == raw

    def test_a_non_utf8_file_raises_a_typed_error_at_the_library_boundary(
        self, tmp_path
    ):
        """Converted in `_read_exact`, not in the CLI, so a driver calling
        the library directly gets the same typed failure."""
        p = tmp_path / "not-text.yaml"
        p.write_bytes(b"development_status:\n  epic-1: d\xff\xfeone\n")
        with pytest.raises(SprintError, match="not valid UTF-8"):
            SprintDoc.read(p)

    def test_a_file_with_no_trailing_newline_round_trips_on_disk(self, tmp_path):
        p = tmp_path / "nonl.yaml"
        raw = b"development_status:\n  epic-1: done"
        p.write_bytes(raw)
        assert round_trip_ok(p)
        assert SprintDoc.read(p).render().encode("utf-8") == raw

    def test_a_flip_never_appends_a_trailing_newline(self, store, tmp_path):
        p = tmp_path / "nonl.yaml"
        p.write_bytes(b"development_status:\n  epic-1: backlog")
        import_path(store, "sample", p)
        set_status(store, "sample", "epic-1", "done", p)
        assert p.read_bytes() == b"development_status:\n  epic-1: done"

    def test_utf8_content_outside_the_status_map_survives(self, store, tmp_path):
        p = tmp_path / "utf8.yaml"
        raw = "development_status:\n  epic-1: backlog  # café — naïve\n".encode("utf-8")
        p.write_bytes(raw)
        import_path(store, "sample", p)
        set_status(store, "sample", "epic-1", "done", p)
        assert p.read_bytes() == raw.replace(b"epic-1: backlog", b"epic-1: done")


class TestUpdatedAtMeansWhenTheStatusChanged:
    """PR #13 round 4. `import_doc` documents `updated_at` as "when this
    status last changed" and skips the write when nothing changed;
    `set_status` bumped it unconditionally, so the two write paths meant
    different things by the same column."""

    def _updated_at(self, store, key):
        return store.conn.execute(
            "SELECT updated_at FROM story WHERE project='sample' AND key=?", (key,)
        ).fetchone()[0]

    def test_setting_a_status_to_its_current_value_does_not_bump_updated_at(
        self, store, workfile
    ):
        import_path(store, "sample", workfile)
        before = self._updated_at(store, "4-3-being-worked")
        set_status(store, "sample", "4-3-being-worked", "in-progress", workfile)
        assert self._updated_at(store, "4-3-being-worked") == before

    def test_a_real_change_does_bump_updated_at(self, store, workfile):
        import_path(store, "sample", workfile)
        before = self._updated_at(store, "4-3-being-worked")
        set_status(store, "sample", "4-3-being-worked", "review", workfile)
        assert self._updated_at(store, "4-3-being-worked") > before

    def test_a_noop_set_still_resyncs_a_diverged_file(self, store, workfile):
        """The DB not changing does not mean the FILE agrees with it - a
        hand edit can have moved the file, and write-through is what puts
        it back."""
        import_path(store, "sample", workfile)
        hand_edited = workfile.read_text(encoding="utf-8").replace(
            "4-3-being-worked: in-progress", "4-3-being-worked: backlog"
        )
        workfile.write_text(hand_edited, encoding="utf-8")
        assert set_status(
            store, "sample", "4-3-being-worked", "in-progress", workfile
        ) is True
        assert SprintDoc.read(workfile).entry("4-3-being-worked").status == "in-progress"

    def test_a_membership_only_move_does_not_bump_updated_at(self, store, tmp_path):
        """PR #13 round 9. The UPSERT bumped `updated_at` when only the
        EPIC changed, so a story moved between epic blocks looked like a
        status change - the same column meaning two things again, which
        round 4 fixed for set_status but not for import."""
        p = tmp_path / "s.yaml"
        p.write_text(
            "development_status:\n  epic-1: done\n  9-1-a-story: backlog\n"
            "  epic-2: backlog\n",
            encoding="utf-8",
        )
        import_path(store, "sample", p)
        before = store.conn.execute(
            "SELECT epic, updated_at FROM story WHERE project='sample' "
            "AND key='9-1-a-story'"
        ).fetchone()
        assert before["epic"] == "1"
        # the same story, now listed under epic 2
        p.write_text(
            "development_status:\n  epic-1: done\n  epic-2: backlog\n"
            "  9-1-a-story: backlog\n",
            encoding="utf-8",
        )
        import_path(store, "sample", p)
        after = store.conn.execute(
            "SELECT epic, updated_at FROM story WHERE project='sample' "
            "AND key='9-1-a-story'"
        ).fetchone()
        assert after["epic"] == "2"  # membership DID move
        assert after["updated_at"] == before["updated_at"]  # the status did not

    def test_a_status_change_during_import_still_bumps_updated_at(
        self, store, workfile
    ):
        import_path(store, "sample", workfile)
        before = store.conn.execute(
            "SELECT updated_at FROM story WHERE project='sample' "
            "AND key='4-3-being-worked'"
        ).fetchone()[0]
        workfile.write_text(
            workfile.read_text(encoding="utf-8").replace(
                "4-3-being-worked: in-progress", "4-3-being-worked: review"
            ),
            encoding="utf-8",
        )
        import_path(store, "sample", workfile)
        after = store.conn.execute(
            "SELECT updated_at FROM story WHERE project='sample' "
            "AND key='4-3-being-worked'"
        ).fetchone()[0]
        assert after > before

    def test_a_key_in_the_file_but_not_the_db_is_refused_as_untracked(
        self, store, workfile
    ):
        """The 'not tracked' branch: the file carries the key, the DB does
        not. Distinct from 'no such key', which is the file's answer."""
        import_path(store, "sample", workfile)
        with store.conn:
            store.conn.execute(
                "DELETE FROM story WHERE project='sample' AND key='4-3-being-worked'"
            )
        with pytest.raises(SprintError, match="not tracked"):
            set_status(store, "sample", "4-3-being-worked", "review", workfile)


class TestImportRefreshesMembershipLoudly:
    """PR #13 round 5. A key deleted from the file stayed in the DB, so
    `export` failed with "tracked but absent" immediately after a fresh
    import and no tool could clear it. Deleting is now possible but never
    automatic: the DB is on its way to being the writer, so dropping its
    rows because a DERIVED artifact lost a line is backwards, and a
    truncated file would silently delete real tracking."""

    def _drop_line(self, path: Path, key: str) -> None:
        kept = [
            line
            for line in path.read_text(encoding="utf-8").splitlines(keepends=True)
            if not line.strip().startswith(f"{key}:")
        ]
        path.write_text("".join(kept), encoding="utf-8")

    def test_a_key_removed_from_the_file_is_reported_not_deleted(
        self, store, workfile
    ):
        import_path(store, "sample", workfile)
        self._drop_line(workfile, "4-1-already-shipped")
        import_path(store, "sample", workfile)
        assert orphaned_keys(store, "sample", SprintDoc.read(workfile)) == [
            "4-1-already-shipped"
        ]
        assert "4-1-already-shipped" in statuses(store, "sample")

    def test_export_names_the_remedy_for_a_stale_tracked_key(self, store, workfile):
        import_path(store, "sample", workfile)
        self._drop_line(workfile, "4-1-already-shipped")
        with pytest.raises(SprintError, match="--prune"):
            export(store, "sample", workfile)

    def test_prune_removes_them_deliberately(self, store, workfile):
        import_path(store, "sample", workfile)
        self._drop_line(workfile, "4-1-already-shipped")
        import_path(store, "sample", workfile, prune=True)
        assert "4-1-already-shipped" not in statuses(store, "sample")
        assert export(store, "sample", workfile) is False

    def test_prune_never_reaches_another_project(self, store, workfile):
        import_path(store, "sample", workfile)
        import_path(store, "other", workfile)
        self._drop_line(workfile, "4-1-already-shipped")
        import_path(store, "sample", workfile, prune=True)
        assert "4-1-already-shipped" in statuses(store, "other")

    def test_importing_a_file_with_no_status_map_is_refused(self, store, tmp_path):
        """PR #13 round 8. `import` seeds from a sprint-status.yaml, so a
        document carrying no entries means the operator pointed at the
        wrong file or a truncated one - reporting "imported 0 row(s)" and
        exiting 0 calls that success."""
        wrong = tmp_path / "some-other.yaml"
        wrong.write_text("project: other\nsomething_else:\n  key: value\n", encoding="utf-8")
        with pytest.raises(SprintError, match="no status-map entries"):
            import_path(store, "sample", wrong)

    def test_an_empty_status_map_is_refused(self, store, tmp_path):
        p = tmp_path / "empty.yaml"
        p.write_text("project: x\ndevelopment_status:\n", encoding="utf-8")
        with pytest.raises(SprintError, match="no status-map entries"):
            import_path(store, "sample", p)

    def test_pruning_against_the_wrong_file_cannot_wipe_tracking(
        self, store, workfile, tmp_path
    ):
        """The severe case the refusal closes: --prune treats every
        tracked key absent from the document as an orphan, so an empty
        document made `import --prune wrong-file.yaml` delete the whole
        project's tracking and exit 0."""
        import_path(store, "sample", workfile)
        before = statuses(store, "sample")
        assert before
        wrong = tmp_path / "some-other.yaml"
        wrong.write_text("project: other\n", encoding="utf-8")
        with pytest.raises(SprintError, match="no status-map entries"):
            import_path(store, "sample", wrong, prune=True)
        assert statuses(store, "sample") == before

    def test_orphans_are_empty_for_a_matching_file(self, store, workfile):
        import_path(store, "sample", workfile)
        assert orphaned_keys(store, "sample", SprintDoc.read(workfile)) == []


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

    def test_concurrent_writers_do_not_share_a_temp_file(self, tmp_path, monkeypatch):
        """PR #13 round 7. A fixed temp name let a second writer overwrite
        the first writer's staged bytes, so the first writer's os.replace
        published content it never wrote (or failed spuriously because its
        temp had already been renamed away)."""
        import claudomater.sprint as sprint_mod

        target = tmp_path / "s.yaml"
        target.write_text("original\n", encoding="utf-8")
        seen: list[str] = []
        real_chmod = os.chmod
        fired = []

        def chmod_hook(p, mode):
            seen.append(str(p))
            # fire once, at the moment writer A has staged its temp but has
            # not yet replaced: a whole second writer runs start to finish
            if not fired:
                fired.append(True)
                sprint_mod._write_atomically(target, "writer-B\n")
            return real_chmod(p, mode)

        monkeypatch.setattr(sprint_mod.os, "chmod", chmod_hook)
        sprint_mod._write_atomically(target, "writer-A\n")
        monkeypatch.setattr(sprint_mod.os, "chmod", real_chmod)

        # A finished last, so A's bytes win - and A must not have failed
        assert target.read_text(encoding="utf-8") == "writer-A\n"
        # the two writers staged to DIFFERENT paths
        assert len(seen) == 2 and seen[0] != seen[1]
        assert [p.name for p in tmp_path.iterdir() if "omater-tmp" in p.name] == []

    def test_a_failing_fdopen_does_not_leak_the_descriptor(
        self, tmp_path, monkeypatch
    ):
        """PR #13 round 9. mkstemp hands back a RAW fd; until fdopen wraps
        it, nothing owns it. If fdopen itself raises, the outer handler
        unlinked the path but left the descriptor open."""
        import claudomater.sprint as sprint_mod

        target = tmp_path / "s.yaml"
        target.write_text("original\n", encoding="utf-8")
        captured: list[int] = []

        def failing_fdopen(fd, *a, **kw):
            captured.append(fd)
            raise OSError("fdopen refused")

        monkeypatch.setattr(sprint_mod.os, "fdopen", failing_fdopen)
        with pytest.raises(OSError, match="fdopen refused"):
            sprint_mod._write_atomically(target, "new\n")

        assert captured, "fdopen was never reached"
        with pytest.raises(OSError):  # EBADF: the descriptor was closed
            os.fstat(captured[0])
        assert target.read_text(encoding="utf-8") == "original\n"
        assert [p.name for p in tmp_path.iterdir() if "omater-tmp" in p.name] == []

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
        err = capsys.readouterr().err
        assert "status file I/O failed" in err and "nope.yaml" in err

    def test_a_write_failure_is_not_reported_as_a_read_failure(
        self, tmp_path, workfile, capsys, monkeypatch
    ):
        """PR #13 round 1: `export`/`set` write as well as read, so a
        fixed 'cannot read' message would misdirect triage."""
        import claudomater.sprint as sprint_mod

        assert main(self._args(tmp_path, "import", str(workfile))) == EXIT_OK
        capsys.readouterr()

        def boom(path, text):
            raise OSError("No space left on device")

        monkeypatch.setattr(sprint_mod, "_write_atomically", boom)
        rc = main(
            self._args(tmp_path, "set", "4-3-being-worked", "review", str(workfile))
        )
        assert rc == EXIT_ERROR
        err = capsys.readouterr().err
        assert "No space left on device" in err
        assert "cannot read" not in err

    def test_export_reports_already_in_sync(self, tmp_path, workfile, capsys):
        assert main(self._args(tmp_path, "import", str(workfile))) == EXIT_OK
        capsys.readouterr()
        assert main(self._args(tmp_path, "export", str(workfile))) == EXIT_OK
        assert "already in sync" in capsys.readouterr().out

    def test_a_resolve_failure_is_a_cli_error_not_a_traceback(
        self, tmp_path, capsys, monkeypatch
    ):
        """PR #13 round 5. `Path.resolve()` reads the filesystem and calls
        getcwd() for a relative path, so it raises OSError when the
        working directory has been deleted. Running it before the try
        let that escape as a traceback."""

        def boom(self, *a, **kw):
            raise FileNotFoundError(2, "No such file or directory")

        monkeypatch.setattr(Path, "resolve", boom)
        rc = main(self._args(tmp_path, "import", "sprint-status.yaml"))
        assert rc == EXIT_ERROR
        err = capsys.readouterr().err
        assert "status file I/O failed" in err
        # the raw argument, since resolve() never produced a path - "(None)"
        # would drop the one detail identifying which file was meant
        assert "sprint-status.yaml" in err and "None" not in err

    def test_import_reports_keys_the_file_no_longer_carries(
        self, tmp_path, workfile, capsys
    ):
        assert main(self._args(tmp_path, "import", str(workfile))) == EXIT_OK
        capsys.readouterr()
        kept = [
            line
            for line in workfile.read_text(encoding="utf-8").splitlines(keepends=True)
            if not line.strip().startswith("4-1-already-shipped:")
        ]
        workfile.write_text("".join(kept), encoding="utf-8")
        assert main(self._args(tmp_path, "import", str(workfile))) == EXIT_OK
        out = capsys.readouterr().out
        assert "tracked but absent from the file: 4-1-already-shipped" in out
        assert "--prune" in out
        # reported, NOT deleted
        assert main(self._args(tmp_path, "export", str(workfile))) == EXIT_ERROR

    def test_prune_clears_them_and_export_recovers(self, tmp_path, workfile, capsys):
        assert main(self._args(tmp_path, "import", str(workfile))) == EXIT_OK
        kept = [
            line
            for line in workfile.read_text(encoding="utf-8").splitlines(keepends=True)
            if not line.strip().startswith("4-1-already-shipped:")
        ]
        workfile.write_text("".join(kept), encoding="utf-8")
        capsys.readouterr()
        assert main(self._args(tmp_path, "import", str(workfile), "--prune")) == EXIT_OK
        assert "pruned: 4-1-already-shipped" in capsys.readouterr().out
        assert main(self._args(tmp_path, "export", str(workfile))) == EXIT_OK

    def test_a_non_utf8_file_is_a_cli_error_not_a_traceback(self, tmp_path, capsys):
        """PR #13 round 10. A decode failure is a ValueError, not an
        OSError, so it sailed past every I/O handler."""
        binary = tmp_path / "not-text.yaml"
        binary.write_bytes(b"development_status:\n  epic-1: d\xff\xfeone\n")
        rc = main(self._args(tmp_path, "import", str(binary)))
        assert rc == EXIT_ERROR
        err = capsys.readouterr().err
        assert "not valid UTF-8" in err and "not-text.yaml" in err

    def test_importing_the_wrong_file_is_a_cli_error(self, tmp_path, capsys):
        wrong = tmp_path / "some-other.yaml"
        wrong.write_text("project: other\nkeys:\n  a: b\n", encoding="utf-8")
        rc = main(self._args(tmp_path, "import", str(wrong)))
        assert rc == EXIT_ERROR
        assert "no status-map entries" in capsys.readouterr().err

    def test_status_json_is_machine_readable(self, tmp_path, workfile, capsys):
        assert main(self._args(tmp_path, "import", str(workfile))) == EXIT_OK
        capsys.readouterr()
        assert main(self._args(tmp_path, "status", "--json")) == EXIT_OK
        rows = json.loads(capsys.readouterr().out)
        assert {"key", "epic", "status", "updated_at"} == set(rows[0])


class TestSprintProjectResolution:
    """--sprint-project must never guess. The shipped default used to be
    a hardcoded consumer project name, so a fresh install that omitted
    the flag silently keyed every sprint row under someone else's
    project. An omitted flag now resolves from `.omater.yaml`'s
    `project` key in the cwd, and with neither the command refuses."""

    def _args(self, tmp_path, *rest):
        # like TestSprintCli._args, but WITHOUT --sprint-project: the
        # resolution path under test only runs when the flag is absent
        return [
            "sprint", *rest,
            "--user-config", str(tmp_path / "missing.yaml"),
            "--db", str(tmp_path / "cli.db"),
            "--export-dir", str(tmp_path / "cli-lessons"),
        ]

    def test_an_omitted_flag_reads_the_cwds_omater_yaml(
        self, tmp_path, workfile, monkeypatch, capsys
    ):
        (tmp_path / ".omater.yaml").write_text(
            "project: fromconfig\n", encoding="utf-8"
        )
        monkeypatch.chdir(tmp_path)
        assert main(self._args(tmp_path, "import", str(workfile))) == EXIT_OK
        capsys.readouterr()
        # the rows really landed under the config's key, not a constant
        assert main(
            self._args(
                tmp_path, "status", "--sprint-project", "fromconfig", "--json"
            )
        ) == EXIT_OK
        rows = json.loads(capsys.readouterr().out)
        assert "4-3-being-worked" in {r["key"] for r in rows}

    def test_an_explicit_flag_beats_the_config(
        self, tmp_path, workfile, monkeypatch, capsys
    ):
        (tmp_path / ".omater.yaml").write_text(
            "project: fromconfig\n", encoding="utf-8"
        )
        monkeypatch.chdir(tmp_path)
        assert main(
            self._args(
                tmp_path, "import", str(workfile), "--sprint-project", "explicit"
            )
        ) == EXIT_OK
        capsys.readouterr()
        # nothing under the config's name...
        assert main(self._args(tmp_path, "status")) == EXIT_OK
        assert "0 tracked row(s)" in capsys.readouterr().out
        # ...everything under the explicit one
        assert main(
            self._args(tmp_path, "status", "--sprint-project", "explicit")
        ) == EXIT_OK
        assert "4-3-being-worked" in capsys.readouterr().out

    def test_neither_flag_nor_config_refuses_loudly(
        self, tmp_path, workfile, monkeypatch, capsys
    ):
        monkeypatch.chdir(tmp_path)  # no .omater.yaml here
        rc = main(self._args(tmp_path, "import", str(workfile)))
        assert rc == EXIT_ERROR
        err = capsys.readouterr().err
        # the message names both remedies: the flag and the config key
        assert "--sprint-project" in err and ".omater.yaml" in err
        # resolution runs BEFORE the store opens: the refusal must not
        # leave a freshly created DB behind
        assert not (tmp_path / "cli.db").exists()


def _line_for(doc: SprintDoc, key: str) -> str:
    """The rendered line carrying `key`, with its line ending."""
    for line in doc.render().splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith(f"{key}:"):
            return line
    raise AssertionError(f"no rendered line for {key!r}")


# ---- slice B: epic creation + the retro-vocabulary gate --------------------


class TestAddEpicWritesTheBlock:
    """Phase 3 deliverable 2: epic creation writes the retro line at
    CREATION, always `fable-review-required`, with the file's every
    existing byte surviving the insertion exactly."""

    def _create(self, store, workfile, stories=("5-1-first", "5-2-second")):
        import_path(store, "sample", workfile)
        from claudomater.sprint import add_epic

        return add_epic(store, "sample", workfile, "5", stories=stories)

    def test_every_existing_byte_survives_the_insertion(self, store, workfile):
        """Total equality against a hand-built expectation: the new bytes
        are EXACTLY the old bytes with one block spliced in right after
        the last epic's retro line - nothing reformatted, nothing moved."""
        before = workfile.read_bytes()
        self._create(store, workfile)
        anchor = b"epic-4-retrospective: fable-review-required\n"
        cut = before.index(anchor) + len(anchor)
        assert workfile.read_bytes() == (
            before[:cut]
            + b"\n"
            + b"  epic-5: backlog\n"
            + b"  5-1-first: backlog\n"
            + b"  5-2-second: backlog\n"
            + b"  epic-5-retrospective: fable-review-required\n"
            + before[cut:]
        )

    def test_the_block_lands_before_the_project_scoped_tail(self, store, workfile):
        self._create(store, workfile)
        doc = SprintDoc.read(workfile)
        keys = [e.key for e in doc.entries]
        assert keys.index("epic-5") > keys.index("epic-4-retrospective")
        assert keys.index("epic-5-retrospective") < keys.index("project-retrospective")
        # membership is positional, so the parse must attribute the new
        # stories to the new epic
        assert doc.entry("5-1-first").epic == "5"

    def test_the_retro_line_is_pre_registered_and_constant(self, store, workfile):
        """The real files carry `fable-review-required` on epics that are
        still backlog (pre-registered at creation, not at close) - and
        add_epic has NO parameter that could write anything else."""
        import inspect

        from claudomater.sprint import add_epic

        self._create(store, workfile)
        assert (
            SprintDoc.read(workfile).entry("epic-5-retrospective").status
            == "fable-review-required"
        )
        params = inspect.signature(add_epic).parameters
        assert "retro_status" not in params  # unrepresentable, not defaulted

    def test_db_and_file_agree_after_creation(self, store, workfile):
        new_keys = self._create(store, workfile)
        assert new_keys == [
            "epic-5",
            "5-1-first",
            "5-2-second",
            "epic-5-retrospective",
        ]
        db = statuses(store, "sample")
        file_statuses = SprintDoc.read(workfile).statuses()
        for key in new_keys:
            assert db[key] == file_statuses[key]
        assert export(store, "sample", workfile) is False  # already in sync

    def test_an_epic_with_no_stories_is_legal(self, store, workfile):
        """The real files pre-register epics with a retro line and no
        stories yet - creation must support exactly that shape."""
        new_keys = self._create(store, workfile, stories=())
        assert new_keys == ["epic-5", "epic-5-retrospective"]

    def test_a_crlf_file_gains_crlf_lines(self, store, tmp_path):
        crlf = tmp_path / "crlf.yaml"
        crlf.write_bytes(
            b"development_status:\r\n"
            b"  epic-1: done\r\n"
            b"  epic-1-retrospective: done\r\n"
        )
        import_path(store, "sample", crlf)
        from claudomater.sprint import add_epic

        add_epic(store, "sample", crlf, "2", stories=("2-1-x",))
        assert crlf.read_bytes() == (
            b"development_status:\r\n"
            b"  epic-1: done\r\n"
            b"  epic-1-retrospective: done\r\n"
            b"\r\n"
            b"  epic-2: backlog\r\n"
            b"  2-1-x: backlog\r\n"
            b"  epic-2-retrospective: fable-review-required\r\n"
        )

    def test_indent_and_gap_are_copied_from_the_anchor(self, store, tmp_path):
        wide = tmp_path / "wide.yaml"
        wide.write_text(
            "development_status:\n"
            "    epic-1:   done\n"
            "    epic-1-retrospective:   done\n",
            encoding="utf-8",
        )
        import_path(store, "sample", wide)
        from claudomater.sprint import add_epic

        add_epic(store, "sample", wide, "2")
        assert "    epic-2:   backlog\n" in wide.read_text(encoding="utf-8")

    def test_sub_epic_ids_work(self, store, workfile):
        import_path(store, "sample", workfile)
        from claudomater.sprint import add_epic

        new_keys = add_epic(
            store, "sample", workfile, "5-1", stories=("5-1-1-nested",)
        )
        assert new_keys[-1] == "epic-5-1-retrospective"
        assert SprintDoc.read(workfile).entry("5-1-1-nested").epic == "5-1"

    def test_the_result_reparses_and_round_trips(self, store, workfile):
        from claudomater.sprint import round_trip_ok

        self._create(store, workfile)
        assert round_trip_ok(workfile)


class TestAddEpicRefusals:
    @pytest.fixture(autouse=True)
    def _seed(self, store, workfile):
        import_path(store, "sample", workfile)
        self.store = store
        self.workfile = workfile

    def _add(self, epic, **kwargs):
        from claudomater.sprint import add_epic

        return add_epic(self.store, "sample", self.workfile, epic, **kwargs)

    def test_bad_epic_ids_are_refused(self):
        for bad in ("abc", "5-", "-5", "5.1", "epic-5", ""):
            with pytest.raises(SprintError, match="epic id"):
                self._add(bad)

    def test_an_existing_epic_is_refused(self):
        with pytest.raises(SprintError, match="already in the status map"):
            self._add("4")

    def test_story_keys_must_carry_the_epics_prefix(self):
        with pytest.raises(SprintError, match="prefix"):
            self._add("5", stories=("6-1-wrong-epic",))

    def test_story_keys_that_classify_as_other_kinds_are_refused(self):
        with pytest.raises(SprintError, match="retro"):
            self._add("5", stories=("5-x-retrospective",))
        with pytest.raises(SprintError, match="not a legal story key|epic"):
            self._add("5", stories=("epic-5-1",))

    def test_duplicate_story_keys_are_refused(self):
        with pytest.raises(SprintError, match="duplicate"):
            self._add("5", stories=("5-1-a", "5-1-a"))

    def test_statuses_are_validated_against_the_write_vocabulary(self):
        with pytest.raises(SprintError, match="not a writable epic status"):
            self._add("5", epic_status="open")
        with pytest.raises(SprintError, match="not a writable story status"):
            self._add("5", stories=("5-1-a",), story_status="started")

    def test_a_document_with_no_epics_is_refused(self, store, tmp_path):
        bare = tmp_path / "bare.yaml"
        bare.write_text(
            "development_status:\n  project-retrospective: done\n",
            encoding="utf-8",
        )
        import_path(store, "bareproj", bare)
        from claudomater.sprint import add_epic

        with pytest.raises(SprintError, match="no epic entries"):
            add_epic(store, "bareproj", bare, "1")

    def test_a_missing_final_newline_at_the_anchor_is_refused(self, store, tmp_path):
        clipped = tmp_path / "clipped.yaml"
        clipped.write_bytes(
            b"development_status:\n  epic-1: done\n  epic-1-retrospective: done"
        )
        import_path(store, "clipproj", clipped)
        before = clipped.read_bytes()
        from claudomater.sprint import add_epic

        with pytest.raises(SprintError, match="final newline"):
            add_epic(store, "clipproj", clipped, "2")
        assert clipped.read_bytes() == before  # nothing rewritten

    def test_an_orphaned_db_row_is_a_loud_refusal_and_the_file_is_untouched(
        self, store, workfile
    ):
        """The DB already tracks a key the file lost (the orphan shape
        import/export refuse to paper over): creation must not resolve
        that divergence as a side effect - and because the DB insert and
        the file write share one transaction, neither side moves."""
        store.conn.execute(
            "INSERT INTO story(project, key, epic, status, updated_at) "
            "VALUES('sample', 'epic-5', '5', 'backlog', 'x')"
        )
        store.conn.commit()
        before = workfile.read_bytes()
        with pytest.raises(SprintError, match="already tracked in the DB"):
            self._add("5")
        assert workfile.read_bytes() == before

    def test_an_unwritable_file_rolls_the_db_back(self, store, workfile):
        workfile.chmod(0o444)
        try:
            # os.replace onto a read-only FILE succeeds (the directory
            # grants the rename), so pin the transaction the other way:
            # make the temp-file creation fail via a read-only DIRECTORY.
            workfile.parent.chmod(0o555)
            try:
                with pytest.raises((SprintError, OSError)):
                    self._add("5")
            finally:
                workfile.parent.chmod(0o755)
        finally:
            workfile.chmod(0o644)
        assert "epic-5" not in statuses(self.store, "sample")


class TestRetroBanScan:
    """The independent gate: raw-line scan, deliberately NOT the entry
    parser, with the on_complete gate's existence guard as a hard error."""

    def test_a_missing_file_is_a_loud_failure_not_a_pass(self, tmp_path):
        from claudomater.sprint import retro_ban_scan

        with pytest.raises(SprintError, match="never read"):
            retro_ban_scan(tmp_path / "nowhere.yaml")

    def test_an_injected_optional_is_caught_with_its_line_number(self, workfile):
        """The verifier must FAIL on an injected violation - a check that
        cannot fail is decoration (the 2026-08 convention lesson). The
        injection is raw bytes, not the writer API, which cannot express
        it."""
        from claudomater.sprint import retro_ban_scan

        raw = workfile.read_bytes().replace(
            b"epic-4-retrospective: fable-review-required",
            b"epic-4-retrospective: optional",
        )
        workfile.write_bytes(raw)
        violations, _ = retro_ban_scan(workfile)
        assert len(violations) == 2  # the injected one + the fixture's legacy line
        lines = {line_no for line_no, _ in violations}
        legacy_line = next(
            e.line_no
            for e in SprintDoc.read(workfile).entries
            if e.key == "epic-3-retrospective"
        )
        assert legacy_line in lines

    def test_the_fixture_legacy_line_is_the_only_violation_at_rest(self, workfile):
        from claudomater.sprint import retro_ban_scan

        violations, distribution = retro_ban_scan(workfile)
        assert [v for _, v in violations] == [
            "  epic-3-retrospective: optional  # LEGACY value, banned "
            "2026-02-14 in THIS FIXTURE'S invented timeline - preserved as "
            "audit trail"
        ]
        assert distribution["fable-review-required"] == 2  # epic-4 + project
        assert distribution["done"] == 2
        assert distribution["optional"] == 1

    def test_sub_epic_and_project_retros_are_in_scope(self, tmp_path):
        """The LZ gate's `epic-[0-9]+-retrospective` cannot match a
        sub-epic's retro (`[0-9]+` cannot span the inner hyphen) or the
        project-scoped line - this scan is deliberately wider, because a
        banned value on those lines is the same rot."""
        from claudomater.sprint import retro_ban_scan

        f = tmp_path / "s.yaml"
        f.write_text(
            "development_status:\n"
            "  epic-4-5: done\n"
            "  epic-4-5-retrospective: optional\n"
            "  project-retrospective: optional\n",
            encoding="utf-8",
        )
        violations, _ = retro_ban_scan(f)
        assert len(violations) == 2

    def test_creation_output_passes_the_gate_it_feeds(self, store, workfile):
        """add_epic's own output must satisfy the verifier that gates it -
        the two sides of deliverable 2 meeting in one test."""
        import_path(store, "sample", workfile)
        from claudomater.sprint import add_epic, retro_ban_scan

        add_epic(store, "sample", workfile, "5", stories=("5-1-a",))
        violations, distribution = retro_ban_scan(workfile)
        assert len(violations) == 1  # still only the fixture's legacy line
        assert distribution["fable-review-required"] == 3


class TestSprintCliSliceB:
    def _seeded(self, tmp_path, workfile):
        db = tmp_path / "learn.db"
        assert (
            main(
                [
                    "sprint",
                    "import",
                    str(workfile),
                    "--db",
                    str(db),
                    "--export-dir",
                    str(tmp_path / "exp"),
                    "--sprint-project",
                    "sample",
                ]
            )
            == EXIT_OK
        )
        return db

    @staticmethod
    def _dod_file(tmp_path):
        f = tmp_path / "epic-5-under-test.md"
        f.write_text("# Epic 5\n\n## Definition of Done\n\n- ships\n", encoding="utf-8")
        return f

    def test_add_epic_cli_creates_and_reports(self, tmp_path, workfile, capsys):
        db = self._seeded(tmp_path, workfile)
        rc = main(
            [
                "sprint",
                "add-epic",
                "5",
                str(workfile),
                "--story",
                "5-1-first",
                "--story",
                "5-2-second",
                "--db",
                str(db),
                "--export-dir",
                str(tmp_path / "exp"),
                "--sprint-project",
                "sample",
                "--epic-file",
                str(self._dod_file(tmp_path)),
            ]
        )
        out = capsys.readouterr().out
        assert rc == EXIT_OK
        assert "created epic-5 (4 line(s))" in out
        assert "epic-5-retrospective" in out

    def test_add_epic_cli_refusals_are_clean_errors(self, tmp_path, workfile, capsys):
        db = self._seeded(tmp_path, workfile)
        rc = main(
            [
                "sprint",
                "add-epic",
                "4",  # exists
                str(workfile),
                "--db",
                str(db),
                "--export-dir",
                str(tmp_path / "exp"),
                "--sprint-project",
                "sample",
                "--epic-file",
                str(self._dod_file(tmp_path)),
            ]
        )
        assert rc == EXIT_ERROR
        assert "already in the status map" in capsys.readouterr().err

    def test_check_retros_cli_flags_the_legacy_line(self, workfile, capsys):
        rc = main(["sprint", "check-retros", str(workfile)])
        captured = capsys.readouterr()
        assert rc == EXIT_ERROR
        assert "epic-3-retrospective: optional" in captured.out
        assert "FATAL" in captured.err

    def test_check_retros_cli_passes_a_clean_file_with_the_distribution(
        self, tmp_path, capsys
    ):
        f = tmp_path / "clean.yaml"
        f.write_text(
            "development_status:\n"
            "  epic-1: done\n"
            "  epic-1-retrospective: done\n"
            "  epic-2: backlog\n"
            "  epic-2-retrospective: fable-review-required\n",
            encoding="utf-8",
        )
        rc = main(["sprint", "check-retros", str(f)])
        out = capsys.readouterr().out
        assert rc == EXIT_OK
        assert "OK: no banned retrospective statuses" in out
        assert "1 done" in out and "1 fable-review-required" in out

    def test_check_retros_cli_fails_loudly_on_a_missing_file(self, tmp_path, capsys):
        rc = main(["sprint", "check-retros", str(tmp_path / "nope.yaml")])
        assert rc == EXIT_ERROR
        assert "never read" in capsys.readouterr().err


class TestParityRetroGateProof:
    """READ-ONLY against the real parity file, like TestParityAcceptanceProof:
    the wider scan (sub-epic + project retros included) must agree with
    the file's own hygiene rule. Measured at slice B build time: 46 retro
    lines, all in the legal write vocabulary."""

    @pytest.mark.skipif(
        not PARITY_SPRINT_STATUS.is_file(), reason="parity sprint file not configured (OMATER_PARITY_SPRINT_STATUS)"
    )
    def test_the_real_file_is_clean_under_the_wider_scan(self):
        from claudomater.sprint import retro_ban_scan

        violations, distribution = retro_ban_scan(PARITY_SPRINT_STATUS)
        assert violations == []
        assert set(distribution) <= {"fable-review-required", "done"}


class TestRetroBanScanRoundTwo:
    """Copilot round-1 findings on the gate itself."""

    def test_a_retro_line_with_no_status_token_fails_loudly(self, tmp_path):
        """`epic-1-retrospective:` (bare, or comment-only) is a line the
        gate cannot meaningfully evaluate - recording an empty-string
        status in the distribution read as CLEAN, the silent-pass shape
        this module refuses everywhere else."""
        from claudomater.sprint import retro_ban_scan

        for tail in ("", " ", " # comment only"):
            f = tmp_path / "bare.yaml"
            f.write_text(
                f"development_status:\n  epic-1-retrospective:{tail}\n",
                encoding="utf-8",
            )
            with pytest.raises(SprintError, match="no status token"):
                retro_ban_scan(f)

    def test_check_retros_prints_the_line_exactly_as_on_disk(
        self, workfile, capsys
    ):
        """The CLI stripped leading indentation off the violation line,
        hiding the exact on-disk content the operator is about to fix."""
        rc = main(["sprint", "check-retros", str(workfile)])
        assert rc == EXIT_ERROR
        out = capsys.readouterr().out
        assert "  epic-3-retrospective: optional" in out  # indent intact
        # the STRIPPED form (separator's single space glued straight onto
        # the key) must not appear - that's what hiding the indent looks like
        assert ": epic-3-retrospective" not in out


class TestRequireDod:
    """Epic-47 retro F8: an epic registers with a machine-checkable DoD or
    not at all."""

    def test_a_file_with_the_anchored_heading_passes(self, tmp_path):
        f = tmp_path / "epic-9-thing.md"
        f.write_text("# Epic 9\n\n## Definition of Done\n\n- [ ] ships\n")
        require_dod(f)  # no raise

    def test_a_numbered_or_decorated_heading_fails(self, tmp_path):
        """The epic-26 shape: `## 8 - Definition of Done` misses the
        anchor, which is exactly why it broke the waiver lookup."""
        f = tmp_path / "epic-9-thing.md"
        f.write_text("# Epic 9\n\n## 8 - Definition of Done\n")
        with pytest.raises(SprintError, match="no `## Definition of Done`"):
            require_dod(f)

    def test_a_missing_file_is_the_louder_failure(self, tmp_path):
        with pytest.raises(SprintError, match="cannot read epic file"):
            require_dod(tmp_path / "absent.md")

    def test_add_epic_refuses_without_a_dod(self, tmp_path):
        from claudomater.learnstore import LearnStore
        from claudomater.sprint import add_epic

        store = LearnStore.open(tmp_path / "l.db")
        try:
            work = tmp_path / "s.yaml"
            work.write_text(
                "development_status:\n  epic-1: done\n"
                "  epic-1-retrospective: done\n"
            )
            epic_file = tmp_path / "epic-2-x.md"
            epic_file.write_text("# Epic 2\n\nno dod here\n")
            with pytest.raises(SprintError, match="Definition of Done"):
                add_epic(store, "p", work, "2", epic_file=epic_file)
            # BEFORE any write: the file gained no block
            assert "epic-2" not in work.read_text()
        finally:
            store.close()

    def test_add_epic_registers_with_a_dod(self, tmp_path):
        from claudomater.learnstore import LearnStore
        from claudomater.sprint import add_epic

        store = LearnStore.open(tmp_path / "l.db")
        try:
            work = tmp_path / "s.yaml"
            work.write_text(
                "development_status:\n  epic-1: done\n"
                "  epic-1-retrospective: done\n"
            )
            epic_file = tmp_path / "epic-2-x.md"
            epic_file.write_text("# Epic 2\n\n## Definition of Done\n\n- x\n")
            keys = add_epic(store, "p", work, "2", epic_file=epic_file)
            assert keys[0] == "epic-2"
        finally:
            store.close()


class TestSetStoryFileStatus:
    """Epic-47 retro F1: the finish flow owns the story FILE's flip too."""

    STORY = "# Story 9-1\n\nStatus: review\n\n## Story\n\ntext\n"

    def test_flips_and_returns_previous(self, tmp_path):
        f = tmp_path / "9-1-x.md"
        f.write_text(self.STORY)
        assert set_story_file_status(f, "done") == "review"
        assert "Status: done\n" in f.read_text()
        # every other byte survives
        assert f.read_text() == self.STORY.replace("Status: review", "Status: done")

    def test_bold_status_lines_flip_too(self, tmp_path):
        f = tmp_path / "9-1-x.md"
        f.write_text("# S\n\n**Status:** review\n")
        assert set_story_file_status(f, "done") == "review"
        assert "**Status:** done" in f.read_text()

    def test_no_status_line_raises(self, tmp_path):
        f = tmp_path / "9-1-x.md"
        f.write_text("# S\n\nno status here\n")
        with pytest.raises(SprintError, match="no `Status:` line"):
            set_story_file_status(f, "done")

    def test_a_colonless_status_word_is_prose_not_the_marker(self, tmp_path):
        """Copilot round 5: 'Status review' (no colon) must read as prose -
        matching it would rewrite a sentence, or falsely refuse a healthy
        file on 'multiple Status lines'."""
        f = tmp_path / "9-1-x.md"
        f.write_text("# S\n\nStatus review\n")
        with pytest.raises(SprintError, match="no `Status:` line"):
            set_story_file_status(f, "done")

    def test_status_prose_beside_the_real_marker_is_ignored(self, tmp_path):
        f = tmp_path / "9-1-x.md"
        f.write_text("Status: review\n\nStatus review happened on Tuesday.\n")
        assert set_story_file_status(f, "done") == "review"
        text = f.read_text()
        assert "Status: done" in text
        assert "Status review happened on Tuesday." in text

    def test_bold_with_outside_colon_flips_too(self, tmp_path):
        f = tmp_path / "9-1-x.md"
        f.write_text("# S\n\n**Status**: review\n")
        assert set_story_file_status(f, "done") == "review"
        assert "**Status**: done" in f.read_text()

    def test_multiple_status_lines_refuse_to_guess(self, tmp_path):
        f = tmp_path / "9-1-x.md"
        f.write_text("Status: review\n\nStatus: done\n")
        with pytest.raises(SprintError, match="refusing to guess"):
            set_story_file_status(f, "done")

    def test_the_write_vocabulary_gates_the_flip(self, tmp_path):
        f = tmp_path / "9-1-x.md"
        f.write_text(self.STORY)
        with pytest.raises(SprintError):
            set_story_file_status(f, "finished")
