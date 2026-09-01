"""Completion-integrity gate: a story cannot flip `done` while its own
paperwork disagrees with reality. (Phase 3 deliverable 4; epic-46 retro
action A5.)

Evidence of record: a story flipped `done` with three top-level tasks
unchecked, their sub-items unexecuted, and five pieces of post-merge
bookkeeping genuinely not done - the run session narrated completion and
nothing diffed the narration against the file. This gate reads the story
file and the ACTUAL merged changeset; narration never satisfies it.

Two blades, both fail-closed:

1. TASK BOXES - any unchecked `- [ ]` inside `## Tasks / Subtasks`, at
   any indent (the evidence's sub-items were indented), blocks. A story
   whose Tasks section cannot be found also blocks: a gate that cannot
   see the boxes must not report them ticked.

2. FILE LIST vs THE MERGE - the `### File List` entries are compared as
   sets against `git show --name-only` on the merge commit. A merged
   file the list omits and a listed file the merge lacks are BOTH
   problems, each named. `exempt` prefixes cover driver-owned artifacts
   that legitimately ride outside the PR (the story file itself, the
   sprint file - they live in a separate repo and never appear in the
   merge). A MISSING File List section blocks by default: measured on
   the real corpus, one shipped story carries a File List that matches
   its merge exactly, and one shipped story has no File List section at
   all - the gate must at least SAY that, because "no list" and "list
   agrees" must never read the same. `require_file_list=False` is the
   explicit project-level opt-out for templates that do not mandate one.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

TASKS_HEADING_RE = re.compile(r"^##\s+Tasks(\s*/\s*Subtasks)?\s*$", re.MULTILINE)
FILE_LIST_HEADING_RE = re.compile(r"^###\s+File List\s*$", re.MULTILINE)
_HEADING_RE = re.compile(r"^#{2,3}\s+\S", re.MULTILINE)
# \s* after the box, not \s+: a bare `- [ ]` with no label text is still
# an unchecked box, and the gate's contract is ANY unchecked box blocks
_UNCHECKED_RE = re.compile(r"^\s*[-*]\s+\[ \]\s*(?P<text>.*)$", re.MULTILINE)
# a File List entry: `- path`, optionally backticked, optionally with a
# trailing annotation like (new) / (modified) / (deleted)
_LIST_ENTRY_RE = re.compile(
    r"^\s*[-*]\s+`?(?P<path>[^`()\s][^`()]*?)`?\s*(?:\((?P<note>[^)]*)\))?\s*$",
    re.MULTILINE,
)


class CompletionError(Exception):
    """The gate cannot be evaluated honestly. Never a pass."""


def normalize_exempt(entries: object) -> tuple[str, ...]:
    """Validated exempt prefixes from config. Entries are repo-relative
    path prefixes (the same segment-boundary semantics `_exempt` matches
    on). Anything that could silently match nothing - absolute paths,
    backslashes, `..`, blank strings - fails loudly at LOAD (epic-47
    retro F3: an exemption nobody can reproduce from the repo is a
    verdict nobody can trust)."""
    if entries is None:
        return ()
    if not isinstance(entries, list):
        raise CompletionError(
            f"completion.exempt must be a list of path prefixes, got {entries!r}"
        )
    out: list[str] = []
    for raw in entries:
        if not isinstance(raw, str) or not raw.strip():
            raise CompletionError(
                f"completion.exempt entries must be non-blank strings, got {raw!r}"
            )
        entry = raw.strip()
        if "\\" in entry:
            raise CompletionError(
                f"completion.exempt entries use forward slashes, got {raw!r}"
            )
        if entry.startswith("./"):
            entry = entry[2:]
        entry = entry.rstrip("/")
        if not entry or entry == ".":
            raise CompletionError(
                f"completion.exempt entry {raw!r} normalizes to nothing - "
                "an exempt-everything entry must be impossible to write"
            )
        if entry.startswith("/") or entry.startswith("~"):
            raise CompletionError(
                f"completion.exempt entries are repo-relative, got {raw!r}"
            )
        segments = entry.split("/")
        if ".." in segments:
            raise CompletionError(
                f"completion.exempt entries must not traverse with '..': {raw!r}"
            )
        if "" in segments or "." in segments:
            # `a//b` and `a/./b` startswith-match no real git path: an
            # entry that can never match is an exemption that silently
            # does nothing - the exact shape this grammar exists to refuse
            raise CompletionError(
                f"completion.exempt entry {raw!r} carries an empty or '.' "
                "path segment and would never match a real path"
            )
        out.append(entry)
    return tuple(out)


@dataclass
class CompletionReport:
    """The gate's verdict with its evidence. `ok` is True only when both
    blades found nothing."""

    unchecked: list[str] = field(default_factory=list)
    missing_from_list: list[str] = field(default_factory=list)
    phantom_in_list: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "unchecked": self.unchecked,
            "missing_from_list": self.missing_from_list,
            "phantom_in_list": self.phantom_in_list,
            "problems": self.problems,
        }


def _section(text: str, heading_re: re.Pattern[str]) -> str | None:
    """The body between `heading_re`'s match and the next ##/### heading,
    or None when the heading is absent."""
    m = heading_re.search(text)
    if m is None:
        return None
    nxt = _HEADING_RE.search(text, m.end())
    return text[m.end() : nxt.start() if nxt else len(text)]


def _exempt(path: str, prefixes: Sequence[str]) -> bool:
    for prefix in prefixes:
        p = prefix.rstrip("/")
        if path == p or path.startswith(p + "/"):
            return True
    return False


def file_list_paths(section: str) -> list[str]:
    """Paths from a File List section's bullet lines. Lines that are not
    bullets (prose, blank) are ignored; a bullet that yields no path is a
    malformed entry and raises rather than silently thinning the list."""
    paths: list[str] = []
    for line in section.splitlines():
        # a BULLET is dash/star followed by whitespace: a bare startswith
        # read a markdown horizontal rule ('---') as a bullet and raised
        # a false malformed-entry error
        if not re.match(r"\s*[-*]\s", line):
            continue
        m = _LIST_ENTRY_RE.match(line)
        if m is None or not m.group("path").strip():
            raise CompletionError(
                f"malformed File List entry: {line.strip()!r} - the gate "
                "compares paths, and an entry it cannot read would silently "
                "thin the list"
            )
        paths.append(m.group("path").strip())
    return paths


def completion_report(
    story_text: str,
    merged_files: Sequence[str],
    require_file_list: bool = True,
) -> CompletionReport:
    """Evaluate both blades with NO exemptions. The exempt-carrying seam
    is `run_completion_gate`, which reads the list from project config
    and logs the invocation - a call site choosing its own exemptions is
    exactly the unreproducible-verdict mechanism epic-47 retro F3 named
    (retirement condition 1). Ad-hoc callers get the strict gate only."""
    return _completion_report(
        story_text, merged_files, exempt=(), require_file_list=require_file_list
    )


def run_completion_gate(
    project_root: Path | str,
    cfg: "object",
    story_file: Path | str,
    merge_sha: str,
    runlog: "object",
    require_file_list: bool = True,
) -> CompletionReport:
    """THE production completion gate (retirement condition 1): exempt
    prefixes come from `.omater.yaml` `completion.exempt` (already
    normalized on the config object), the inputs and the exempt list
    actually used ride into the run log BEFORE the verdict is returned,
    and there is no argument through which a driver can widen the
    exemptions for one call."""
    root = Path(project_root)
    try:
        raw_exempt = cfg.completion_exempt
    except AttributeError as exc:
        # typed, not defaulted: a cfg without the field is a wrong OBJECT
        # (not a project that declared no exemptions), and silently running
        # the strict gate against it would hide the caller's bug
        raise CompletionError(
            "cfg has no completion_exempt - pass a loaded ProjectConfig"
        ) from exc
    if not isinstance(raw_exempt, (list, tuple)) or not all(
        isinstance(e, str) for e in raw_exempt
    ):
        raise CompletionError(
            f"cfg.completion_exempt must be a sequence of strings, got {raw_exempt!r}"
        )
    exempt = tuple(raw_exempt)
    story_path = Path(story_file)
    if not story_path.is_absolute():
        story_path = root / story_path
    try:
        story_text = story_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CompletionError(f"cannot read story file {story_path}: {exc}") from exc
    merged = merged_files_of(root, merge_sha)
    report = _completion_report(
        story_text, merged, exempt=exempt, require_file_list=require_file_list
    )
    # One event carrying the inputs, the exempt list USED (not the
    # config's state at some later read), and the verdict.
    runlog.event(
        "gate",
        "completion-gate",
        {
            "story_file": str(story_path),
            "merge_sha": merge_sha,
            "exempt": list(exempt),
            "merged_files": len(merged),
            "require_file_list": require_file_list,
            **report.as_dict(),
        },
    )
    return report


def _completion_report(
    story_text: str,
    merged_files: Sequence[str],
    *,
    exempt: Sequence[str] = (),
    require_file_list: bool = True,
) -> CompletionReport:
    """Evaluate both blades against the story file's text and the ACTUAL
    merged file set (use `merged_files_of` to read it from git). Module
    private: `exempt` is config-owned state, reachable in production only
    through `run_completion_gate`."""
    cleaned = [p.strip() for p in merged_files if p and p.strip()]
    if not cleaned:
        # same contract as the surface classifier: an empty changeset is a
        # broken lookup, and judging paperwork against nothing would pass
        # any File List at all
        raise CompletionError(
            "no merged files supplied - refusing to judge a File List "
            "against an empty changeset (indistinguishable from a broken "
            "lookup)"
        )
    report = CompletionReport()

    tasks = _section(story_text, TASKS_HEADING_RE)
    if tasks is None:
        report.problems.append(
            "no `## Tasks / Subtasks` section found - the gate cannot see "
            "the task boxes, and unseen boxes must not read as ticked"
        )
    else:
        for m in _UNCHECKED_RE.finditer(tasks):
            report.unchecked.append(m.group("text").strip())
        if report.unchecked:
            report.problems.append(
                f"{len(report.unchecked)} unchecked task box(es) - a story "
                "cannot flip done while its own checklist says otherwise: "
                + "; ".join(f"[ ] {t}" for t in report.unchecked[:5])
                + (" ..." if len(report.unchecked) > 5 else "")
            )

    list_section = _section(story_text, FILE_LIST_HEADING_RE)
    if list_section is None:
        if require_file_list:
            report.problems.append(
                "no `### File List` section found - 'no list' and 'list "
                "agrees with the merge' must never read the same; add the "
                "section (or run with require_file_list=False if this "
                "project's template genuinely does not mandate one)"
            )
    else:
        listed = {p for p in file_list_paths(list_section) if not _exempt(p, exempt)}
        merged = {p for p in cleaned if not _exempt(p, exempt)}
        report.missing_from_list = sorted(merged - listed)
        report.phantom_in_list = sorted(listed - merged)
        if report.missing_from_list:
            report.problems.append(
                "merged but not in the File List: "
                + ", ".join(report.missing_from_list)
            )
        if report.phantom_in_list:
            report.problems.append(
                "in the File List but not in the merge: "
                + ", ".join(report.phantom_in_list)
            )
    return report


def merged_files_of(repo: Path | str, sha: str) -> list[str]:
    """The merge commit's changed files, from git itself - the gate diffs
    the actual changeset, so this is the only supported source."""
    try:
        proc = subprocess.run(
            # quotepath=false: with it on (the default), git backslash-
            # escapes non-ASCII filenames, which would falsely mismatch a
            # File List carrying the real name. Sorted so the caller sees
            # a deterministic list regardless of git's emit order. The
            # timeout keeps a hung git (FS trouble, credential prompt)
            # from stalling the whole done-flip.
            [
                "git", "-c", "core.quotepath=false",
                "show", "--name-only", "--no-renames", "--format=", sha,
            ],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CompletionError(f"cannot run git in {repo}: {exc}") from exc
    if proc.returncode != 0:
        raise CompletionError(
            f"git show {sha} failed in {repo}: {proc.stderr.strip()}"
        )
    return sorted(line for line in proc.stdout.splitlines() if line.strip())
