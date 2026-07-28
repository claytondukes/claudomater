"""Story 24-10 AC6 — unit test for the auto-answer matcher.

The matcher is gated on a conservative whitelist. These cases pin the
contract: known elicitation phrasings match, near-misses and content
that legitimately CONTAINS the literal `(y/n)?` substring (story
prose, diff hunks) do not.
"""

from __future__ import annotations

import pytest

from story_automator.core.auto_answer import match_known_prompt


class TestKnownPromptMatches:
    def test_apply_all_3_edits_canonical(self) -> None:
        pane = "Some preamble line\nApply all 3 edits? (y/n)"
        assert match_known_prompt(pane) == "Apply all 3 edits? (y/n)"

    def test_apply_all_17_edits_trailing_qmark(self) -> None:
        pane = "stuff\nApply all 17 edits (y/n)?"
        assert match_known_prompt(pane) == "Apply all 17 edits (y/n)?"

    def test_apply_all_1_edit_singular(self) -> None:
        pane = "Apply all 1 edit? (y/n)"
        assert match_known_prompt(pane) == "Apply all 1 edit? (y/n)"

    def test_continue_with_these_changes(self) -> None:
        pane = "previous content\nContinue with these changes? (y/n)"
        assert match_known_prompt(pane) == "Continue with these changes? (y/n)"


class TestNoMatchOnFalsePositives:
    def test_bare_y_slash_n_alone_does_not_match(self) -> None:
        """Bare `(y/n)?` would false-positive on docs and diffs."""
        assert match_known_prompt("Some inline doc: (y/n)?") is None

    def test_loose_apply_edits_phrasing_does_not_match(self) -> None:
        """`Apply edits? y/n` is too loose; tighten = safer."""
        assert match_known_prompt("Apply edits? y/n") is None

    def test_diff_hunk_containing_literal_does_not_match(self) -> None:
        """Diff hunks frequently contain `(y/n)?` in unrelated code."""
        diff = (
            "diff --git a/cli.py b/cli.py\n"
            "+    prompt = input('Continue (y/n)? ')\n"
            "-    prompt = input('Continue? ')\n"
        )
        assert match_known_prompt(diff) is None

    def test_story_prose_referencing_literal_does_not_match(self) -> None:
        """Story / wiki text that DESCRIBES the prompt is not the prompt."""
        prose = (
            "The wizard prompts you with `(y/n)?` and waits for input. "
            "Story 24-10 closes this stall."
        )
        assert match_known_prompt(prose) is None


class TestTailAnchor:
    def test_match_in_scrollback_far_from_tail_is_ignored(self) -> None:
        """The interactive prompt always sits at the bottom of the
        pane; matches in deep scrollback are stale and must be
        ignored to avoid re-answering the same prompt twice in a row.
        """
        scrollback_with_old_prompt = "\n".join(
            ["Apply all 3 edits? (y/n)"]  # line -20, well past tail window
            + ["progress line"] * 19
            + ["final state — no prompt visible"]
        )
        assert match_known_prompt(scrollback_with_old_prompt) is None

    def test_match_within_last_10_lines_is_caught(self) -> None:
        pane = "\n".join(
            ["scrollback"] * 15
            + ["intermediate"] * 3
            + ["Apply all 3 edits? (y/n)"]
        )
        assert match_known_prompt(pane) == "Apply all 3 edits? (y/n)"

    def test_match_survives_trailing_blank_padding(self) -> None:
        """tmux capture-pane returns the prompt followed by 49 blank
        lines when only one row has rendered. The tail-anchor must
        strip the padding before applying the 10-line limit, or real
        captures (and the AC7 integration test) silently no-match.
        """
        pane = "Apply all 3 edits? (y/n)" + "\n" * 49
        assert match_known_prompt(pane) == "Apply all 3 edits? (y/n)"


class TestEdgeCases:
    def test_empty_string(self) -> None:
        assert match_known_prompt("") is None

    def test_whitespace_only(self) -> None:
        assert match_known_prompt("   \n  \n") is None

    @pytest.mark.parametrize(
        "case_variant",
        [
            "apply all 3 edits? (y/n)",  # lowercase
            "APPLY ALL 3 EDITS? (Y/N)",  # uppercase
            "Apply All 3 Edits? (Y/n)",  # mixed
        ],
    )
    def test_case_insensitive_matching(self, case_variant: str) -> None:
        result = match_known_prompt(case_variant)
        assert result is not None and result.lower() == case_variant.lower()
