"""Surface classification (Phase 3 deliverable 3, part 1).

Engine pins run against a SYNTHETIC rule set that mirrors the real one's
shapes (a `**` dir glob, a nested-test exclusion inside a surface dir,
fnmatch wildcards, the root-dotfile regex). The acceptance replays run
against the REAL consumer repo - its committed rules, its real merges -
and are skipped on machines without it, like the sprint proofs.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from claudomater.surface import (
    SurfaceError,
    SurfaceRules,
    classify_changed_files,
    classify_path,
    rules_from_config,
)

RULES = SurfaceRules(
    surface=(
        "app/src/**",
        "app/index.html",
        "server/api/**",
        "server/main.py",
    ),
    exclude=(
        "app/src/test/**",
        "docs/**",
        "notes*",
        "compose.*.yml",
    ),
    exclude_root_dotfiles=True,
)

# The ui3 checkout the acceptance replays run against. READ-ONLY - rules
# come from its committed .omater.yaml, file sets from `git show` on real
# merges; nothing is copied into this repo.
UI3 = Path(os.environ.get("OMATER_UI3_ROOT", Path.home() / "sourcecode/ui3"))

requires_ui3 = pytest.mark.skipif(
    not (UI3 / ".omater.yaml").is_file(), reason="ui3 checkout not present"
)


class TestRuleValidation:
    def test_absent_block_means_no_gate(self):
        assert rules_from_config(None) is None

    def test_a_valid_block_loads(self):
        rules = rules_from_config(
            {
                "surface": ["app/src/**"],
                "exclude": ["docs/**"],
                "exclude_root_dotfiles": True,
            }
        )
        assert rules.surface == ("app/src/**",)
        assert rules.exclude_root_dotfiles is True

    def test_exclude_and_dotfiles_are_optional(self):
        rules = rules_from_config({"surface": ["app/**"]})
        assert rules.exclude == ()
        assert rules.exclude_root_dotfiles is False

    def test_unknown_keys_fail_loudly(self):
        """A typo'd key would silently deactivate the rule it meant to
        declare - the exact drift this block exists to end."""
        with pytest.raises(SurfaceError, match="unknown key"):
            rules_from_config({"surface": ["a/**"], "exlude": ["docs/**"]})

    def test_non_mapping_and_non_list_shapes_fail(self):
        with pytest.raises(SurfaceError, match="mapping"):
            rules_from_config(["app/src/**"])
        with pytest.raises(SurfaceError, match="must be a list"):
            rules_from_config({"surface": "app/src/**"})
        with pytest.raises(SurfaceError, match="true/false"):
            rules_from_config({"surface": ["a/**"], "exclude_root_dotfiles": "yes"})

    def test_an_empty_surface_list_is_refused(self):
        """Everything would classify non-surface: the gate never fires and
        every story silently waives."""
        with pytest.raises(SurfaceError, match="at least one surface pattern"):
            rules_from_config({"surface": []})

    def test_absolute_and_blank_patterns_are_refused(self):
        with pytest.raises(SurfaceError, match="absolute"):
            SurfaceRules(surface=("/app/src/**",))
        with pytest.raises(SurfaceError, match="non-empty"):
            SurfaceRules(surface=("app/**",), exclude=("  ",))


class TestClassifyPath:
    def test_exclusion_beats_surface(self):
        """Match precedence is the documented contract: a path matching an
        exclusion is non-surface even when it also matches a surface glob."""
        assert classify_path("app/src/test/utils/geom.ts", RULES) == "excluded"
        assert classify_path("app/src/App.tsx", RULES) == "surface"

    def test_dir_globs_match_on_segment_boundaries(self):
        """fnmatch alone lets `*` cross `/`, so `app/src/**` would swallow
        a sibling whose name merely starts with `src`."""
        assert classify_path("app/srcfoo/x.ts", RULES) == "neutral"
        assert classify_path("app/src", RULES) == "surface"  # the dir itself

    def test_root_dotfiles_are_excluded_but_nested_ones_are_not(self):
        assert classify_path(".gitignore", RULES) == "excluded"
        assert classify_path("app/.gitignore", RULES) == "neutral"

    def test_dotfile_exclusion_is_opt_in(self):
        bare = SurfaceRules(surface=("app/**",))
        assert classify_path(".gitignore", bare) == "neutral"

    def test_neutral_is_a_real_third_outcome(self):
        assert classify_path("app/package.json", RULES) == "neutral"

    def test_fnmatch_wildcards_still_work_for_file_patterns(self):
        assert classify_path("notes.md", RULES) == "excluded"
        assert classify_path("compose.override.yml", RULES) == "excluded"
        # `notes*` is root-anchored the way fnmatch is: no `/` in the
        # pattern means it cannot match a nested path
        assert classify_path("app/notes.md", RULES) == "neutral"

    def test_dot_slash_prefix_is_stripped_literally(self):
        """lstrip('./') is a character-set strip that eats the leading dot
        of every dotfile - the prefix strip must be literal."""
        assert classify_path("./app/src/App.tsx", RULES) == "surface"
        assert classify_path("./.gitignore", RULES) == "excluded"

    def test_absolute_and_traversing_paths_are_refused(self):
        """They would match nothing and land on 'neutral' - a silent
        waiver for what may well be a surface file."""
        for bad in ("/app/src/App.tsx", "app/../secrets", ".."):
            with pytest.raises(SurfaceError, match="not repo-relative"):
                classify_path(bad, RULES)

    def test_an_empty_path_is_refused(self):
        with pytest.raises(SurfaceError, match="empty path"):
            classify_path("   ", RULES)


class TestClassifyChangedFiles:
    def test_buckets_and_the_flag(self):
        verdict = classify_changed_files(
            ["app/src/App.tsx", "docs/guide.md", "app/tsconfig.json"], RULES
        )
        assert verdict.surface_touching is True
        assert verdict.surface == ["app/src/App.tsx"]
        assert verdict.excluded == ["docs/guide.md"]
        assert verdict.neutral == ["app/tsconfig.json"]
        assert set(verdict.as_dict()) == {
            "surface_touching", "surface", "excluded", "neutral",
        }

    def test_neutral_only_is_not_surface(self):
        verdict = classify_changed_files(["app/tsconfig.json"], RULES)
        assert verdict.surface_touching is False

    def test_an_empty_file_set_is_refused(self):
        with pytest.raises(SurfaceError, match="no changed files"):
            classify_changed_files([], RULES)

    def test_an_all_blank_file_set_is_refused_too(self):
        """The predecessor's review caught this separate door: every entry
        skipped by the loop produced a confident 'no surface' off a lookup
        that resolved to nothing."""
        with pytest.raises(SurfaceError, match="no changed files"):
            classify_changed_files(["  ", ""], RULES)


def _ui3_rules():
    from claudomater.config import load_project_config

    rules = load_project_config(UI3).surface_rules
    assert rules is not None, "ui3 .omater.yaml must declare surface_rules"
    return rules


def _merged_files(sha: str) -> list[str]:
    out = subprocess.run(
        ["git", "show", "--name-only", "--format=", sha],
        cwd=UI3,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [line for line in out.splitlines() if line.strip()]


@requires_ui3
class TestUi3AcceptanceReplays:
    """The named acceptance proofs, replayed through the REAL rules in
    ui3's committed .omater.yaml against the REAL merged file sets - the
    same discipline as the sprint round-trip proof: read-only, operator's
    machine only, nothing copied into this repo.

    Merge SHAs: story 34-36 = a5105e31 (PR #381), story 46-7 = 5b26c746
    (PR #379), epic 43 = 91b45132 / cd87c3ea / 3b96971f / 0d367a5c
    (PRs #348/#350/#351/#352) - the predecessor's regression corpus, whose
    failure of record is that epic shipping with no board presence at all.
    """

    def test_34_36_replays_surface(self):
        verdict = classify_changed_files(_merged_files("a5105e31"), _ui3_rules())
        assert verdict.surface_touching is True
        # the one excluded path is the shared test utility under the
        # test-dir exclusion; the co-located component tests are NOT
        # excluded (the exclusion is path-prefixed, not filename-based)
        assert len(verdict.excluded) == 1
        assert verdict.excluded[0].startswith("ui/src/test/")
        assert all(p.endswith((".tsx", ".ts")) for p in verdict.surface)
        assert len(verdict.surface) == 18

    def test_46_7_replays_no_surface_with_claude_md_neutral(self):
        verdict = classify_changed_files(_merged_files("5b26c746"), _ui3_rules())
        assert verdict.surface_touching is False
        # CLAUDE.md matches NEITHER list: neutral must not read as surface,
        # and must not need an exclusion to avoid the gate
        assert "CLAUDE.md" in verdict.neutral
        assert len(verdict.excluded) == 3  # two workflow files + docs

    def test_epic_43_replays_match_the_regression_corpus(self):
        rules = _ui3_rules()
        for sha, expect_surface in (
            ("91b45132", True),   # 43-1: UI correctness fixes
            ("cd87c3ea", True),   # 43-2: license-lock UI chain
            ("3b96971f", False),  # 43-3: CI/workflow tooling
            ("0d367a5c", False),  # 43-4: automator hook honesty
        ):
            verdict = classify_changed_files(_merged_files(sha), rules)
            assert verdict.surface_touching is expect_surface, (sha, verdict)

    def test_the_45_4_shape_is_surface_now(self):
        """The drift bug's victim shape: a backend/services/** change is
        user-visible surface per CLAUDE.md since 2026-08-27, and the
        predecessor's hardcoded list missed it for four days. The committed
        rules must classify it surface from day one."""
        assert (
            classify_path("backend/services/health_score.py", _ui3_rules())
            == "surface"
        )


class TestCaseSensitivity:
    def test_matching_is_case_sensitive_on_every_os(self):
        """Copilot round-1: plain fnmatch normcases both sides, making the
        classification OS-dependent (case-insensitive on Windows). A repo
        path's case is data: 'readme.md' is not 'README*'."""
        rules = SurfaceRules(surface=("app/**",), exclude=("README*",))
        assert classify_path("README.md", rules) == "excluded"
        assert classify_path("readme.md", rules) == "neutral"
