"""Project conventions: config-carried standing rules (epic-47 close
follow-up). Two mechanisms: the verbatim prompt block every phase agent
receives, and the pre-push sweep over a diff's added lines."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from claudomater.conventions import (
    ConventionsError,
    conventions_block,
    normalize_conventions,
    sweep_added_lines,
    sweep_git_range,
)
from claudomater.phases import PhaseSpec, inject_conventions

RULES = [
    "No em-dashes in authored content - use ' - '.",
    "No attribution or co-author footers in commits or PR bodies.",
    "Fail loudly: no TODO comments, no silent fallbacks.",
]


class TestGrammar:
    def test_none_is_empty(self):
        assert normalize_conventions(None) == ()

    def test_entries_are_stripped_verbatim(self):
        assert normalize_conventions(["  a rule  "]) == ("a rule",)

    @pytest.mark.parametrize("bad", ["", "   ", 3, None])
    def test_non_strings_and_blanks_are_refused(self, bad):
        with pytest.raises(ConventionsError):
            normalize_conventions([bad])

    def test_non_list_is_refused(self):
        with pytest.raises(ConventionsError, match="must be a list"):
            normalize_conventions("a rule")


class TestPromptInjection:
    def test_the_phase_prompt_provably_contains_the_block(self):
        """The acceptance: a phase prompt carries every configured rule
        verbatim under the fixed frame."""
        cfg = type("C", (), {"conventions": tuple(RULES)})()
        spec = PhaseSpec(name="dev", model="m", prompt="Do the story.")
        out = inject_conventions(spec, cfg)
        assert "## Project conventions" in out.prompt
        for rule in RULES:
            assert rule in out.prompt

    def test_an_empty_list_leaves_the_spec_unchanged(self):
        cfg = type("C", (), {"conventions": ()})()
        spec = PhaseSpec(name="dev", model="m", prompt="Do the story.")
        assert inject_conventions(spec, cfg) is spec

    def test_block_of_nothing_is_empty(self):
        assert conventions_block(()) == ""


DIFF_TEMPLATE = """\
diff --git a/docs/x.md b/docs/x.md
--- a/docs/x.md
+++ b/docs/x.md
@@ -1,2 +1,4 @@
 unchanged line
-removed line with an em-dash — legal, it is leaving
+{added}
"""


class TestSweep:
    def test_red_an_injected_em_dash_is_a_finding(self):
        """The acceptance RED: authored prose gains an em-dash."""
        diff = DIFF_TEMPLATE.format(added="new prose — with an em-dash")
        findings = sweep_added_lines(diff)
        assert len(findings) == 1
        assert "em-dash" in findings[0] and "docs/x.md" in findings[0]

    def test_a_backtick_quoted_em_dash_is_legal(self):
        """The 47-4 case: quoting a historical heading verbatim inside a
        code span must stay legal."""
        diff = DIFF_TEMPLATE.format(
            added="its DoD heading is `## 8 — Definition of Done` which fails"
        )
        assert sweep_added_lines(diff) == []

    def test_removed_lines_are_not_judged(self):
        diff = DIFF_TEMPLATE.format(added="clean added line")
        assert sweep_added_lines(diff) == []

    def test_an_added_line_starting_with_plus_plus_is_still_swept(self):
        """An added content line that itself begins with '++' renders as
        '+++...' in the unified diff - it must not be mistaken for a
        file header and escape the sweep."""
        diff = DIFF_TEMPLATE.format(added="++prefix prose — with an em-dash")
        findings = sweep_added_lines(diff)
        assert len(findings) == 1 and "em-dash" in findings[0]

    @pytest.mark.parametrize(
        "footer",
        [
            "Co-Authored-By: Somebody <x@y>",
            "co-authored-by: lower case",
            "Generated with [Claude Code](https://claude.com/claude-code)",
            "generated with claude",
        ],
    )
    def test_attribution_footers_are_findings(self, footer):
        diff = DIFF_TEMPLATE.format(added=footer)
        findings = sweep_added_lines(diff)
        assert len(findings) == 1
        assert "attribution" in findings[0]

    def test_sweep_git_range_reads_a_real_diff(self, tmp_path):
        repo = tmp_path / "r"
        repo.mkdir()
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull,
               "GIT_CONFIG_SYSTEM": os.devnull}

        def git(*args):
            subprocess.run(["git", *args], cwd=repo, env=env, check=True,
                           capture_output=True)

        git("init", "-q")
        git("config", "user.email", "t@t")
        git("config", "user.name", "t")
        (repo / "a.md").write_text("clean\n")
        git("add", "-A")
        git("commit", "-qm", "base")
        (repo / "a.md").write_text("clean\nnew — dash\n")
        git("add", "-A")
        git("commit", "-qm", "change")
        findings = sweep_git_range(repo, "HEAD~1..HEAD")
        assert len(findings) == 1 and "em-dash" in findings[0]

    def test_a_bad_range_is_a_typed_error(self, tmp_path):
        repo = tmp_path / "r"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        with pytest.raises(ConventionsError, match="git diff"):
            sweep_git_range(repo, "nope..nada")
