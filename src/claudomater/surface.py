"""Surface classification: does a changed-file set touch user-facing
surface? (Phase 3 deliverable 3, part 1.)

A port of the epic-44 classifier from the predecessor automator, split
into what it always really was: a GENERIC exclusions-first pattern engine
(this module) and a PROJECT rule set (the consumer repo's `.omater.yaml`
`surface_rules:` block). The predecessor hardcoded the patterns beside the
engine and they drifted from the project's own rules document within
days of that document changing - the rules now live in committed config
beside the document that governs them, and this engine has no defaults
to drift.

Semantics preserved from the original, each one a burn scar:
- Exclusions are evaluated FIRST: `ui/src/test/**` is non-surface even
  though it also matches `ui/src/**`. Match precedence is the documented
  contract, not an implementation accident.
- `**` matches on a path-segment boundary: `ui/src/**` must not swallow
  `ui/srcfoo/x` (fnmatch alone lets `*` cross `/`).
- `neutral` is a real third outcome: a path in neither list neither
  triggers the gate nor needs an exclusion to avoid triggering it.
- A path outside the repo-relative namespace (absolute, `..`) has no
  honest classification: every pattern is repo-relative, so it would
  match nothing and land on `neutral` - silently waiving the gate for
  what may well be a surface file. Refused instead.
- An empty file set - literally, or after discarding blank entries - is
  a broken changed-file lookup, not a story that touched nothing.
  Refused: `surface_touching: False` from a lookup that resolved to
  nothing is the exact silent waiver this module exists to remove.
- Optionally (per the rule set), root-level dotfiles are excluded by
  regex, scoped to the repo root: a dotfile nested under `ui/` is NOT
  excluded by it.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from typing import Any

_ROOT_DOTFILE_RE = re.compile(r"^\.[^/]+$")


class SurfaceError(Exception):
    """The classification cannot be made honestly. Never swallowed: a
    silent pass is indistinguishable from a real pass at the call site."""


@dataclass(frozen=True)
class SurfaceRules:
    """A project's surface-classification rule set.

    Loaded from committed config (`.omater.yaml` `surface_rules:`), so the
    rules are reviewable in the same repo as the process document that
    governs them - if they ever disagree, the document wins and the
    config is the bug, and the disagreement is at least VISIBLE in one
    repo's diffs.
    """

    surface: tuple[str, ...]
    exclude: tuple[str, ...] = ()
    exclude_root_dotfiles: bool = False

    def __post_init__(self) -> None:
        for name, patterns in (("surface", self.surface), ("exclude", self.exclude)):
            for pattern in patterns:
                if not isinstance(pattern, str) or not pattern.strip():
                    raise SurfaceError(
                        f"{name} patterns must be non-empty strings, got {pattern!r}"
                    )
                if pattern.startswith("/"):
                    raise SurfaceError(
                        f"{name} pattern {pattern!r} is absolute - every "
                        "pattern is repo-relative"
                    )
        if not self.surface:
            # An empty surface list classifies EVERYTHING as non-surface:
            # the gate would never fire and every story would silently
            # waive. If a project genuinely has no surface, it should not
            # be running a surface gate at all.
            raise SurfaceError(
                "a surface rule set needs at least one surface pattern - "
                "an empty list waives the gate on every story"
            )


def _matches(path: str, pattern: str) -> bool:
    """Glob match with `**` meaning "this dir and everything under it",
    compared on a path-segment boundary (fnmatch's `*` crosses `/`, so
    `ui/src/**` as raw fnmatch would swallow `ui/srcfoo/x`)."""
    if pattern.endswith("/**"):
        prefix = pattern[:-3]
        return path == prefix or path.startswith(prefix + "/")
    return fnmatch.fnmatch(path, pattern)


def classify_path(path: str, rules: SurfaceRules) -> str:
    """One repo-relative path -> 'excluded' | 'surface' | 'neutral'.

    Order is load-bearing: exclusions first, surface second, neutral for
    a path in neither list."""
    path = path.strip()
    # a literal-prefix strip, NOT lstrip("./"): lstrip is a character-set
    # strip that eats the leading dot of every dotfile path, turning
    # `.claude/x` into `claude/x` - which then matches nothing
    if path.startswith("./"):
        path = path[2:]
    if not path:
        raise SurfaceError("empty path passed to classify_path")
    if path.startswith("/") or ".." in path.split("/"):
        raise SurfaceError(
            f"path is not repo-relative: {path!r}. Every pattern is "
            "repo-relative, so this path would match nothing and classify "
            "'neutral' - silently waiving the gate. Pass paths as they "
            "appear in `git show --name-only`."
        )
    if rules.exclude_root_dotfiles and _ROOT_DOTFILE_RE.match(path):
        return "excluded"
    for pattern in rules.exclude:
        if _matches(path, pattern):
            return "excluded"
    for pattern in rules.surface:
        if _matches(path, pattern):
            return "surface"
    return "neutral"


@dataclass
class SurfaceVerdict:
    """Per-story classification with the evidence kept alongside."""

    surface_touching: bool = False
    surface: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    neutral: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "surface_touching": self.surface_touching,
            "surface": self.surface,
            "excluded": self.excluded,
            "neutral": self.neutral,
        }


def classify_changed_files(paths: list[str], rules: SurfaceRules) -> SurfaceVerdict:
    """Classify a story's changed-file set, refusing an input that is
    empty either literally or after trimming blanks.

    The blank case is checked SEPARATELY from `if not paths` on purpose
    (the predecessor's review caught this door): `["  "]` skipped every
    loop iteration and produced a confident 'no surface' off a file set
    that had resolved to nothing."""
    cleaned = [raw.strip() for raw in paths if raw and raw.strip()]
    if not cleaned:
        raise SurfaceError(
            "no changed files resolved - refusing to report 'no surface' "
            f"from a file set of {len(paths)} entr"
            f"{'y' if len(paths) == 1 else 'ies'} that is empty after "
            "trimming (indistinguishable from a broken lookup)"
        )
    verdict = SurfaceVerdict()
    for path in cleaned:
        getattr(verdict, classify_path(path, rules)).append(path)
    verdict.surface_touching = bool(verdict.surface)
    return verdict


def rules_from_config(raw: Any) -> SurfaceRules | None:
    """The `surface_rules:` config block -> SurfaceRules; None when the block
    is absent (project declares no surface gate). Garbage fails loudly at
    config load, like every other knob:

        surface_rules:
          surface: [\"ui/src/**\", \"backend/api/**\"]
          exclude: [\"ui/src/test/**\", \"docs/**\"]
          exclude_root_dotfiles: true
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SurfaceError(
            f"surface_rules must be a mapping with 'surface'/'exclude' pattern "
            f"lists, got {type(raw).__name__}"
        )
    unknown = sorted(
        k for k in raw if k not in ("surface", "exclude", "exclude_root_dotfiles")
    )
    if unknown:
        # a typo'd key would silently deactivate the rule it meant to
        # declare - the exact drift this block exists to end
        raise SurfaceError(f"surface_rules: unknown key(s): {', '.join(unknown)}")
    surface = raw.get("surface")
    exclude = raw.get("exclude", [])
    dotfiles = raw.get("exclude_root_dotfiles", False)
    for name, patterns in (("surface", surface), ("exclude", exclude)):
        if not isinstance(patterns, list):
            raise SurfaceError(
                f"surface_rules.{name} must be a list of patterns, got {patterns!r}"
            )
    if not isinstance(dotfiles, bool):
        raise SurfaceError(
            f"surface_rules.exclude_root_dotfiles must be true/false, got {dotfiles!r}"
        )
    return SurfaceRules(
        surface=tuple(surface),
        exclude=tuple(exclude),
        exclude_root_dotfiles=dotfiles,
    )
