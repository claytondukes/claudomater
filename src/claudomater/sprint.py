"""Sprint tracking: the DB is the writer, `sprint-status.yaml` is a
byte-exact write-through export (design §Phase 2, "Sprint bridge").

The file this exports is not a data file that happens to have comments —
it is a hand-curated audit record that happens to carry a status map. In
ui3's copy, 745 of 1328 lines are prose: a rules preamble, a status
vocabulary with the reasoning behind each value, per-line justifications,
and a STRUCTURAL CHANGE LOG whose dated entries are the only record of
why epics were re-sliced. A regenerating exporter that serialized the
status map back out would destroy all of it and read as success.

So the export is not "render the DB as YAML". It is **rewrite exactly the
status tokens that changed, and nothing else** — implemented as a span
model rather than a formatter:

- Every source line is kept as its RAW text, including its line ending.
- A recognized entry line additionally records the (start, end) character
  offsets of its status token within that raw text.
- Rendering is `"".join(raw)`. Byte-exactness for an unchanged document
  is therefore STRUCTURAL, not a property the formatter has to re-derive
  and the tests have to chase.
- Flipping a status is `raw[:start] + new + raw[end:]`, so indentation,
  separator spacing, the trailing inline comment and the exact number of
  spaces before its `#` all survive by construction — there is no code
  path that could reformat them.

READING NEVER VALIDATES A STATUS VALUE. `optional` was banned as a retro
status in 2026-08-21, but historical `optional` lines are audit trail: an
exporter that "corrected" them on the way through would silently rewrite
history to match today's rules. The vocabulary below gates WRITES only.
"""

from __future__ import annotations

import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from claudomater.learnstore import LearnStore, utc_now

# Write-side vocabulary, per the file's own "Status definitions" preamble.
# A value already in the file is NEVER checked against this (see module
# docstring): these gate what the pipeline is allowed to WRITE.
EPIC_STATUSES = ("backlog", "in-progress", "done")
STORY_STATUSES = (
    "backlog",
    "ready-for-dev",
    "in-progress",
    "review",
    "done",
    "deferred",
    "scrapped",
    "superseded",
)
RETRO_STATUSES = ("fable-review-required", "done")
_WRITABLE: dict[str, tuple[str, ...]] = {
    "epic": EPIC_STATUSES,
    "story": STORY_STATUSES,
    "retro": RETRO_STATUSES,
}

DATA_BLOCK_KEY = "development_status"
_RETRO_SUFFIX = "-retrospective"

# An entry is `<indent><key>:<gap><status>[<spaces>#<comment>]<eol>`. The
# status token is `[^\s#]+` — a value carrying whitespace is NOT a status
# map entry and is refused (loudly) rather than half-parsed.
#
# The EOL alternatives are exactly `\r\n` and `\n`. A BARE `\r` must not
# qualify: paired with a line splitter that breaks on it, `epic-1: do\rne`
# parsed as status 'do' and a later flip rewrote that span into
# `epic-1: done\rne` — silent corruption of a curated document.
_ENTRY_RE = re.compile(
    r"^(?P<indent>[ \t]+)"
    r"(?P<key>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r":(?P<gap>[ \t]+)"
    r"(?P<status>[^\s#]+)"
    r"(?P<trailer>[ \t]+#[^\r\n]*)?"
    r"(?P<eol>\r?\n)?$"
)
_TOP_KEY_RE = re.compile(r"^(?P<key>[A-Za-z0-9_][A-Za-z0-9._-]*):")
# `\r?` before the end: in a CRLF file a blank line IS `\r\n`, and reading
# it as a dedent would end the data block and silently drop every entry
# after it.
_BLANK_OR_COMMENT_RE = re.compile(r"^[ \t]*(?:#|\r?$)")


def _split_keepends(text: str) -> list[str]:
    """Split on `\\n` ONLY, keeping line endings.

    `str.splitlines()` also breaks on `\\r`, `\\x0b`, `\\x0c`, `\\x1c`-`\\x1e`,
    U+0085, U+2028 and U+2029. Any of those appearing inside a status map
    would cut one source line into two, and the leading fragment would
    parse as a complete entry — recording a truncated status and leaving
    the remainder to be reinterpreted. Splitting only on `\\n` keeps such a
    character inside its line, where the entry regex refuses it and the
    parse fails loudly.
    """
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:  # trailing text with no final newline
        lines.append(parts[-1])
    return lines


class SprintError(Exception):
    pass


# EVERY read in this module goes through here, so newline handling is
# decided in one place (the matching write side is `_write_atomically`).
# `newline=""` disables universal-newline translation: without it
# `read_text()` silently turns a CRLF file into LF on every platform,
# rewriting every line of a file this module promises not to touch. That
# bug was invisible to an in-memory test AND to `round_trip_ok`, which
# compared normalized text to normalized text and so returned True while
# the on-disk bytes changed.
def _read_exact(path: Path) -> str:
    with path.open("r", encoding="utf-8", newline="") as fh:
        return fh.read()


@dataclass(frozen=True)
class SprintEntry:
    """One status-map line: `34-36-timeseries-chart-click-semantics: backlog`."""

    key: str
    status: str
    kind: str  # 'epic' | 'story' | 'retro'
    epic: str  # '' for project-scoped rows
    line_no: int  # 1-based, so errors point at the source line


@dataclass(frozen=True)
class _Line:
    raw: str
    entry: SprintEntry | None = None
    span: tuple[int, int] | None = None  # status offsets within `raw`


def _classify(key: str) -> str:
    # retro BEFORE epic: `epic-34-retrospective` is both prefixed and
    # suffixed, and it is a retro line
    if key.endswith(_RETRO_SUFFIX):
        return "retro"
    if key.startswith("epic-"):
        return "epic"
    return "story"


def _epic_of(key: str, kind: str, current_epic: str) -> str:
    """Which epic a line belongs to.

    Derived from the KEY where the key states it, and from DOCUMENT
    POSITION otherwise — because a story key genuinely cannot be parsed.
    ui3 has a sub-epic `epic-4-5` whose stories are `4-5-1-...`, so
    `4-5-1-ec-api-research` is ambiguous between "epic 4, story 5-1" and
    "epic 4-5, story 1" from the key alone; it also has `34-T-board-truth
    -up-pass-2`, whose second segment is not a number at all. The file
    expresses grouping positionally (an `epic-N` line, then its stories,
    then its retro line), so that is what gets read.
    """
    if key == "project" or key.startswith("project-"):
        return ""  # project-scoped: the whole-project retro is not epic 46's
    if kind == "retro" and key.startswith("epic-"):
        return key[len("epic-") : -len(_RETRO_SUFFIX)]
    if kind == "epic":
        return key[len("epic-") :]
    return current_epic


class SprintDoc:
    """A parsed sprint-status.yaml that renders back byte-exactly."""

    def __init__(self, lines: tuple[_Line, ...]) -> None:
        self._lines = lines
        self._index = {
            line.entry.key: i for i, line in enumerate(lines) if line.entry is not None
        }

    @classmethod
    def parse(cls, text: str) -> "SprintDoc":
        raws = _split_keepends(text)
        lines: list[_Line] = []
        seen: dict[str, int] = {}
        in_block = False
        block_indent = 0
        current_epic = ""
        for i, raw in enumerate(raws):
            line_no = i + 1
            if not in_block:
                top = _TOP_KEY_RE.match(raw)
                if top is not None and top.group("key") == DATA_BLOCK_KEY:
                    in_block = True
                    block_indent = len(raw) - len(raw.lstrip(" \t"))
                lines.append(_Line(raw))
                continue
            if _BLANK_OR_COMMENT_RE.match(raw):
                lines.append(_Line(raw))
                continue
            indent = len(raw) - len(raw.lstrip(" \t"))
            if indent <= block_indent:
                # dedent back to the block's own level ends the data block
                in_block = False
                lines.append(_Line(raw))
                continue
            m = _ENTRY_RE.match(raw)
            if m is None:
                # A line inside the status map that is not a status-map
                # entry is a SILENT DROP waiting to happen: it would render
                # back fine but never reach the DB, so the two would
                # disagree with nothing to show for it.
                raise SprintError(
                    f"line {line_no}: unparseable entry inside "
                    f"{DATA_BLOCK_KEY!r}: {raw.rstrip()!r}"
                )
            key = m.group("key")
            if key in seen:
                raise SprintError(
                    f"line {line_no}: duplicate key {key!r} "
                    f"(first seen at line {seen[key]})"
                )
            seen[key] = line_no
            kind = _classify(key)
            epic = _epic_of(key, kind, current_epic)
            if kind == "epic":
                current_epic = epic
            lines.append(
                _Line(
                    raw,
                    SprintEntry(key, m.group("status"), kind, epic, line_no),
                    (m.start("status"), m.end("status")),
                )
            )
        return cls(tuple(lines))

    @classmethod
    def read(cls, path: Path) -> "SprintDoc":
        return cls.parse(_read_exact(path))

    def render(self) -> str:
        return "".join(line.raw for line in self._lines)

    @property
    def entries(self) -> tuple[SprintEntry, ...]:
        return tuple(
            line.entry for line in self._lines if line.entry is not None
        )

    def entry(self, key: str) -> SprintEntry:
        i = self._index.get(key)
        if i is None:
            raise SprintError(f"no such key in the status map: {key!r}")
        line = self._lines[i]
        assert line.entry is not None  # indexed only when the entry exists
        return line.entry

    def statuses(self) -> dict[str, str]:
        return {e.key: e.status for e in self.entries}

    def with_statuses(self, updates: Mapping[str, str]) -> "SprintDoc":
        """A new doc with ONLY these keys' status tokens substituted.

        Every other byte of every line — including the flipped lines' own
        indentation, separator, inline comment and comment spacing — is
        carried through untouched, because only the status span is
        replaced.
        """
        if not updates:
            return self
        lines = list(self._lines)
        for key, status in updates.items():
            i = self._index.get(key)
            if i is None:
                raise SprintError(f"no such key in the status map: {key!r}")
            line = lines[i]
            assert line.entry is not None and line.span is not None
            if status == line.entry.status:
                continue
            _validate_status(line.entry.kind, status, key)
            start, end = line.span
            raw = line.raw[:start] + status + line.raw[end:]
            lines[i] = _Line(
                raw,
                SprintEntry(
                    key, status, line.entry.kind, line.entry.epic, line.entry.line_no
                ),
                (start, start + len(status)),
            )
        return SprintDoc(tuple(lines))


def _validate_status(kind: str, status: str, key: str) -> None:
    allowed = _WRITABLE[kind]
    if status not in allowed:
        raise SprintError(
            f"{status!r} is not a writable {kind} status for {key!r} "
            f"(allowed: {', '.join(allowed)})"
        )


# ---- the DB side -------------------------------------------------------


def orphaned_keys(store: LearnStore, project: str, doc: SprintDoc) -> list[str]:
    """Keys the DB tracks that the document no longer carries.

    A divergence someone has to decide about, which is why it is reported
    rather than resolved: see `import_doc`'s `prune`.
    """
    known = doc.statuses()
    return sorted(k for k in statuses(store, project) if k not in known)


def import_doc(
    store: LearnStore, project: str, doc: SprintDoc, prune: bool = False
) -> int:
    """Seed/refresh the `story` rows from the file. Returns the number of
    entries READ (not the number that changed — `updated_at` deliberately
    only moves when a row's status or epic actually changed, so it keeps
    meaning "when this status last changed" across repeated imports).

    The file is the seed, so its values land VERBATIM — a legacy
    `optional` retro line imports as `optional`, because the DB is a
    mirror of the audit record, not a corrected version of it.

    `prune` removes rows for keys the document no longer carries. It is
    OPT-IN, and deliberately not the default: the DB is on its way to
    being the writer, so dropping its rows because a DERIVED artifact
    lost a line is backwards, and an accidentally truncated file would
    delete real tracking without a word. Without it, orphans are
    reported (`orphaned_keys`) and `export` refuses until a human
    decides — the divergence stays visible instead of being resolved by
    whichever side was read most recently.
    """
    now = utc_now()
    rows = [(project, e.key, e.epic, e.status, now) for e in doc.entries]
    with store.conn:
        store.conn.executemany(
            "INSERT INTO story(project, key, epic, status, updated_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(project, key) DO UPDATE SET "
            "epic=excluded.epic, status=excluded.status, "
            "updated_at=excluded.updated_at WHERE story.status != excluded.status "
            "OR story.epic != excluded.epic",
            rows,
        )
        if prune:
            stale = orphaned_keys(store, project, doc)
            store.conn.executemany(
                "DELETE FROM story WHERE project = ? AND key = ?",
                [(project, key) for key in stale],
            )
    return len(rows)


def _write_atomically(path: Path, text: str) -> None:
    """Replace `path`'s content in one step, keeping its mode.

    The target is a curated document under version control: a partial
    write from a full disk or a crash mid-write would corrupt an audit
    record that the tool cannot reconstruct. The temp file is created in
    the SAME directory so `os.replace` is a real atomic rename rather
    than a cross-device copy.

    The temp name is UNIQUE per call, not `.{name}.omater-tmp`: a fixed
    name is shared state between processes, so a second writer staging to
    it could overwrite the first writer's bytes (making the first
    writer's `os.replace` publish content it never wrote) or rename it
    away first (making the first writer fail for no real reason).
    """
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".omater-tmp"
    )
    tmp = Path(tmp_name)
    try:
        # newline="" for the same reason `_read_exact` uses it: the text
        # already carries the source's own line endings, and translating
        # them here would rewrite every line
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.chmod(tmp, stat.S_IMODE(path.stat().st_mode))
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def statuses(store: LearnStore, project: str) -> dict[str, str]:
    cur = store.conn.execute(
        "SELECT key, status FROM story WHERE project = ? ORDER BY key", (project,)
    )
    return {key: status for key, status in cur.fetchall()}


def stories(
    store: LearnStore, project: str, epic: str | None = None
) -> list[dict[str, str]]:
    """The on-demand sprint view (design §6: rendered from the tables)."""
    sql = "SELECT key, epic, status, updated_at FROM story WHERE project = ?"
    params: list[str] = [project]
    if epic is not None:
        sql += " AND epic = ?"
        params.append(epic)
    sql += " ORDER BY key"
    cur = store.conn.execute(sql, params)
    return [
        {"key": k, "epic": e, "status": s, "updated_at": u}
        for k, e, s, u in cur.fetchall()
    ]


def set_status(
    store: LearnStore, project: str, key: str, status: str, path: Path
) -> bool:
    """Write a status to the DB, then write through to the export.

    Write-through is not a convenience here: the yaml is what ui3's
    CLAUDE.md gates and skills still read, so a DB write that did not
    reach the file would be invisible to every consumer that matters
    until cutover. Validated against the file's own vocabulary BEFORE
    the DB write, so a rejected value never lands anywhere.

    The export runs INSIDE the transaction: if the file cannot be
    written, the DB write rolls back rather than leaving the writer and
    its export disagreeing about what the sprint says.
    """
    entry = SprintDoc.read(path).entry(key)  # raises if the file lacks the key
    _validate_status(entry.kind, status, key)
    with store.conn:
        # Existence is checked by READING the row, not by inspecting an
        # UPDATE's rowcount: it separates "this key is not tracked" from
        # "this write changed nothing", which a rowcount cannot do.
        row = store.conn.execute(
            "SELECT status FROM story WHERE project = ? AND key = ?", (project, key)
        ).fetchone()
        if row is None:
            raise SprintError(
                f"{key!r} is not tracked for project {project!r} — "
                "run `omater sprint import` first"
            )
        if row["status"] != status:
            # `updated_at` means "when this status last changed" — the
            # same meaning `import_doc` keeps. Bumping it for a write that
            # changed nothing would make the two writers disagree about
            # what the column records.
            store.conn.execute(
                "UPDATE story SET status = ?, updated_at = ? "
                "WHERE project = ? AND key = ?",
                (status, utc_now(), project, key),
            )
        # exported unconditionally: an unchanged DB does not mean the FILE
        # agrees with it, and write-through is what reconciles a hand edit
        return export(store, project, path)


def export(store: LearnStore, project: str, path: Path) -> bool:
    """Write the DB's statuses through to the file. Returns True if the
    bytes changed.

    A key the DB tracks but the file does not carry is a LOUD failure,
    never a silent skip and never an appended line: choosing where a new
    story belongs (which epic block, above which retro line) is a
    sprint-planning decision this exporter has no basis to make, and
    guessing it would corrupt a curated document. Adding lines stays with
    the planning tool until the yaml retires.
    """
    # one read: the same bytes are both parsed and compared against, so
    # the "did anything change" answer cannot be about a different revision
    # of the file than the one that was rewritten
    source = _read_exact(path)
    doc = SprintDoc.parse(source)
    db = statuses(store, project)
    if not db:
        raise SprintError(
            f"no tracked stories for project {project!r} — "
            "`omater sprint import` seeds the DB from the file"
        )
    known = doc.statuses()
    missing = sorted(k for k in db if k not in known)
    if missing:
        raise SprintError(
            f"tracked but absent from {path.name}: {', '.join(missing)} — "
            "the exporter rewrites status tokens, it never adds lines. If "
            "those keys were deliberately removed from the file, clear "
            "them with `omater sprint import --prune`"
        )
    updates = {k: v for k, v in db.items() if known[k] != v}
    rendered = doc.with_statuses(updates).render()
    if rendered == source:
        return False
    _write_atomically(path, rendered)
    return True


def round_trip_ok(path: Path) -> bool:
    """Parse-then-render equals the source bytes. The proof that the line
    model preserves a document it did not author."""
    text = _read_exact(path)
    return SprintDoc.parse(text).render() == text


def import_path(
    store: LearnStore, project: str, path: Path, prune: bool = False
) -> int:
    return import_doc(store, project, SprintDoc.read(path), prune=prune)


def unknown_statuses(doc: SprintDoc) -> list[SprintEntry]:
    """Entries whose value is outside the WRITE vocabulary for their kind.

    Reporting, not enforcement: these are exactly the legacy rows
    (`optional` retros) the export must carry through untouched, so the
    tool surfaces them for a human instead of correcting them.
    """
    return [e for e in doc.entries if e.status not in _WRITABLE[e.kind]]
