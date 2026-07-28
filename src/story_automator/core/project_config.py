"""Per-project story-automator configuration.

Reads ``$PROJECT_ROOT/_bmad/automator/story-automator.yaml`` — the dedicated,
BMAD-reinstall-safe contract that carries the few values that genuinely vary
per project: the project name, the test gauntlet, the review-bridge skill, the
branch pattern, and the PR/Copilot toggles.

Dependency-free on purpose: the rest of this package parses YAML with the
stdlib (see ``sprint.py``), so this module ships a tiny parser for the shallow,
documented schema rather than pulling in PyYAML. It handles top-level scalars,
a single block list, and one level of nested maps — nothing more.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .utils import get_project_root, read_text

RELATIVE_PATH = Path("_bmad") / "automator" / "story-automator.yaml"

# Sensible global defaults so the engine runs even with no per-project file.
DEFAULT_REVIEW_BRIDGE = "bmad-story-automator-review"
DEFAULT_BRANCH_PATTERN = "epic{epic}/{story_slug}"

# Config-pattern placeholders -> the bash variables the dev prompt computes
# before it names the story branch (see data/prompts/dev.md, Step 0).
_BRANCH_BASH_VARS = {
    "{epic}": "${epic_num}",
    "{story_slug}": "${story_slug}",
    "{story_id}": "${story_id}",
    "{story_prefix}": "${story_prefix}",
}


def _coerce(value: str) -> Any:
    value = value.strip()
    if value[:1] in ("'", '"'):
        quote = value[0]
        end = value.find(quote, 1)
        return value[1:end] if end != -1 else value[1:]
    # Unquoted scalar: drop a trailing inline comment, then coerce booleans.
    if " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    low = value.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    return value


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    """Parse the documented story-automator.yaml schema. Not general YAML."""
    root: dict[str, Any] = {}
    current_key: str | None = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        stripped = raw.strip()
        if indent == 0:
            key, sep, val = stripped.partition(":")
            if not sep:
                continue
            key = key.strip()
            val = val.strip()
            if val == "":
                root[key] = None
                current_key = key
            else:
                root[key] = _coerce(val)
                current_key = None
        else:
            if current_key is None:
                continue
            if stripped.startswith("- "):
                if not isinstance(root.get(current_key), list):
                    root[current_key] = []
                root[current_key].append(_coerce(stripped[2:]))
            else:
                k, sep, v = stripped.partition(":")
                if not sep:
                    continue
                if not isinstance(root.get(current_key), dict):
                    root[current_key] = {}
                root[current_key][k.strip()] = _coerce(v.strip())
    return root


def load_project_config(project_root: str | Path | None = None) -> dict[str, Any]:
    """Return the parsed per-project config, or ``{}`` if the file is absent."""
    root = Path(project_root or get_project_root()).resolve()
    path = root / RELATIVE_PATH
    if not path.is_file():
        return {}
    try:
        parsed = _parse_simple_yaml(read_text(path))
    except OSError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def reviewer_bridge(project_root: str | Path | None = None, config: dict[str, Any] | None = None) -> str:
    cfg = config if config is not None else load_project_config(project_root)
    reviewer = cfg.get("reviewer")
    if isinstance(reviewer, dict):
        bridge = str(reviewer.get("bridge") or "").strip()
        if bridge:
            return bridge
    return DEFAULT_REVIEW_BRIDGE


def project_name(project_root: str | Path | None = None, config: dict[str, Any] | None = None) -> str:
    cfg = config if config is not None else load_project_config(project_root)
    name = str(cfg.get("project_name") or "").strip()
    if name:
        return name
    return Path(project_root or get_project_root()).resolve().name


def test_gauntlet(project_root: str | Path | None = None, config: dict[str, Any] | None = None) -> list[str]:
    cfg = config if config is not None else load_project_config(project_root)
    commands = cfg.get("test_gauntlet")
    if isinstance(commands, list):
        return [str(c) for c in commands if str(c).strip()]
    return []


def test_gauntlet_block(project_root: str | Path | None = None, config: dict[str, Any] | None = None) -> str:
    """The test gauntlet as a newline-joined shell block (empty string if none)."""
    return "\n".join(test_gauntlet(project_root, config))


def branch_pattern(project_root: str | Path | None = None, config: dict[str, Any] | None = None) -> str:
    """The raw ``branch_pattern`` (e.g. ``epic{epic}/{story_slug}``), or the default."""
    cfg = config if config is not None else load_project_config(project_root)
    pattern = str(cfg.get("branch_pattern") or "").strip()
    return pattern or DEFAULT_BRANCH_PATTERN


def branch_pattern_bash(project_root: str | Path | None = None, config: dict[str, Any] | None = None) -> str:
    """The branch pattern with ``{...}`` placeholders rewritten as bash vars.

    The dev prompt computes ``epic_num``/``story_slug``/``story_id``/``story_prefix``
    before naming the branch; this returns a string safe to drop into a
    double-quoted bash assignment (e.g. ``epic${epic_num}/${story_slug}``).
    """
    pattern = branch_pattern(project_root, config)
    for placeholder, bash_var in _BRANCH_BASH_VARS.items():
        pattern = pattern.replace(placeholder, bash_var)
    return pattern
