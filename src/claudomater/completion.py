"""Completion-integrity gate: a story cannot flip `done` while its own
paperwork disagrees with reality. (Phase 3 deliverable 4; epic-46 retro
action A5.)

Evidence of record: a story flipped `done` with three top-level tasks
unchecked, their sub-items unexecuted, and five pieces of post-merge
bookkeeping genuinely not done - the run session narrated completion and
nothing diffed the narration against the file. This gate reads the story
file and the ACTUAL merged changeset; narration never satisfies it.

Three blades, all fail-closed (the third is an opt-in):

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

3. LAB RECORD VOCABULARY (`completion.lab_record: required`) - the
   `### Merge and lab record` must say, in the literal words, what the
   lab arms induced and what they did not: every bullet item in the
   section that names an arm (the word `arm`) carries `induced`,
   `not induced`, `not inducible` or `STATED`, the section as a whole
   carries at least one positive `induced` and at least one negative
   disposition (or says every arm was induced / no runtime arm exists),
   and a missing section blocks. Four epics of close reviews found the
   line carried by discipline alone under an epic sentence promising this
   gate. Off by default: a project opts in per `.omater.yaml`.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

TASKS_HEADING_RE = re.compile(r"^##\s+Tasks(\s*/\s*Subtasks)?\s*$", re.MULTILINE)
FILE_LIST_HEADING_RE = re.compile(r"^###\s+File List\s*$", re.MULTILINE)
# The operator's post-merge record. Its heading carries a suffix in the
# records seen so far ("(operator, 2026-09-30)"), so the match stops at
# the words.
LAB_RECORD_HEADING_RE = re.compile(r"^###\s+Merge and lab record\b.*$", re.MULTILINE)
# The literal disposition vocabulary a lab record must use (epic-64 retro
# A3, epic-65 A4, epic-66 A6, epic-63 F5: four epics of records that
# carried it by discipline alone). `induced` must appear, and the record
# must say what was NOT induced - or that nothing was left un-induced.
# A positive `induced` is one not preceded by `not` / `not-` / `un-`:
# "not induced: the port arm" must never satisfy the positive side.
_INDUCED_RE = re.compile(r"(?<!\bnot )(?<!\bnot-)(?<!\bun-)\binduced\b", re.IGNORECASE)
_NOT_INDUCED_RE = re.compile(
    r"\bnot induced\b|\bnot-induced\b|\bnot inducible\b|\bSTATED\b",
    re.IGNORECASE,
)
# The whole-record statements that stand in for a negative line (nothing
# was left un-induced, or there is no runtime arm at all).
_ALL_INDUCED_RE = re.compile(
    r"\bevery arm (?:was )?induced\b|\bno (?:non-inducible|not-induced) arm\b"
    r"|\bno runtime arm\b|\ball arms induced\b",
    re.IGNORECASE,
)
_ARM_WORD_RE = re.compile(r"\barms?\b", re.IGNORECASE)
_DISPOSITION_RE = re.compile(
    r"(?<!\bnot )(?<!\bnot-)(?<!\bun-)\binduced\b|\bnot induced\b|\bnot-induced\b"
    r"|\bnot inducible\b|\bSTATED\b",
    re.IGNORECASE,
)
_BULLET_RE = re.compile(r"^\s*[-*]\s")


def _bullet_items(section: str) -> list[str]:
    """The section's bullet items, each with its indented continuation
    lines joined; prose paragraphs are not items."""
    items: list[str] = []
    current: list[str] | None = None
    for line in section.splitlines():
        if _BULLET_RE.match(line):
            if current:
                items.append(" ".join(current))
            current = [line.strip()]
        elif current is not None and line.strip() and line[:1].isspace():
            current.append(line.strip())
        else:
            if current:
                items.append(" ".join(current))
            current = None
    if current:
        items.append(" ".join(current))
    return items


def lab_record_problems(section: str) -> tuple[list[str], list[str]]:
    """(missing, problems) for a lab-record section under `required`:
    every bullet item that names an arm is judged on its own and a
    dispositionless one is named; then the section as a whole must carry
    a positive `induced` and a negative disposition, unless it says every
    arm was induced or no runtime arm exists."""
    missing: list[str] = []
    problems: list[str] = []
    undisposed = [
        item for item in _bullet_items(section)
        if _ARM_WORD_RE.search(item) and not _DISPOSITION_RE.search(item)
    ]
    for item in undisposed:
        missing.append(f"arm:{item[:60]}")
    if undisposed:
        problems.append(
            "lab-record arm entries without an induced / not-induced disposition: "
            + "; ".join(repr(i[:60]) for i in undisposed[:5])
            + (" ..." if len(undisposed) > 5 else "")
        )
    all_induced = bool(_ALL_INDUCED_RE.search(section))
    if not _INDUCED_RE.search(section) and not all_induced:
        missing.append("induced")
    if not _NOT_INDUCED_RE.search(section) and not all_induced:
        missing.append("not-induced")
    words = [m for m in missing if not m.startswith("arm:")]
    if words:
        problems.append(
            "the `### Merge and lab record` lacks the literal induced / "
            "not-induced line: missing " + ", ".join(words)
            + " (say per failure arm `induced` or `not induced` / `not inducible` "
            "/ `STATED`, or that every arm was induced)"
        )
    return missing, problems
LAB_RECORD_MODES = ("off", "required")
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


def normalize_lab_record(value: object) -> str:
    """The `completion.lab_record` mode from config: `off` (default; the
    record is not judged) or `required` (the finish refuses a story whose
    `### Merge and lab record` lacks the literal induced / not-induced
    vocabulary). Anything else is a config error, never a silent off."""
    if value is None:
        return "off"
    # YAML 1.1 parses bare off/on as booleans - the documented values must
    # work unquoted, so map them back before validating (merge.converge
    # does the same).
    if value is False:
        return "off"
    if value is True:
        return "required"
    if not isinstance(value, str) or value not in LAB_RECORD_MODES:
        raise CompletionError(
            f"completion.lab_record must be one of {LAB_RECORD_MODES}, got {value!r}"
        )
    return value


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
    """The gate's verdict with its evidence. `ok` is True only when no
    blade found anything (the lab-record blade contributes only when the
    project opted in)."""

    unchecked: list[str] = field(default_factory=list)
    missing_from_list: list[str] = field(default_factory=list)
    lab_record_missing: list[str] = field(default_factory=list)
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
            "lab_record_missing": self.lab_record_missing,
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
        lab_record = cfg.completion_lab_record
    except AttributeError as exc:
        # typed, not defaulted: a cfg without the field is a wrong OBJECT
        # (not a project that declared no exemptions), and silently running
        # the strict gate against it would hide the caller's bug
        raise CompletionError(
            "cfg has no completion_exempt / completion_lab_record - pass a "
            "loaded ProjectConfig"
        ) from exc
    lab_record = normalize_lab_record(lab_record)
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
        story_text,
        merged,
        exempt=exempt,
        require_file_list=require_file_list,
        lab_record=lab_record,
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
            "lab_record": lab_record,
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
    lab_record: str = "off",
) -> CompletionReport:
    """Evaluate the blades against the story file's text and the ACTUAL
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

    if normalize_lab_record(lab_record) == "required":
        # The third blade: the post-merge record must say, in the literal
        # words, what the lab arms induced and what they did not. The
        # engine cannot enumerate the arms (they are prose), so it holds
        # the vocabulary: a record with no `induced` at all, or one that
        # never says what was NOT induced (or that nothing was left
        # un-induced), is a record a later reader cannot trust.
        lab = _section(story_text, LAB_RECORD_HEADING_RE)
        if lab is None:
            report.lab_record_missing.append("section")
            report.problems.append(
                "no `### Merge and lab record` section found - the finish "
                "cannot judge a lab record it cannot see"
            )
        else:
            missing, problems = lab_record_problems(lab)
            report.lab_record_missing.extend(missing)
            report.problems.extend(problems)
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
