"""Completion-integrity gate (Phase 3 deliverable 4, epic-46 retro A5).

Unit pins run on synthetic story text. The acceptance replays run against
the REAL corpus - the story file as it stood AT THE DONE-FLIP COMMIT
(recovered from the artifacts repo's history) and the real merged file
sets - and are skipped on machines without the checkouts.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from claudomater.completion import (
    CompletionError,
    completion_report,
    file_list_paths,
    merged_files_of,
)

UI3 = Path(os.environ.get("OMATER_UI3_ROOT", Path.home() / "sourcecode/ui3"))
BMAD = UI3 / "_bmad-output"

requires_corpus = pytest.mark.skipif(
    not (BMAD / "implementation-artifacts").is_dir(),
    reason="ui3 + artifacts checkouts not present",
)

STORY = """\
# Story X-1

## Tasks / Subtasks

- [x] Task 1 - build it
  - [x] subtask done
- [x] Task 2 - test it

## Dev Agent Record

### File List

- `app/src/Widget.tsx` (modified)
- `app/src/Widget.test.tsx` (new)
- docs/note.md (modified)

## Change Log
"""

MERGED = ["app/src/Widget.tsx", "app/src/Widget.test.tsx", "docs/note.md"]


class TestTaskBoxes:
    def test_a_clean_story_passes(self):
        assert completion_report(STORY, MERGED).ok

    def test_an_unchecked_top_level_box_blocks(self):
        text = STORY.replace("- [x] Task 2", "- [ ] Task 2")
        report = completion_report(text, MERGED)
        assert not report.ok
        assert report.unchecked == ["Task 2 - test it"]
        assert any("unchecked task box" in p for p in report.problems)

    def test_an_unchecked_nested_box_blocks_too(self):
        """The evidence of record's unexecuted work lived in INDENTED
        sub-items - a top-level-only scan would have read it as done."""
        text = STORY.replace("  - [x] subtask done", "  - [ ] subtask done")
        report = completion_report(text, MERGED)
        assert not report.ok
        assert report.unchecked == ["subtask done"]

    def test_uppercase_x_counts_as_checked(self):
        text = STORY.replace("- [x] Task 2", "- [X] Task 2")
        assert completion_report(text, MERGED).ok

    def test_star_bullets_are_boxes_too(self):
        text = STORY.replace("- [x] Task 2", "* [ ] Task 2")
        assert not completion_report(text, MERGED).ok

    def test_a_missing_tasks_section_blocks(self):
        """Unseen boxes must not read as ticked."""
        text = STORY.replace("## Tasks / Subtasks", "## Notes")
        report = completion_report(text, MERGED)
        assert any("cannot see the task boxes" in p for p in report.problems)

    def test_boxes_outside_the_tasks_section_are_not_judged(self):
        """An unchecked box in, say, a QA-notes section is not a task."""
        text = STORY + "\n## QA Notes\n\n- [ ] optional follow-up idea\n"
        assert completion_report(text, MERGED).ok


class TestFileListBlade:
    def test_annotations_and_backticks_parse(self):
        section = (
            "\n- `a/b.py` (new)\n- c/d.md (modified)\n- `e/f.ts`\n\nprose\n"
        )
        assert file_list_paths(section) == ["a/b.py", "c/d.md", "e/f.ts"]

    def test_a_merged_file_the_list_omits_is_named(self):
        report = completion_report(STORY, MERGED + ["app/src/new-thing.ts"])
        assert not report.ok
        assert report.missing_from_list == ["app/src/new-thing.ts"]

    def test_a_listed_file_the_merge_lacks_is_named(self):
        """Narration never satisfies the gate - a File List claiming a
        file the merge does not carry is the narration-vs-reality gap."""
        report = completion_report(STORY, MERGED[:-1])
        assert not report.ok
        assert report.phantom_in_list == ["docs/note.md"]

    def test_a_missing_file_list_blocks_by_default(self):
        """'No list' and 'list agrees' must never read the same."""
        text = STORY.replace("### File List", "### Files I Touched")
        report = completion_report(text, MERGED)
        assert any("no `### File List`" in p for p in report.problems)

    def test_the_opt_out_is_explicit(self):
        text = STORY.replace("### File List", "### Files I Touched")
        assert completion_report(text, MERGED, require_file_list=False).ok

    def test_exempt_prefixes_cover_driver_owned_artifacts_on_both_sides(self):
        text = STORY.replace(
            "- docs/note.md (modified)",
            "- docs/note.md (modified)\n- `_bmad-output/implementation-artifacts/x-1.md` (modified)",
        )
        merged = MERGED + ["_bmad-output/other.md"]
        report = completion_report(text, merged, exempt=["_bmad-output"])
        assert report.ok

    def test_a_malformed_entry_raises(self):
        text = STORY.replace("- docs/note.md (modified)", "- (modified)")
        with pytest.raises(CompletionError, match="malformed File List"):
            completion_report(text, MERGED)

    def test_an_empty_changeset_raises(self):
        with pytest.raises(CompletionError, match="no merged files"):
            completion_report(STORY, ["  ", ""])


class TestMergedFilesOf:
    def test_reads_the_commits_file_set(self, tmp_path):
        repo = tmp_path / "r"
        repo.mkdir()
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}

        def git(*args):
            subprocess.run(
                ["git", *args], cwd=repo, env=env, check=True, capture_output=True
            )

        git("init", "-q")
        git("config", "user.email", "t@example.invalid")
        git("config", "user.name", "T")
        (repo / "a.txt").write_text("x\n")
        (repo / "b.txt").write_text("y\n")
        git("add", "-A")
        git("commit", "-qm", "two files")
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, env=env,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert merged_files_of(repo, sha) == ["a.txt", "b.txt"]

    def test_a_bad_sha_raises(self, tmp_path):
        repo = tmp_path / "r"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        with pytest.raises(CompletionError, match="git show"):
            merged_files_of(repo, "deadbeef")


@requires_corpus
class TestRealCorpusReplays:
    """The acceptance proofs, against the real artifacts repo's history.
    Nothing is copied into this repo; the RED corpus is recovered live
    from the commit at which the story flipped done."""

    def _story_at(self, ref: str, name: str) -> str:
        return subprocess.run(
            ["git", "show", f"{ref}:implementation-artifacts/{name}"],
            cwd=BMAD,
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    def test_red_the_done_flip_that_should_have_been_blocked(self):
        """Story 46-7 at ui3-bmad 1dbdf53: flipped done with Tasks 6/7/8
        unchecked and their sub-items unexecuted. The gate must block it -
        this is deliverable 4's reason to exist."""
        story = self._story_at(
            "1dbdf53", "46-7-testyml-least-privilege-and-csp-doc.md"
        )
        merged = merged_files_of(UI3, "5b26c746")
        report = completion_report(story, merged)
        assert not report.ok
        assert len(report.unchecked) >= 3  # 3 top-level tasks + sub-items
        # its File List, notably, was accurate all along - the boxes blade
        # is what was missing
        assert report.missing_from_list == []
        assert report.phantom_in_list == []

    def test_green_the_repaired_story_passes(self):
        """The same story as repaired at epic close (boxes ticked with
        confessions, File List matching the merge exactly)."""
        story = self._story_at(
            "HEAD", "46-7-testyml-least-privilege-and-csp-doc.md"
        )
        merged = merged_files_of(UI3, "5b26c746")
        assert completion_report(story, merged).ok

    def test_34_36_passes_boxes_but_its_missing_file_list_is_flagged(self):
        """Measured during this slice: story 34-36 shipped with every box
        ticked and NO File List section at all (its dev record is prose).
        The boxes blade passes; the default gate flags the absent list -
        'no list' must not read as 'list agrees'."""
        story = self._story_at(
            "HEAD", "34-36-timeseries-chart-click-semantics.md"
        )
        merged = merged_files_of(UI3, "a5105e31")
        report = completion_report(story, merged)
        assert report.unchecked == []
        assert any("File List" in p for p in report.problems)
        assert completion_report(
            story, merged, require_file_list=False
        ).ok
