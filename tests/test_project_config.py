"""Coverage for the per-project story-automator.yaml accessors.

Focus on branch_pattern, which the dev prompt consumes via the
``{{branch_pattern}}`` template variable (see data/prompts/dev.md, Step 0).
The default must render byte-identical to the previously-hardcoded branch name
so upgrading an existing project is a no-op.
"""

from __future__ import annotations

from pathlib import Path

from story_automator.core import project_config as pc


def _write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "_bmad" / "automator" / "story-automator.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return tmp_path


def test_branch_pattern_default_when_no_config(tmp_path):
    # No file at all -> documented default.
    assert pc.branch_pattern(project_root=tmp_path) == pc.DEFAULT_BRANCH_PATTERN


def test_branch_pattern_bash_default_matches_legacy_hardcode(tmp_path):
    # The old dev.md hardcoded exactly this; the default must not change it.
    assert pc.branch_pattern_bash(project_root=tmp_path) == "epic${epic_num}/${story_slug}"


def test_branch_pattern_custom_is_honored(tmp_path):
    root = _write_config(tmp_path, 'branch_pattern: "feature/{story_id}-{story_slug}"\n')
    assert pc.branch_pattern(project_root=root) == "feature/{story_id}-{story_slug}"
    assert pc.branch_pattern_bash(project_root=root) == "feature/${story_id}-${story_slug}"


def test_branch_pattern_empty_falls_back_to_default(tmp_path):
    root = _write_config(tmp_path, 'branch_pattern: ""\n')
    assert pc.branch_pattern_bash(project_root=root) == "epic${epic_num}/${story_slug}"


def test_branch_pattern_accepts_preloaded_config():
    cfg = {"branch_pattern": "epic{epic}/{story_prefix}"}
    assert pc.branch_pattern_bash(config=cfg) == "epic${epic_num}/${story_prefix}"
