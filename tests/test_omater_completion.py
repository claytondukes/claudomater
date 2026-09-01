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
    _completion_report,
    completion_report,
    file_list_paths,
    merged_files_of,
    normalize_exempt,
    run_completion_gate,
)

PARITY = Path(os.environ.get("OMATER_PARITY_ROOT") or "/nonexistent")
BMAD = PARITY / "_bmad-output"

requires_corpus = pytest.mark.skipif(
    not (BMAD / "implementation-artifacts").is_dir(),
    reason="parity + artifacts checkouts not configured (OMATER_PARITY_ROOT)",
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
        # exercises the MODULE-PRIVATE seam on purpose: production code
        # reaches exemptions only through run_completion_gate (F3), and a
        # separate test greps the source tree to prove it
        text = STORY.replace(
            "- docs/note.md (modified)",
            "- docs/note.md (modified)\n- `_bmad-output/implementation-artifacts/x-1.md` (modified)",
        )
        merged = MERGED + ["_bmad-output/other.md"]
        report = _completion_report(text, merged, exempt=["_bmad-output"])
        assert report.ok

    def test_the_public_report_seam_has_no_exempt_parameter(self):
        import inspect

        assert "exempt" not in inspect.signature(completion_report).parameters
        assert "exempt" not in inspect.signature(run_completion_gate).parameters

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
        """Story 46-7 at artifact-repo 1dbdf53: flipped done with Tasks 6/7/8
        unchecked and their sub-items unexecuted. The gate must block it -
        this is deliverable 4's reason to exist."""
        story = self._story_at(
            "1dbdf534c5c370654893a4bffaa909cf900eef7b", "46-7-testyml-least-privilege-and-csp-doc.md"
        )
        merged = merged_files_of(PARITY, "5b26c746f7153b6209610dfdd36d34d44f260e0b")
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
        merged = merged_files_of(PARITY, "5b26c746f7153b6209610dfdd36d34d44f260e0b")
        assert completion_report(story, merged).ok

    def test_34_36_passes_boxes_but_its_missing_file_list_is_flagged(self):
        """Measured during this slice: story 34-36 shipped with every box
        ticked and NO File List section at all (its dev record is prose).
        The boxes blade passes; the default gate flags the absent list -
        'no list' must not read as 'list agrees'."""
        story = self._story_at(
            "HEAD", "34-36-timeseries-chart-click-semantics.md"
        )
        merged = merged_files_of(PARITY, "a5105e31abd06adfdcd5801fa6062d86052b13f5")
        report = completion_report(story, merged)
        assert report.unchecked == []
        assert any("File List" in p for p in report.problems)
        assert completion_report(
            story, merged, require_file_list=False
        ).ok


class TestRoundTwoPins:
    def test_a_horizontal_rule_is_not_a_bullet(self):
        """Copilot round-2: '---' matched startswith('-') and raised a
        false malformed-entry error; a bullet is dash/star + whitespace."""
        text = STORY.replace(
            "### File List\n", "### File List\n\n---\n"
        )
        assert completion_report(text, MERGED).ok

    def test_a_bare_unchecked_box_with_no_label_still_blocks(self):
        """Copilot round-3: `- [ ]` with no trailing text slipped the \\s+
        in the regex - ANY unchecked box blocks, label or not."""
        text = STORY.replace("- [x] Task 2 - test it", "- [ ]")
        report = completion_report(text, MERGED)
        assert not report.ok
        assert len(report.unchecked) == 1


class _FakeRunLog:
    def __init__(self):
        self.events = []

    def event(self, scope, kind, detail=None, **kw):
        self.events.append({"scope": scope, "kind": kind, "detail": detail or {}})


def _synthetic_repo(tmp_path, files):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}

    def git(*args):
        subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "t@example.invalid")
    git("config", "user.name", "T")
    for rel, content in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    git("add", "-A")
    git("commit", "-qm", "merge")
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, env=env,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return repo, sha


class _CfgWithExempt:
    def __init__(self, exempt):
        self.completion_exempt = tuple(exempt)


CROSS_REPO_STORY = STORY.replace(
    "- docs/note.md (modified)",
    "- docs/note.md (modified)\n"
    "- `_bmad-output/implementation-artifacts/x-1.md` (modified)",
)


class TestRunCompletionGateIsConfigured:
    """Retirement condition 1 (epic-47 retro F3): the acceptance pair -
    the SAME story with a cross-repo File List entry FAILS under an empty
    config exempt and PASSES under the config's `_bmad-output` entry, and
    the invocation logs the exempt list it actually used."""

    def _arrange(self, tmp_path):
        repo, sha = _synthetic_repo(
            tmp_path,
            {
                "app/src/Widget.tsx": "w\n",
                "app/src/Widget.test.tsx": "t\n",
                "docs/note.md": "n\n",
            },
        )
        (repo / "story.md").write_text(CROSS_REPO_STORY)
        return repo, sha

    def test_red_without_the_config_exempt_the_cross_repo_entry_blocks(self, tmp_path):
        repo, sha = self._arrange(tmp_path)
        log = _FakeRunLog()
        report = run_completion_gate(repo, _CfgWithExempt([]), "story.md", sha, log)
        assert not report.ok
        assert report.phantom_in_list == [
            "_bmad-output/implementation-artifacts/x-1.md"
        ]

    def test_green_the_config_exempt_admits_it(self, tmp_path):
        repo, sha = self._arrange(tmp_path)
        log = _FakeRunLog()
        report = run_completion_gate(
            repo, _CfgWithExempt(["_bmad-output"]), "story.md", sha, log
        )
        assert report.ok

    def test_the_invocation_logs_inputs_exempt_and_verdict(self, tmp_path):
        repo, sha = self._arrange(tmp_path)
        log = _FakeRunLog()
        run_completion_gate(repo, _CfgWithExempt(["_bmad-output"]), "story.md", sha, log)
        (ev,) = log.events
        assert (ev["scope"], ev["kind"]) == ("gate", "completion-gate")
        d = ev["detail"]
        assert d["exempt"] == ["_bmad-output"]
        assert d["merge_sha"] == sha
        assert d["ok"] is True
        assert d["merged_files"] == 3
        assert d["story_file"].endswith("story.md")

    def test_an_unreadable_story_file_raises(self, tmp_path):
        repo, sha = self._arrange(tmp_path)
        with pytest.raises(CompletionError, match="cannot read story file"):
            run_completion_gate(repo, _CfgWithExempt([]), "absent.md", sha, _FakeRunLog())


class TestExemptGrammar:
    def test_none_is_empty(self):
        assert normalize_exempt(None) == ()

    def test_entries_normalize_to_prefixes(self):
        assert normalize_exempt(["_bmad-output/", "./docs/x/"]) == (
            "_bmad-output",
            "docs/x",
        )

    @pytest.mark.parametrize(
        "bad",
        ["", "   ", "/abs", "~home", "a/../b", "a\\b", "./", ".", "a//b", "a/./b"],
    )
    def test_dangerous_shapes_are_refused(self, bad):
        with pytest.raises(CompletionError):
            normalize_exempt([bad])

    def test_non_list_is_refused(self):
        with pytest.raises(CompletionError, match="must be a list"):
            normalize_exempt("_bmad-output")

    def test_exempt_is_keyword_only_on_the_private_seam(self):
        """A positional third argument would bypass the exempt= grep pin."""
        with pytest.raises(TypeError):
            _completion_report(STORY, MERGED, ["_bmad-output"])



class TestNoCallSitePassesExempt:
    def test_grep_the_source_tree(self):
        """The acceptance grep, as a pinned test: outside completion.py
        itself, NO production module passes `exempt=` - config is the only
        path to an exemption. (Tests exercise the private seam on purpose;
        this scans src/ only.)"""
        import re

        # \b so the config field kwarg `completion_exempt=` (a different
        # name entirely) does not read as the gate parameter; \s* because
        # `exempt = x` is valid Python for a kwarg too (and a production
        # module ASSIGNING a local named `exempt` is the same ad-hoc
        # exemption-handling this test exists to forbid)
        pattern = re.compile(r"\bexempt\s*=")
        src = Path(__file__).resolve().parents[1] / "src" / "claudomater"
        offenders = []
        for py in sorted(src.glob("*.py")):
            if py.name == "completion.py":
                continue
            for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f"{py.name}:{i}: {line.strip()}")
        assert offenders == []


class TestGateCfgShapeIsTyped:
    def test_a_cfg_without_the_field_is_a_typed_error(self, tmp_path):
        class Wrong: ...
        with pytest.raises(CompletionError, match="no completion_exempt"):
            run_completion_gate(tmp_path, Wrong(), "s.md", "sha", _FakeRunLog())

    def test_a_non_sequence_exempt_is_a_typed_error(self, tmp_path):
        bad_cfg = type("C", (), {"completion_exempt": "x"})()
        with pytest.raises(CompletionError, match="sequence of strings"):
            run_completion_gate(tmp_path, bad_cfg, "s.md", "sha", _FakeRunLog())
