"""Project conventions: standing style/policy rules as CONFIG, not
GO-prompt restatement (Clay, epic-47 close follow-up).

The lessons store deliberately carries zero style rows - style rules are
policy, not incidents - and until now their load-bearing carrier was the
GO prompt itself: the conventions-held-only-by-memory shape ui3's own
lesson warns about. Two mechanisms close it:

- `conventions_block(...)` renders the project's `conventions:` list as a
  framed prompt section every phase agent receives verbatim
  (`phases.inject_conventions` is the one seam, mirroring lessons).
- `sweep_added_lines(...)` is the pre-push check: the ADDED lines of a
  diff must carry no em-dash outside backtick code spans (quoting a
  historical em-dash heading verbatim stays legal - the 47-4 case) and
  no attribution/co-author footer anywhere.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Sequence

EM_DASH = "—"
_CODE_SPAN_RE = re.compile(r"`[^`]*`")
_ATTRIBUTION_RE = re.compile(
    r"Co-Authored-By\s*:|Generated with\b.{0,40}\bClaude", re.IGNORECASE
)
_DIFF_FILE_RE = re.compile(r"^\+\+\+ b/(?P<path>.+)$")


class ConventionsError(Exception):
    pass


def normalize_conventions(entries: object) -> tuple[str, ...]:
    """Validated `conventions:` list: non-blank strings, injected verbatim.
    An entry that is not a string (or is blank) fails at LOAD - a rule
    that renders as garbage is a rule nobody receives."""
    if entries is None:
        return ()
    if not isinstance(entries, list):
        raise ConventionsError(
            f"conventions must be a list of rule strings, got {entries!r}"
        )
    out: list[str] = []
    for raw in entries:
        if not isinstance(raw, str) or not raw.strip():
            raise ConventionsError(
                f"conventions entries must be non-blank strings, got {raw!r}"
            )
        out.append(raw.strip())
    return tuple(out)


def conventions_block(conventions: Sequence[str]) -> str:
    """The framed prompt section. Verbatim rules under a fixed frame, so
    what the agent receives is exactly what the config's diff shows."""
    if not conventions:
        return ""
    lines = "\n".join(f"- {rule}" for rule in conventions)
    return (
        "## Project conventions (from the tracked .omater.yaml - binding "
        "for every artifact you author)\n" + lines
    )


def sweep_added_lines(diff_text: str) -> list[str]:
    """Violations among a unified diff's ADDED lines: em-dashes outside
    backtick code spans, and attribution/co-author footers anywhere.
    Returns human-readable findings naming the file and quoting the
    offending added line; empty means clean."""
    findings: list[str] = []
    current = "?"
    for line in diff_text.splitlines():
        m = _DIFF_FILE_RE.match(line)
        if m:
            current = m.group("path")
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        added = line[1:]
        if _ATTRIBUTION_RE.search(added):
            findings.append(
                f"{current}: attribution footer in added line: {added.strip()[:120]!r}"
            )
        # code spans may QUOTE an em-dash (verbatim historical headings);
        # authored prose may not carry one
        if EM_DASH in _CODE_SPAN_RE.sub("", added):
            findings.append(
                f"{current}: em-dash in added line (use ' - '): "
                f"{added.strip()[:120]!r}"
            )
    return findings


def sweep_git_range(repo: Path | str, rev_range: str) -> list[str]:
    """Sweep the added lines of `git diff <rev_range>` in `repo`."""
    try:
        proc = subprocess.run(
            # determinism flags: an external diff driver, textconv, or
            # color codes would silently reshape the text this parser reads
            ["git", "-C", str(repo), "diff", "--no-ext-diff",
             "--no-textconv", "--no-color", rev_range],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConventionsError(f"git diff {rev_range} failed to run: {exc}") from exc
    if proc.returncode != 0:
        raise ConventionsError(
            f"git diff {rev_range} failed: {proc.stderr.strip()[:300]}"
        )
    return sweep_added_lines(proc.stdout)
