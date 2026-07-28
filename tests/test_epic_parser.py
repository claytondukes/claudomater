"""Regression tests for epic-scoped parsing (Story 1.1 postmortem, 2026-07-14).

Run 2 of the first Acme orchestration recorded "Epic name: Acme - Epic
Breakdown" (the document H1) and "Story count: 91" (all stories in all
epics) because `parse_epic_file` was called unscoped against a multi-epic
epics.md. Scoped parsing must return the selected epic's `## Epic N:`
heading title and only that epic's stories.
"""

from story_automator.core.epic_parser import epic_complete, parse_epic_file

MULTI_EPIC = """\
# Acme - Epic Breakdown

Intro prose about the whole document.

## Epic 1: Trustworthy Event Capture & Delivery

### Story 1.1: Cargo workspace, crate DAG & CI foundation

Some description.

### Story 1.2: Signed release builds

More description.

## Epic 2: Query & Storage

### Story 2.1: Storage engine skeleton

Description.

### Story 2.2: Query planner

Description.

### Story 2.3: Retention policies

Description.
"""

SINGLE_EPIC_NO_HEADING = """\
# Epic 7: Lone Epic Title

### Story 7.1: Only story

Description.
"""


class TestParseEpicFileUnscoped:
    def test_unscoped_returns_document_title_and_all_stories(self, tmp_path):
        epic_file = tmp_path / "epics.md"
        epic_file.write_text(MULTI_EPIC, encoding="utf-8")
        result = parse_epic_file(epic_file)
        assert result["epicTitle"] == "Acme - Epic Breakdown"
        assert result["count"] == 5
        assert "epicNum" not in result


class TestParseEpicFileScoped:
    def test_epic_1_returns_its_heading_title_and_only_its_stories(self, tmp_path):
        epic_file = tmp_path / "epics.md"
        epic_file.write_text(MULTI_EPIC, encoding="utf-8")
        result = parse_epic_file(epic_file, epic_num="1")
        assert result["epicTitle"] == "Trustworthy Event Capture & Delivery"
        assert result["epicNum"] == "1"
        assert result["count"] == 2
        assert [story["storyId"] for story in result["stories"]] == ["1.1", "1.2"]
        assert all(story["epicTitle"] == "Trustworthy Event Capture & Delivery" for story in result["stories"])

    def test_epic_2_returns_its_heading_title_and_only_its_stories(self, tmp_path):
        epic_file = tmp_path / "epics.md"
        epic_file.write_text(MULTI_EPIC, encoding="utf-8")
        result = parse_epic_file(epic_file, epic_num="2")
        assert result["epicTitle"] == "Query & Storage"
        assert result["count"] == 3
        assert [story["storyId"] for story in result["stories"]] == ["2.1", "2.2", "2.3"]

    def test_scoped_missing_epic_returns_empty(self, tmp_path):
        epic_file = tmp_path / "epics.md"
        epic_file.write_text(MULTI_EPIC, encoding="utf-8")
        result = parse_epic_file(epic_file, epic_num="9")
        assert result["count"] == 0
        assert result["stories"] == []

    def test_single_epic_file_without_epic_heading_falls_back_to_h1(self, tmp_path):
        epic_file = tmp_path / "epic-7.md"
        epic_file.write_text(SINGLE_EPIC_NO_HEADING, encoding="utf-8")
        result = parse_epic_file(epic_file, epic_num="7")
        assert result["epicTitle"] == "Epic 7: Lone Epic Title"
        assert result["count"] == 1
        assert result["stories"][0]["storyId"] == "7.1"


class TestEpicComplete:
    """epic_complete must judge completion within the range's epic, not the file.

    Postmortem follow-up (2026-07-14): the whole-file max meant epic 1's
    final story compared against the last epic's last story and never
    reported complete on a multi-epic file.
    """

    def _write(self, tmp_path):
        epic_file = tmp_path / "epics.md"
        epic_file.write_text(MULTI_EPIC, encoding="utf-8")
        return epic_file

    def test_full_epic_range_is_complete_despite_later_epics(self, tmp_path):
        result = epic_complete(self._write(tmp_path), "1.1,1.2")
        assert result["epicComplete"] is True
        assert result["maxEpicStory"] == "1.2"
        assert result["epicNum"] == "1"

    def test_partial_range_is_incomplete(self, tmp_path):
        result = epic_complete(self._write(tmp_path), "1.1")
        assert result["epicComplete"] is False
        assert result["maxEpicStory"] == "1.2"

    def test_explicit_epic_num_overrides_range_epic(self, tmp_path):
        result = epic_complete(self._write(tmp_path), "1.1,1.2", epic_num="2")
        assert result["epicComplete"] is False
        assert result["maxEpicStory"] == "2.3"
        assert result["epicNum"] == "2"

    def test_separator_mismatch_still_matches(self, tmp_path):
        result = epic_complete(self._write(tmp_path), "1-1,1-2")
        assert result["epicComplete"] is True

    def test_empty_range_keeps_whole_file_backcompat(self, tmp_path):
        result = epic_complete(self._write(tmp_path), "")
        assert result["epicComplete"] is False
        assert result["maxEpicStory"] == "2.3"
        assert "epicNum" not in result

    def test_unknown_epic_raises_no_stories_found(self, tmp_path):
        import pytest

        with pytest.raises(ValueError, match="no_stories_found"):
            epic_complete(self._write(tmp_path), "4.1")
