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
    def __init__(self, exempt, lab_record="off"):
        self.completion_exempt = tuple(exempt)
        self.completion_lab_record = lab_record


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
        bad_cfg = type("C", (), {"completion_exempt": "x", "completion_lab_record": "off"})()
        with pytest.raises(CompletionError, match="sequence of strings"):
            run_completion_gate(tmp_path, bad_cfg, "s.md", "sha", _FakeRunLog())


LAB_RECORD_OK = """\
### Merge and lab record (operator, 2026-09-30)

- Merged as `abcd1234`.
- Lab: the interface arm INDUCED for real and restored; STATED, not
  induced: the port arm (a port change takes the shared lab down).
"""
LAB_RECORD_NO_DISPOSITION = """\
### Merge and lab record (operator, 2026-09-30)

- Merged as `abcd1234`.
- Lab: 29/29 on the first run, every arm exact-wire.
"""
LAB_RECORD_EVERY_ARM = """\
### Merge and lab record (operator, 2026-09-30)

- Lab: 18/18; every arm induced - every arm is a click.
"""


class TestLabRecordBlade:
    """Issue #31 (epic-64 A3, epic-65 A4, epic-66 A6, epic-63 F5): the
    finish refuses a story whose post-merge record never says, in the
    literal words, what the lab arms induced and what they did not."""

    # STORY already carries a File List; the record goes in front of it and the
    # merged set is the three files that list names.
    MERGED = ["app/src/Widget.tsx", "app/src/Widget.test.tsx", "docs/note.md"]

    def _story(self, record):
        return STORY.replace("### File List", record + "\n### File List", 1)

    def test_off_never_judges_the_record(self):
        report = completion_report(self._story(LAB_RECORD_NO_DISPOSITION), self.MERGED)
        assert report.lab_record_missing == []

    def test_required_passes_a_record_with_both_words(self):
        from claudomater.completion import _completion_report

        report = _completion_report(self._story(LAB_RECORD_OK), self.MERGED, lab_record="required")
        assert report.lab_record_missing == [] and report.ok

    def test_required_accepts_every_arm_induced(self):
        from claudomater.completion import _completion_report

        report = _completion_report(self._story(LAB_RECORD_EVERY_ARM), self.MERGED, lab_record="required")
        assert report.lab_record_missing == [] and report.ok

    def test_required_fails_a_record_without_the_disposition(self):
        from claudomater.completion import _completion_report

        report = _completion_report(self._story(LAB_RECORD_NO_DISPOSITION), self.MERGED, lab_record="required")
        # "every arm exact-wire" names arms without a disposition, so it is an
        # undisposed arm entry on top of the two missing words
        assert report.lab_record_missing == ["arm:2", "induced", "not-induced"]
        assert not report.ok and any("lacks the literal induced" in p for p in report.problems)

    def test_a_heading_with_trailing_words_is_not_the_record(self):
        from claudomater.completion import _completion_report

        template = (
            "### Merge and lab record instructions\n\n"
            "- say per arm whether it was induced or not induced; STATED for non-runtime arms.\n"
        )
        report = _completion_report(self._story(template), self.MERGED, lab_record="required")
        assert report.lab_record_missing == ["section"] and not report.ok
        # the documented annotation form is still the record
        titled = LAB_RECORD_OK.replace("(operator, 2026-09-30)", "(operator, 2026-10-01, re-run)")
        assert _completion_report(self._story(titled), self.MERGED, lab_record="required").ok

    def test_required_fails_when_the_section_is_absent(self):
        from claudomater.completion import _completion_report

        report = _completion_report(STORY, self.MERGED, lab_record="required")
        assert report.lab_record_missing == ["section"] and not report.ok

    def test_each_arm_entry_is_judged_and_the_undisposed_one_is_named(self):
        from claudomater.completion import _completion_report

        record = """\
### Merge and lab record (operator, 2026-09-30)

- ADMIN arm: induced through the real dialog.
- PORT arm: not induced (a port change takes the shared lab down).
- ERROR arm: the transport error, covered by the pins
  (`useServerTimeZone.test.tsx`).
- Cleanup asserted 204.
"""
        report = _completion_report(self._story(record), self.MERGED, lab_record="required")
        # the third bullet is the undisposed arm; the report carries its position, never its text
        assert report.lab_record_missing == ["arm:3"]
        assert not report.ok and any("bullet item(s) 3 of 4" in p for p in report.problems)
        joined = " ".join(report.problems) + " ".join(report.lab_record_missing)
        assert "transport error" not in joined and "useServerTimeZone" not in joined
        assert not any("lacks the literal" in p for p in report.problems)

    def test_story_text_never_reaches_the_report(self):
        from claudomater.completion import _completion_report

        record = (
            "### Merge and lab record (operator, 2026-09-30)\n\n"
            "- TOKEN arm: the probe used sk-live-EXAMPLE-SECRET-0000 and saw 200.\n"
        )
        report = _completion_report(self._story(record), self.MERGED, lab_record="required")
        assert report.lab_record_missing[0] == "arm:1"
        assert "EXAMPLE-SECRET" not in (" ".join(report.problems) + " ".join(report.lab_record_missing))

    def test_a_negative_only_record_fails_the_positive_side(self):
        from claudomater.completion import _completion_report

        record = "### Merge and lab record (operator, 2026-09-30)\n\n- not induced: the port arm.\n"
        report = _completion_report(self._story(record), self.MERGED, lab_record="required")
        assert report.lab_record_missing == ["induced"] and not report.ok

    def test_a_wrapped_negative_only_record_fails_the_positive_side(self):
        from claudomater.completion import _completion_report

        # markdown wraps the phrase: "not" ends one line, "induced" starts the next
        record = (
            "### Merge and lab record (operator, 2026-09-30)\n\n"
            "- The port arm was not\n  induced (a port change takes the shared lab down);\n"
            "  STATED.\n"
        )
        report = _completion_report(self._story(record), self.MERGED, lab_record="required")
        assert report.lab_record_missing == ["induced"] and not report.ok

    def test_never_induced_with_stated_is_negative_only(self):
        from claudomater.completion import _completion_report

        record = "### Merge and lab record (operator, 2026-09-30)\n\n- ADMIN arm: never induced; STATED.\n"
        report = _completion_report(self._story(record), self.MERGED, lab_record="required")
        assert report.lab_record_missing == ["induced"] and not report.ok

    def test_subject_separated_negations_are_negative(self):
        from claudomater.completion import _completion_report

        for line in (
            "- No arm was induced; STATED.",
            "- none of the arms were induced; STATED.",
            "- the port arm was not induced (a port change takes the lab down); STATED.",
            "- neither arm could be induced on the shared lab; STATED.",
        ):
            record = "### Merge and lab record (operator, 2026-09-30)\n\n" + line + "\n"
            report = _completion_report(self._story(record), self.MERGED, lab_record="required")
            assert report.lab_record_missing == ["induced"], line
            assert not report.ok

    def test_coordinated_clauses_scope_their_own_negation(self):
        from claudomater.completion import _completion_report

        for line in (
            "- The port arm was not induced but the interface arm was induced.",
            "- The port arm was not induced and the interface arm was induced for real.",
            "- No arm was skipped, the interface arm was induced while the port arm was not induced.",
        ):
            record = "### Merge and lab record (operator, 2026-09-30)\n\n" + line + "\n"
            report = _completion_report(self._story(record), self.MERGED, lab_record="required")
            assert report.ok, (line, report.problems)
        # and a coordinated all-negative line stays negative-only
        record = ("### Merge and lab record (operator, 2026-09-30)\n\n"
                  "- The port arm was not induced and the TLS arm was never induced; STATED.\n")
        report = _completion_report(self._story(record), self.MERGED, lab_record="required")
        assert report.lab_record_missing == ["induced"]

    def test_a_negated_coordination_without_a_new_subject_stays_negative(self):
        from claudomater.completion import _completion_report

        for line in (
            "- No arm was exercised and induced; STATED.",
            "- The port arm was not exercised and induced; STATED.",
            "- The port arm was never restarted and induced, STATED.",
        ):
            record = "### Merge and lab record (operator, 2026-09-30)\n\n" + line + "\n"
            report = _completion_report(self._story(record), self.MERGED, lab_record="required")
            assert report.lab_record_missing == ["induced"] and not report.ok, line
        # a conjunction that continues the SAME arm keeps a positive positive
        record = ("### Merge and lab record (operator, 2026-09-30)\n\n"
                  "- The interface arm was induced and restored; the port arm was not induced.\n")
        assert _completion_report(self._story(record), self.MERGED, lab_record="required").ok

    def test_plus_sign_bullets_are_arm_items_too(self):
        from claudomater.completion import _completion_report

        # `+` is a Markdown bullet marker: an undisposed arm under it must not
        # slip past the per-item check while the section-wide words are met
        record = ("### Merge and lab record (operator, 2026-09-30)\n\n"
                  "+ ADMIN arm: induced through the real dialog.\n"
                  "+ PORT arm: evidence pending.\n"
                  "+ FALLBACK: not induced.\n")
        report = _completion_report(self._story(record), self.MERGED, lab_record="required")
        assert report.lab_record_missing == ["arm:2"] and not report.ok

    def test_a_negated_comma_predicate_list_stays_negative(self):
        from claudomater.completion import _completion_report

        record = "### Merge and lab record (operator, 2026-09-30)\n\n- No arm was exercised, induced, or restored; STATED.\n"
        report = _completion_report(self._story(record), self.MERGED, lab_record="required")
        assert report.lab_record_missing == ["induced"] and not report.ok
        # a comma that introduces another arm still ends the clause
        record = ("### Merge and lab record (operator, 2026-09-30)\n\n"
                  "- The port arm was not induced, the interface arm was induced for real.\n")
        assert _completion_report(self._story(record), self.MERGED, lab_record="required").ok
        # and the no-runtime-arm assertion may carry an aside after a comma
        record = "### Merge and lab record (operator, 2026-09-30)\n\n- No runtime arm exists, the merge touches docs only.\n"
        assert _completion_report(self._story(record), self.MERGED, lab_record="required").ok

    def test_a_fenced_example_record_is_not_the_record(self):
        from claudomater.completion import _completion_report

        fenced = ("```markdown\n### Merge and lab record (operator, 2026-09-30)\n\n"
                  "- ADMIN arm: induced through the real dialog.\n- PORT arm: not induced; STATED.\n```\n")
        report = _completion_report(self._story(fenced), self.MERGED, lab_record="required")
        assert report.lab_record_missing == ["section"] and not report.ok
        # the same words inside a fence within the real section are not its vocabulary
        record = ("### Merge and lab record (operator, 2026-09-30)\n\n"
                  "The record follows the template:\n\n"
                  "~~~\n- ADMIN arm: induced.\n- PORT arm: not induced; STATED.\n~~~\n")
        report = _completion_report(self._story(record), self.MERGED, lab_record="required")
        assert report.lab_record_missing == ["induced", "not-induced"] and not report.ok
        # an unterminated fence runs to the end of the text
        record = ("### Merge and lab record (operator, 2026-09-30)\n\n"
                  "```\n- ADMIN arm: induced.\n- PORT arm: not induced; STATED.\n")
        report = _completion_report(self._story(record), self.MERGED, lab_record="required")
        assert "induced" in report.lab_record_missing and not report.ok

    def test_object_position_negation_is_negative(self):
        from claudomater.completion import _completion_report

        for line in (
            "- The probe induced no arm; STATED.",
            "- The lab induced none of the arms; STATED.",
            "- The operator induced neither arm (shared lab); STATED.",
        ):
            record = "### Merge and lab record (operator, 2026-09-30)\n\n" + line + "\n"
            report = _completion_report(self._story(record), self.MERGED, lab_record="required")
            assert report.lab_record_missing == ["induced"] and not report.ok, line
        record = ("### Merge and lab record (operator, 2026-09-30)\n\n"
                  "- The probe induced the port arm for real; the TLS arm was not induced.\n")
        assert _completion_report(self._story(record), self.MERGED, lab_record="required").ok

    def test_no_not_induced_arm_as_a_subject_is_not_the_exception(self):
        from claudomater.completion import _completion_report

        record = "### Merge and lab record (operator, 2026-09-30)\n\n- No not-induced arm was exercised.\n"
        report = _completion_report(self._story(record), self.MERGED, lab_record="required")
        assert "induced" in report.lab_record_missing and not report.ok
        for line in ("- No not-induced arm remains.", "- Lab 12/12: no not-induced arm (every arm ran for real)."):
            record = "### Merge and lab record (operator, 2026-09-30)\n\n" + line + "\n"
            report = _completion_report(self._story(record), self.MERGED, lab_record="required")
            assert report.ok, (line, report.problems)

    def test_a_longer_fence_closes_only_on_its_own_length(self):
        from claudomater.completion import _completion_report

        fenced = ("````markdown\n### Merge and lab record (operator, 2026-09-30)\n\n"
                  "```\n- ADMIN arm: induced through the real dialog.\n- PORT arm: not induced; STATED.\n"
                  "````\n")
        report = _completion_report(self._story(fenced), self.MERGED, lab_record="required")
        assert report.lab_record_missing == ["section"] and not report.ok
        # a closing fence with trailing text does not close either
        fenced = ("```\n```markdown\n### Merge and lab record (operator, 2026-09-30)\n\n"
                  "- ADMIN arm: induced.\n- PORT arm: not induced; STATED.\n```\n")
        report = _completion_report(self._story(fenced), self.MERGED, lab_record="required")
        assert report.lab_record_missing == ["section"] and not report.ok

    def test_a_commented_out_record_is_not_the_record(self):
        from claudomater.completion import _completion_report

        hidden = ("<!--\n### Merge and lab record (operator, 2026-09-30)\n\n"
                  "- ADMIN arm: induced through the real dialog.\n- PORT arm: not induced; STATED.\n-->\n")
        report = _completion_report(self._story(hidden), self.MERGED, lab_record="required")
        assert report.lab_record_missing == ["section"] and not report.ok
        # an unterminated comment hides everything after it
        hidden = ("<!-- template follows\n### Merge and lab record (operator, 2026-09-30)\n\n"
                  "- ADMIN arm: induced.\n- PORT arm: not induced; STATED.\n")
        report = _completion_report(self._story(hidden), self.MERGED, lab_record="required")
        assert report.lab_record_missing == ["section"] and not report.ok
        # a comment inside the real section hides its words from the vocabulary
        record = ("### Merge and lab record (operator, 2026-09-30)\n\n"
                  "<!-- - ADMIN arm: induced. -->\n- PORT arm: not induced; STATED.\n")
        report = _completion_report(self._story(record), self.MERGED, lab_record="required")
        assert report.lab_record_missing == ["induced"] and not report.ok

    def test_ordered_list_items_are_arm_items_too(self):
        from claudomater.completion import _completion_report

        for marker in ("1.", "1)"):
            second = marker.replace("1", "2"); third = marker.replace("1", "3")
            record = ("### Merge and lab record (operator, 2026-09-30)\n\n"
                      f"{marker} ADMIN arm: induced through the real dialog.\n"
                      f"{second} PORT arm: evidence pending.\n"
                      f"{third} FALLBACK arm: not induced; STATED.\n")
            report = _completion_report(self._story(record), self.MERGED, lab_record="required")
            assert report.lab_record_missing == ["arm:2"] and not report.ok, marker
        record = ("### Merge and lab record (operator, 2026-09-30)\n\n"
                  "1) No runtime arm (the merge touches docs only).\n")
        assert _completion_report(self._story(record), self.MERGED, lab_record="required").ok

    def test_a_transitive_every_arm_sentence_is_not_the_exception(self):
        from claudomater.completion import _completion_report

        for line in (
            "- Every arm induced an error in the harness.",
            "- All arms induced a restart of the web server.",
        ):
            record = "### Merge and lab record (operator, 2026-09-30)\n\n" + line + "\n"
            report = _completion_report(self._story(record), self.MERGED, lab_record="required")
            assert report.lab_record_missing == ["not-induced"] and not report.ok, line
        for line in (
            "- Every arm was induced for real (12/12).",
            "- All arms were induced and restored.",
            "- Lab: 18/18; every arm induced.",
        ):
            record = "### Merge and lab record (operator, 2026-09-30)\n\n" + line + "\n"
            report = _completion_report(self._story(record), self.MERGED, lab_record="required")
            assert report.ok, (line, report.problems)

    def test_zero_is_a_negation(self):
        from claudomater.completion import _completion_report

        for line in (
            "- Zero arms were induced; STATED.",
            "- 0 arms were induced; STATED.",
            "- The probe induced zero arms; STATED.",
            "- The probe induced 0 of the arms; STATED.",
        ):
            record = "### Merge and lab record (operator, 2026-09-30)\n\n" + line + "\n"
            report = _completion_report(self._story(record), self.MERGED, lab_record="required")
            assert report.lab_record_missing == ["induced"] and not report.ok, line
        # a count elsewhere in the item does not negate its own clause
        record = ("### Merge and lab record (operator, 2026-09-30)\n\n"
                  "- Lab 12/12, 0 drift; ADMIN arm: induced; PORT arm: not induced.\n")
        assert _completion_report(self._story(record), self.MERGED, lab_record="required").ok

    def test_an_indented_code_block_is_not_the_record(self):
        from claudomater.completion import _completion_report

        record = ("### Merge and lab record (operator, 2026-09-30)\n\n"
                  "    - ADMIN arm: induced through the real dialog.\n"
                  "    - PORT arm: not induced; STATED.\n")
        report = _completion_report(self._story(record), self.MERGED, lab_record="required")
        assert report.lab_record_missing == ["induced", "not-induced"] and not report.ok
        # a code block may hold blank lines and still be code
        record = ("### Merge and lab record (operator, 2026-09-30)\n\n"
                  "    - ADMIN arm: induced.\n\n    - PORT arm: not induced; STATED.\n")
        report = _completion_report(self._story(record), self.MERGED, lab_record="required")
        assert report.lab_record_missing == ["induced", "not-induced"] and not report.ok
        # a wrapped bullet continued on a four-space-indented line is prose
        record = ("### Merge and lab record (operator, 2026-09-30)\n\n"
                  "- The port arm was not\n    induced; STATED.\n- ADMIN arm: induced.\n")
        assert _completion_report(self._story(record), self.MERGED, lab_record="required").ok

    def test_every_specific_arm_in_an_item_needs_its_own_disposition(self):
        from claudomater.completion import _completion_report

        record = ("### Merge and lab record (operator, 2026-09-30)\n\n"
                  "- ADMIN arm: induced; PORT arm: evidence pending.\n"
                  "- FALLBACK arm: not induced; STATED.\n")
        report = _completion_report(self._story(record), self.MERGED, lab_record="required")
        assert report.lab_record_missing == ["arm:1"] and not report.ok
        # two arms in one statement, the second without its own disposition
        record = ("### Merge and lab record (operator, 2026-09-30)\n\n"
                  "- The port arm was not induced but the interface arm was exercised.\n")
        report = _completion_report(self._story(record), self.MERGED, lab_record="required")
        assert "arm:1" in report.lab_record_missing and not report.ok
        for line in (
            "- ADMIN arm: induced; PORT arm: not induced; STATED.",
            "- Both arms ran: the ADMIN arm induced for real, the PORT arm not induced (STATED).",
            "- PORT arm: evidence pending; STATED.",
            "- Lab: the interface arm INDUCED for real and restored; STATED, not induced: the port arm (shared lab).",
        ):
            record = "### Merge and lab record (operator, 2026-09-30)\n\n" + line + "\n- ADMIN arm: induced.\n"
            report = _completion_report(self._story(record), self.MERGED, lab_record="required")
            assert report.ok, (line, report.problems)

    def test_a_raw_html_block_is_not_the_record(self):
        from claudomater.completion import _completion_report

        for wrapped in (
            "<pre>\n### Merge and lab record (operator, 2026-09-30)\n\n- ADMIN arm: induced.\n- PORT arm: not induced; STATED.\n</pre>\n",
            "<script>\n### Merge and lab record (operator, 2026-09-30)\n\n- ADMIN arm: induced.\n- PORT arm: not induced; STATED.\n",
            "<div>\n### Merge and lab record (operator, 2026-09-30)\n- ADMIN arm: induced.\n- PORT arm: not induced; STATED.\n</div>\n",
        ):
            report = _completion_report(self._story(wrapped), self.MERGED, lab_record="required")
            assert report.lab_record_missing == ["section"] and not report.ok, wrapped[:12]
        # an HTML block after the real record hides its own contents only
        record = ("### Merge and lab record (operator, 2026-09-30)\n\n"
                  "- ADMIN arm: induced.\n- PORT arm: not induced; STATED.\n\n"
                  "<details>\n<summary>probe output</summary>\n- FALLBACK arm: evidence pending\n</details>\n")
        assert _completion_report(self._story(record), self.MERGED, lab_record="required").ok

    def test_negative_contractions_are_negations(self):
        from claudomater.completion import _completion_report

        for line in (
            "- ADMIN arm hasn't been induced; STATED.",
            "- PORT arm won't be induced on the shared lab; STATED.",
            "- The TLS arm wasn\u2019t induced (curly apostrophe); STATED.",
            "- Both arms haven't been induced and shouldn't be; STATED.",
        ):
            record = "### Merge and lab record (operator, 2026-09-30)\n\n" + line + "\n"
            report = _completion_report(self._story(record), self.MERGED, lab_record="required")
            assert report.lab_record_missing == ["induced"] and not report.ok, line

    def test_other_raw_html_block_forms_are_hidden(self):
        from claudomater.completion import _completion_report

        body = "### Merge and lab record (operator, 2026-09-30)\n\n- ADMIN arm: induced.\n- PORT arm: not induced; STATED.\n"
        for wrapped in (
            "<?template\n" + body + "?>\n",
            "<![CDATA[\n" + body + "]]>\n",
            "<!RECORD\n" + body + ">\n",
            "<?template\n" + body,
            # a complete open tag whose quoted attribute holds > (type 7)
            "<span title=\">\">\n" + body.rstrip("\n") + "\n</span>\n",
            "<span data-x='<' class=\"a b\">\n" + body.rstrip("\n") + "\n</span>\n",
        ):
            report = _completion_report(self._story(wrapped), self.MERGED, lab_record="required")
            assert report.lab_record_missing == ["section"] and not report.ok, wrapped[:12]

    def test_an_indented_fence_line_inside_a_fence_is_content(self):
        from claudomater.completion import _completion_report

        body = "### Merge and lab record (operator, 2026-09-30)\n\n- ADMIN arm: induced.\n- PORT arm: not induced; STATED.\n"
        fenced = "```\n    ```\n" + body + "```\n"
        report = _completion_report(self._story(fenced), self.MERGED, lab_record="required")
        assert report.lab_record_missing == ["section"] and not report.ok
        # a fence opened inside a list item closes at its own indent, and
        # the real record after it is visible
        record = "- note:\n  ```\n  example\n  ```\n\n" + body
        assert _completion_report(self._story(record), self.MERGED, lab_record="required").ok

    def test_a_negated_all_induced_statement_is_not_an_exception(self):
        from claudomater.completion import _completion_report

        record = "### Merge and lab record (operator, 2026-09-30)\n\n- Not every arm was induced.\n"
        report = _completion_report(self._story(record), self.MERGED, lab_record="required")
        assert "induced" in report.lab_record_missing and not report.ok

    def test_no_runtime_arm_followed_by_a_verb_is_not_the_exception(self):
        from claudomater.completion import _completion_report

        record = "### Merge and lab record (operator, 2026-09-30)\n\n- No runtime arm was induced.\n"
        report = _completion_report(self._story(record), self.MERGED, lab_record="required")
        assert "induced" in report.lab_record_missing and not report.ok

    def test_the_no_runtime_arm_exception_works_as_a_normal_bullet(self):
        from claudomater.completion import _completion_report

        record = "### Merge and lab record (operator, 2026-09-30)\n\n- No runtime arm exists: the merge touches docs only (STATED).\n"
        report = _completion_report(self._story(record), self.MERGED, lab_record="required")
        assert report.ok, report.problems

    def test_a_negated_stated_is_not_a_disposition(self):
        from claudomater.completion import _completion_report

        record = ("### Merge and lab record (operator, 2026-09-30)\n\n"
                  "- ADMIN arm: induced for real and restored.\n"
                  "- PORT arm: not STATED because the lab was unavailable.\n")
        report = _completion_report(self._story(record), self.MERGED, lab_record="required")
        assert report.lab_record_missing == ["arm:2", "not-induced"] and not report.ok
        # the same arm with a real disposition passes
        record = record.replace("not STATED because the lab was unavailable",
                                "not induced (the lab was unavailable); STATED")
        assert _completion_report(self._story(record), self.MERGED, lab_record="required").ok

    def test_no_runtime_arm_with_another_predicate_is_not_the_exception(self):
        from claudomater.completion import _completion_report

        for line in (
            "- No runtime arm passed validation.",
            "- No runtime arm requires induction.",
            "- No runtime arm was induced.",
        ):
            record = "### Merge and lab record (operator, 2026-09-30)\n\n" + line + "\n"
            report = _completion_report(self._story(record), self.MERGED, lab_record="required")
            assert "induced" in report.lab_record_missing and not report.ok, line

    def test_the_standalone_no_runtime_arm_clause_is_the_exception(self):
        from claudomater.completion import _completion_report

        for line in (
            "- No runtime arm.",
            "- No runtime arm exists (the merge touches docs only).",
            "- Docs only, no runtime arm; STATED.",
            "- There is no runtime arm for this story.",
        ):
            record = "### Merge and lab record (operator, 2026-09-30)\n\n" + line + "\n"
            report = _completion_report(self._story(record), self.MERGED, lab_record="required")
            assert report.ok, (line, report.problems)

    def test_a_positive_clause_after_a_negative_one_counts(self):
        from claudomater.completion import _completion_report

        record = ("### Merge and lab record (operator, 2026-09-30)\n\n"
                  "- The port arm was not induced; the interface arm was induced for real and restored.\n")
        assert _completion_report(self._story(record), self.MERGED, lab_record="required").ok

    def test_no_non_inducible_arm_is_not_an_all_induced_statement(self):
        from claudomater.completion import _completion_report

        record = "### Merge and lab record (operator, 2026-09-30)\n\n- Lab: 12/12; no non-inducible arm exists.\n"
        report = _completion_report(self._story(record), self.MERGED, lab_record="required")
        # "non-inducible" is a negative span (so the negative side is met) but
        # nothing says an arm WAS induced
        assert report.lab_record_missing == ["induced"] and not report.ok

    def test_every_arm_induced_satisfies_both_sides(self):
        from claudomater.completion import _completion_report

        record = "### Merge and lab record (operator, 2026-09-30)\n\n- Lab: 18/18; every arm induced - every arm is a click.\n"
        assert _completion_report(self._story(record), self.MERGED, lab_record="required").ok

    def test_bare_yaml_off_and_on_normalize(self):
        from claudomater.completion import normalize_lab_record

        assert normalize_lab_record(False) == "off"
        assert normalize_lab_record(True) == "required"

    def test_an_unknown_mode_is_a_typed_error(self):
        from claudomater.completion import _completion_report

        with pytest.raises(CompletionError, match="completion.lab_record"):
            _completion_report(STORY, self.MERGED, lab_record="strict")

    def test_the_gate_reads_the_mode_from_config_and_logs_it(self, tmp_path):
        repo, sha = _synthetic_repo(
            tmp_path,
            {"app/src/Widget.tsx": "w\n", "app/src/Widget.test.tsx": "t\n", "docs/note.md": "n\n"},
        )
        (repo / "story.md").write_text(self._story(LAB_RECORD_NO_DISPOSITION))
        log = _FakeRunLog()
        report = run_completion_gate(repo, _CfgWithExempt([], lab_record="required"), "story.md", sha, log)
        assert not report.ok and report.lab_record_missing[-2:] == ["induced", "not-induced"]
        (ev,) = log.events
        assert ev["detail"]["lab_record"] == "required"
        assert ev["detail"]["lab_record_missing"][-2:] == ["induced", "not-induced"]
        # off: the same story passes
        assert run_completion_gate(repo, _CfgWithExempt([]), "story.md", sha, _FakeRunLog()).ok

