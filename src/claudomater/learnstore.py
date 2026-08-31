"""Learning store (design §6): a local SQLite index + deterministic JSONL
exports carried in git.

The division of authority is the point:

- the **JSONL export is the source of truth** — one file per scope at
  `learning.export_path`, rows sorted, byte-reproducible, written through on
  every DB write. A binary DB in git merges as ours/theirs and silently
  discards one machine's lessons; text lines make a cross-machine conflict a
  visible one-line git conflict instead.
- the **SQLite file is a LOCAL rebuildable index** (`learning.db_path`, never
  committed): FTS retrieval and the volatile `refs`/`sessions` counters live
  here. Counters stay OUT of the export on purpose — every use of a hot
  lesson would otherwise churn the export and make conflicts the common case.
- **lesson content is scrubbed on every write** (Clay's Phase 2 rider): the
  corpus outlives the run that produced it, so a lesson must never carry
  customer hostnames, tokens, or other deny-listed values — `scrub_text`
  runs against the store's `secrets_deny` before anything touches the DB.

Write classification is explicit (design: "on conflict the writer must
classify"): `add` refuses an existing live key, `refine` merges into the
existing row, `supersede` writes a NEW row and points the old one at it
(`status=superseded`, `superseded_by`). One schema deviation from the design
doc's declared DDL, documented here because it is forced by the design's own
semantics: the doc declares `UNIQUE (scope, domain, topic)` AND supersession
that keeps both generations under the same key — those cannot both hold, so
uniqueness is enforced as a partial index over LIVE rows only
(`status IN ('active','promoted')`), which is what the supersession audit
trail actually requires.

Import identity is `(scope, domain, topic, created_at)` — a generation —
with latest `updated_at` winning mutable fields, and `superseded_by` links
relinked from generation order after every import (local row ids never
travel between machines; chains are reconstructed, not copied).
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from claudomater.scrub import scrub_text

LIVE_STATUSES = ("active", "promoted")
STATUSES = ("active", "promoted", "superseded")

# Content + lifecycle only. refs/sessions are volatile local counters and
# never export (rev 3 export hygiene); id/superseded_by are machine-local.
EXPORT_FIELDS = (
    "scope",
    "domain",
    "topic",
    "rule",
    "why",
    "status",
    "created_at",
    "updated_at",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS lesson (
  id            INTEGER PRIMARY KEY,
  scope         TEXT NOT NULL,
  domain        TEXT NOT NULL,
  topic         TEXT NOT NULL,
  rule          TEXT NOT NULL,
  why           TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'active',
  superseded_by INTEGER REFERENCES lesson(id),
  refs          INTEGER NOT NULL DEFAULT 0,
  sessions      INTEGER NOT NULL DEFAULT 1,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
-- Uniqueness over LIVE rows only (see module docstring: the design's
-- supersession semantics require old generations to keep their key).
CREATE UNIQUE INDEX IF NOT EXISTS lesson_live_key
  ON lesson(scope, domain, topic) WHERE status IN ('active','promoted');
-- FTS: external-content tables require sync triggers or the index silently
-- rots (the refine path REWRITES rule text; without delete-then-insert
-- trigger handling, queries return ghosts). Declared in full (design §6).
CREATE VIRTUAL TABLE IF NOT EXISTS lesson_fts
  USING fts5(rule, why, content=lesson, content_rowid=id);
CREATE TRIGGER IF NOT EXISTS lesson_ai AFTER INSERT ON lesson BEGIN
  INSERT INTO lesson_fts(rowid, rule, why) VALUES (new.id, new.rule, new.why);
END;
CREATE TRIGGER IF NOT EXISTS lesson_ad AFTER DELETE ON lesson BEGIN
  INSERT INTO lesson_fts(lesson_fts, rowid, rule, why)
    VALUES ('delete', old.id, old.rule, old.why);
END;
CREATE TRIGGER IF NOT EXISTS lesson_au AFTER UPDATE ON lesson BEGIN
  INSERT INTO lesson_fts(lesson_fts, rowid, rule, why)
    VALUES ('delete', old.id, old.rule, old.why);
  INSERT INTO lesson_fts(rowid, rule, why) VALUES (new.id, new.rule, new.why);
END;

-- sprint tracking lives here too (ratified: DB, not an ever-growing yaml);
-- written by slice C, created with the schema so one open() owns the DDL
CREATE TABLE IF NOT EXISTS story (
  project    TEXT NOT NULL,
  key        TEXT NOT NULL,
  epic       TEXT NOT NULL,
  status     TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (project, key)
);
CREATE TABLE IF NOT EXISTS run_event (
  id         INTEGER PRIMARY KEY,
  project    TEXT NOT NULL,
  run_id     TEXT NOT NULL,
  story_key  TEXT,
  phase      TEXT NOT NULL,
  event      TEXT NOT NULL,
  detail     TEXT,
  created_at TEXT NOT NULL
);
"""


class LearnStoreError(Exception):
    pass


# Fixed-width microseconds: created_at is the generation IDENTITY on
# import and both timestamps drive deterministic ordering (export sort,
# winner selection, chain order) via LEXICOGRAPHIC comparison — which is
# only chronologically sound when every value has the same width. Plain
# 1-second resolution let same-key writes inside one second collide the
# identity and tie the sort.
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z")


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime(TIMESTAMP_FORMAT)


def _validate_timestamp(value: str) -> bool:
    """Fixed-width (regex) AND a real calendar instant (strptime — %f alone
    would accept 1-5 fractional digits, breaking lexicographic order)."""
    if not _TIMESTAMP_RE.fullmatch(value):
        return False
    try:
        datetime.strptime(value, TIMESTAMP_FORMAT)
    except ValueError:
        return False
    return True


# scope names map 1:1 to export filenames, so they must BE filename-safe —
# sanitizing would let distinct scopes ("a/b", "a?b") collide onto one file
# and silently mix corpora
_SCOPE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _validate_scope(scope: str, context: str = "") -> None:
    if not _SCOPE_RE.fullmatch(scope):
        prefix = f"{context}: " if context else ""
        raise LearnStoreError(
            f"{prefix}scope {scope!r} is not filename-safe (alphanumeric "
            "start, then [A-Za-z0-9._-]); scope names map 1:1 to export files"
        )


def _scope_filename(scope: str) -> str:
    _validate_scope(scope)
    return scope + ".jsonl"


@dataclass
class ImportStats:
    new: int = 0
    updated: int = 0
    unchanged: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"new": self.new, "updated": self.updated, "unchanged": self.unchanged}


class LearnStore:
    def __init__(
        self,
        conn: sqlite3.Connection,
        export_dir: Path | None = None,
        secrets_deny: Sequence[str] = (),
        now: Callable[[], str] = _utc_now,
    ):
        self.conn = conn
        self.export_dir = export_dir
        self.secrets_deny = tuple(secrets_deny)
        self.now = now

    # ---- lifecycle ---------------------------------------------------------

    @classmethod
    def open(
        cls,
        db_path: Path | str,
        export_dir: Path | str | None = None,
        secrets_deny: Sequence[str] = (),
        now: Callable[[], str] = _utc_now,
    ) -> "LearnStore":
        """Open (creating if needed) with the design's operational care:
        `PRAGMA integrity_check` at open — the DB is a rebuildable index, so
        corruption fails loudly and names the recovery (re-import from the
        JSONL source of truth) — and an FTS consistency check that rebuilds
        the index from the lesson table on any mismatch."""
        db_path = Path(db_path).expanduser()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            # off by default in SQLite: without this the declared
            # superseded_by REFERENCES is decorative and a future bug could
            # write dangling links unnoticed
            conn.execute("PRAGMA foreign_keys=ON")
            check = conn.execute("PRAGMA integrity_check").fetchone()[0]
        except sqlite3.DatabaseError as exc:
            # heavy corruption raises before integrity_check can even report
            conn.close()
            raise LearnStoreError(
                f"learning DB at {db_path} is unreadable ({exc}); it is a "
                "rebuildable local index — delete it and rebuild with "
                "`omater learn import`"
            ) from exc
        if check != "ok":
            conn.close()
            raise LearnStoreError(
                f"learning DB at {db_path} failed integrity_check ({check}); "
                "it is a rebuildable local index — delete it and rebuild "
                "with `omater learn import`"
            )
        try:
            conn.executescript(SCHEMA)
            store = cls(
                conn,
                export_dir=Path(export_dir).expanduser() if export_dir else None,
                secrets_deny=secrets_deny,
                now=now,
            )
            store._fts_check_and_rebuild()
        except sqlite3.DatabaseError as exc:
            # typed errors at the source: a schema mismatch (a conflicting
            # pre-existing table, missing FTS5) must reach CLI callers as
            # the store's own error, never a raw sqlite traceback
            conn.close()
            raise LearnStoreError(
                f"learning DB at {db_path} cannot initialize ({exc}); it is "
                "a rebuildable local index — delete it and rebuild with "
                "`omater learn import`"
            ) from exc
        except BaseException:
            # a failed open must not leak the connection (open fd, and a
            # held lock on some platforms makes recovery harder)
            conn.close()
            raise
        return store

    def _fts_check_and_rebuild(self) -> bool:
        """FTS5 'integrity-check' verifies the index against the content
        table; any mismatch (rot) triggers a full 'rebuild' from `lesson`.
        Returns True when a rebuild happened."""
        try:
            # rank=1 = ALSO verify against the external content table; the
            # one-argument form only checks the index's internal structure
            # and reports rot as healthy
            self.conn.execute(
                "INSERT INTO lesson_fts(lesson_fts, rank) "
                "VALUES ('integrity-check', 1)"
            )
            # the command INSERT implicitly opened a write transaction;
            # returning without committing would hold the write lock for
            # the store's whole lifetime and starve concurrent writers
            self.conn.commit()
            return False
        except sqlite3.DatabaseError:
            self.conn.execute(
                "INSERT INTO lesson_fts(lesson_fts) VALUES ('rebuild')"
            )
            self.conn.commit()
            return True

    def close(self) -> None:
        self.conn.close()

    # ---- helpers -----------------------------------------------------------

    def _scrub(self, text: str) -> str:
        return scrub_text(text, self.secrets_deny)

    @staticmethod
    def _validate_key(scope: str, domain: str, topic: str) -> None:
        for name, value in (("scope", scope), ("domain", domain), ("topic", topic)):
            if not isinstance(value, str) or not value.strip():
                raise LearnStoreError(f"{name} must be a non-empty string, got {value!r}")
        # fail at the write, not later at export-filename derivation
        _validate_scope(scope)

    def _live_row(self, scope: str, domain: str, topic: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM lesson WHERE scope=? AND domain=? AND topic=? "
            "AND status IN ('active','promoted')",
            (scope, domain, topic),
        ).fetchone()

    def _write_through(self) -> None:
        if self.export_dir is not None:
            self.export()

    # ---- classified write path (design: the writer must classify) ----------

    def add(self, scope: str, domain: str, topic: str, rule: str, why: str) -> int:
        """A genuinely NEW lesson. Refuses an existing live key — the caller
        must classify the conflict as refine or supersede, never silently
        overwrite a judgment.

        The topic is scrubbed ONCE at entry and that form is the key for
        lookup, storage, and error text alike: scrubbing only at insert let
        a deny-listed topic miss the live-row check, hit the unique index
        as an uncaught IntegrityError, and echo the secret in the error."""
        self._validate_key(scope, domain, topic)
        topic = self._scrub(topic)
        if self._live_row(scope, domain, topic) is not None:
            raise LearnStoreError(
                f"a live lesson already exists for ({scope}, {domain}, {topic}); "
                "classify: refine it or supersede it"
            )
        ts = self.now()
        cur = self.conn.execute(
            "INSERT INTO lesson (scope, domain, topic, rule, why, status, "
            "created_at, updated_at) VALUES (?,?,?,?,?, 'active', ?, ?)",
            (
                scope,
                domain,
                topic,
                self._scrub(rule),
                self._scrub(why),
                ts,
                ts,
            ),
        )
        self.conn.commit()
        self._write_through()
        return cur.lastrowid

    def refine(
        self,
        scope: str,
        domain: str,
        topic: str,
        rule: str | None = None,
        why: str | None = None,
    ) -> int:
        """Merge into the EXISTING row (same judgment, better wording).
        At least one of rule/why must change. The topic is normalized to
        its scrubbed form for lookup and messaging (same rule as add)."""
        self._validate_key(scope, domain, topic)
        topic = self._scrub(topic)
        row = self._live_row(scope, domain, topic)
        if row is None:
            raise LearnStoreError(
                f"no live lesson for ({scope}, {domain}, {topic}); add it first"
            )
        if rule is None and why is None:
            raise LearnStoreError("refine needs a new rule and/or why")
        self.conn.execute(
            "UPDATE lesson SET rule=?, why=?, updated_at=? WHERE id=?",
            (
                self._scrub(rule) if rule is not None else row["rule"],
                self._scrub(why) if why is not None else row["why"],
                self.now(),
                row["id"],
            ),
        )
        self.conn.commit()
        self._write_through()
        return row["id"]

    def supersede(self, scope: str, domain: str, topic: str, rule: str, why: str) -> int:
        """A NEW judgment replacing the old one: new row becomes the live
        head, the old row keeps its key as the audit trail
        (status=superseded, superseded_by -> the new row). The topic is
        normalized to its scrubbed form for lookup, storage, and messaging
        (same rule as add)."""
        self._validate_key(scope, domain, topic)
        topic = self._scrub(topic)
        old = self._live_row(scope, domain, topic)
        if old is None:
            raise LearnStoreError(
                f"no live lesson for ({scope}, {domain}, {topic}) to supersede; "
                "use add"
            )
        ts = self.now()
        # demote first: the partial unique index forbids two live rows
        self.conn.execute(
            "UPDATE lesson SET status='superseded', updated_at=? WHERE id=?",
            (ts, old["id"]),
        )
        cur = self.conn.execute(
            "INSERT INTO lesson (scope, domain, topic, rule, why, status, "
            "created_at, updated_at) VALUES (?,?,?,?,?, 'active', ?, ?)",
            (
                scope,
                domain,
                topic,
                self._scrub(rule),
                self._scrub(why),
                ts,
                ts,
            ),
        )
        self.conn.execute(
            "UPDATE lesson SET superseded_by=? WHERE id=?", (cur.lastrowid, old["id"])
        )
        self.conn.commit()
        self._write_through()
        return cur.lastrowid

    # ---- read path ---------------------------------------------------------

    def lessons(
        self, scopes: Sequence[str], domains: Sequence[str] | None = None
    ) -> list[dict[str, Any]]:
        """Live lessons (active + promoted) for the given scopes — retrieval
        never returns superseded rows (design §6)."""
        if not scopes:
            return []
        if domains is not None and not domains:
            # an EMPTY domain filter selects nothing — same contract as
            # scopes; None alone means "no domain filter"
            return []
        query = (
            "SELECT * FROM lesson WHERE status IN ('active','promoted') "
            f"AND scope IN ({','.join('?' * len(scopes))})"
        )
        params: list[Any] = list(scopes)
        if domains is not None:
            query += f" AND domain IN ({','.join('?' * len(domains))})"
            params += list(domains)
        query += " ORDER BY scope, domain, topic"
        return [dict(r) for r in self.conn.execute(query, params).fetchall()]

    def search(
        self, query: str, scopes: Sequence[str], limit: int = 20
    ) -> list[dict[str, Any]]:
        """FTS over rule+why, live rows only, ranked by refs then relevance
        (design: 'ranked by refs'). The query is passed to FTS5 as a plain
        string; a syntactically invalid query is a loud error, not a silent
        empty result."""
        if not scopes:
            return []
        try:
            rows = self.conn.execute(
                "SELECT lesson.* FROM lesson_fts JOIN lesson "
                "ON lesson.id = lesson_fts.rowid "
                "WHERE lesson_fts MATCH ? "
                "AND lesson.status IN ('active','promoted') "
                f"AND lesson.scope IN ({','.join('?' * len(scopes))}) "
                "ORDER BY lesson.refs DESC, rank LIMIT ?",
                (query, *scopes, limit),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            raise LearnStoreError(f"FTS query {query!r} failed: {exc}") from exc
        return [dict(r) for r in rows]

    # ---- deterministic export / import -------------------------------------

    def export_paths(self, export_dir: Path | None = None) -> list[Path]:
        """The canonical per-scope export file list, derived from DB scopes
        WITHOUT a serialization pass — for callers (sync staging) that need
        exactly the artifact list after a write-through export already
        produced the files. Always identical to what export() returns."""
        out_dir = export_dir or self.export_dir
        if out_dir is None:
            raise LearnStoreError("no export directory configured")
        out_dir = Path(out_dir).expanduser()
        return [
            out_dir / _scope_filename(r[0])
            for r in self.conn.execute(
                "SELECT DISTINCT scope FROM lesson ORDER BY scope"
            ).fetchall()
        ]

    def export(self, export_dir: Path | None = None) -> list[Path]:
        """Write one JSONL file per scope: rows of ALL statuses (the
        supersession audit trail is the point), content + lifecycle fields
        only, sorted by (domain, topic, created_at, updated_at), canonical
        JSON (sorted keys, no spaces). Byte-reproducible: the same corpus
        always yields the same bytes, which is what makes the git-carried
        export mergeable and the DB rebuildable."""
        out_dir = export_dir or self.export_dir
        if out_dir is None:
            raise LearnStoreError("no export directory configured")
        out_dir = Path(out_dir).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)
        written = []
        scopes = [
            r[0]
            for r in self.conn.execute(
                "SELECT DISTINCT scope FROM lesson ORDER BY scope"
            ).fetchall()
        ]
        for scope in scopes:
            rows = self.conn.execute(
                "SELECT * FROM lesson WHERE scope=? "
                "ORDER BY domain, topic, created_at, updated_at",
                (scope,),
            ).fetchall()
            lines = [
                json.dumps(
                    {field: row[field] for field in EXPORT_FIELDS},
                    sort_keys=True,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                for row in rows
            ]
            path = out_dir / _scope_filename(scope)
            content = "\n".join(lines) + "\n" if lines else ""
            # typed errors at the IO boundary: raw OSError/UnicodeDecodeError
            # would bypass the CLI's LearnStoreError handler as a traceback
            try:
                # skip no-op rewrites so write-through never churns mtimes
                if not path.exists() or path.read_text(encoding="utf-8") != content:
                    path.write_text(content, encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise LearnStoreError(
                    f"cannot write export file {path}: {exc}"
                ) from exc
            written.append(path)
        return written

    def import_dir(self, import_dir: Path | None = None) -> ImportStats:
        """Import every per-scope JSONL file: keyed upsert on the generation
        identity (scope, domain, topic, created_at), latest `updated_at`
        wins mutable fields, local counters untouched, and supersession
        chains RELINKED from generation order afterwards (row ids are
        machine-local and never travel). Ends with the write-through
        export: every DB write keeps the export current, imports included."""
        if import_dir is None and self.export_dir is None:
            # never fall back to the cwd — importing whatever directory the
            # process happened to start in is a silent wrong-corpus load
            raise LearnStoreError("no import directory configured")
        src = Path(import_dir if import_dir is not None else self.export_dir).expanduser()
        if not src.is_dir():
            raise LearnStoreError(f"no import directory at {src}")
        stats = ImportStats()
        # intended status per GENERATION: statuses cannot be applied row-by-
        # row (the live-uniqueness index forbids two live rows even
        # transiently, and a fresh import may carry several), so rows land
        # neutral and _settle_key applies intent with conflict resolution.
        intended: dict[tuple[str, str, str, str], str] = {}
        for path in sorted(src.glob("*.jsonl")):
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                # import is the untrusted boundary: an unreadable or
                # non-UTF8 file fails closed with the store's own error,
                # never a raw traceback through the CLI
                raise LearnStoreError(f"{path.name}: unreadable ({exc})") from exc
            for lineno, line in enumerate(text.splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise LearnStoreError(
                        f"{path.name}:{lineno}: not valid JSON ({exc})"
                    ) from None
                if not isinstance(row, dict) or not all(
                    isinstance(row.get(f), str) for f in EXPORT_FIELDS
                ):
                    raise LearnStoreError(
                        f"{path.name}:{lineno}: not a lesson row (fields "
                        f"{EXPORT_FIELDS} required)"
                    )
                if row["status"] not in STATUSES:
                    raise LearnStoreError(
                        f"{path.name}:{lineno}: unknown status {row['status']!r}"
                    )
                for field in ("scope", "domain", "topic"):
                    # import is the untrusted boundary and export is
                    # write-through: an empty or unsafe key would only fail
                    # LATER (export filename derivation) without location
                    if not row[field].strip():
                        raise LearnStoreError(
                            f"{path.name}:{lineno}: {field} must be non-empty"
                        )
                _validate_scope(row["scope"], context=f"{path.name}:{lineno}")
                for field in ("created_at", "updated_at"):
                    # winner selection and chain order are lexicographic
                    # comparisons that are only sound for this exact fixed-
                    # width format — a malformed or narrower timestamp would
                    # silently pick wrong winners
                    if not _validate_timestamp(row[field]):
                        raise LearnStoreError(
                            f"{path.name}:{lineno}: {field} {row[field]!r} is "
                            "not a YYYY-MM-DDTHH:MM:SS.ffffffZ timestamp"
                        )
                if row["scope"] != path.stem:
                    # one file per scope is the export contract; a misnamed
                    # file would be re-canonicalized by export and leave a
                    # stray forever-reimported file mixing corpora
                    raise LearnStoreError(
                        f"{path.name}:{lineno}: scope {row['scope']!r} does "
                        f"not match the file (one file per scope; expected "
                        f"{path.stem!r})"
                    )
                self._import_row(row, stats, intended)
        for key in {k[:3] for k in intended}:
            self._settle_key(*key, intended)
        self.conn.commit()
        self._write_through()
        return stats

    def _import_row(
        self,
        row: dict[str, Any],
        stats: ImportStats,
        intended: dict[tuple[str, str, str, str], str],
    ) -> None:
        # topic joins rule/why under the scrub (it is persisted and
        # exported); scrubbing is idempotent, so an already-clean corpus
        # keeps its identity and the round-trip stays byte-exact
        key = (row["scope"], row["domain"], self._scrub(row["topic"]))
        generation = (*key, row["created_at"])
        local = self.conn.execute(
            "SELECT * FROM lesson WHERE scope=? AND domain=? AND topic=? "
            "AND created_at=?",
            (*key, row["created_at"]),
        ).fetchone()
        # imported content is scrubbed again on the way in: this machine's
        # deny list may know values the exporting machine's did not
        rule, why = self._scrub(row["rule"]), self._scrub(row["why"])
        if local is None:
            # neutral status; _settle_key applies the intent afterwards
            self.conn.execute(
                "INSERT INTO lesson (scope, domain, topic, rule, why, status, "
                "created_at, updated_at) VALUES (?,?,?,?,?,'superseded',?,?)",
                (*key, rule, why, row["created_at"], row["updated_at"]),
            )
            stats.new += 1
            intended[generation] = row["status"]
        elif row["updated_at"] > local["updated_at"]:
            self.conn.execute(
                "UPDATE lesson SET rule=?, why=?, updated_at=? WHERE id=?",
                (rule, why, row["updated_at"], local["id"]),
            )
            stats.updated += 1
            intended[generation] = row["status"]
        else:
            stats.unchanged += 1
            # the local generation is newer or equal: its own status IS the
            # intent, but the key still needs settling (links, live count)
            intended.setdefault(generation, local["status"])

    def _settle_key(
        self,
        scope: str,
        domain: str,
        topic: str,
        intended: dict[tuple[str, str, str, str], str],
    ) -> None:
        """Post-import invariants for one key: every generation gets its
        intended status, with the live-head conflict (two machines advanced
        the same key) resolved by latest `updated_at` (ties: created_at) —
        exactly one live head, everything else superseded — and
        `superseded_by` links rebuilt from generation (created_at) order.
        Local-only generations not in the import keep their local status as
        their intent."""
        rows = self.conn.execute(
            "SELECT * FROM lesson WHERE scope=? AND domain=? AND topic=? "
            "ORDER BY created_at, updated_at",
            (scope, domain, topic),
        ).fetchall()
        intents = {
            r["id"]: intended.get((scope, domain, topic, r["created_at"]), r["status"])
            for r in rows
        }
        live = [r for r in rows if intents[r["id"]] in LIVE_STATUSES]
        head_id = (
            max(live, key=lambda r: (r["updated_at"], r["created_at"]))["id"]
            if live
            else None
        )
        updates = []
        for i, r in enumerate(rows):
            status = intents[r["id"]]
            if status in LIVE_STATUSES and r["id"] != head_id:
                status = "superseded"
            link = None
            if status == "superseded":
                # what superseded this row: the next generation, or — when
                # conflict resolution crowned an EARLIER generation (later
                # updated_at wins, not later created_at) — the head that
                # beat it; a NULL link would dangle, breaking supersede()'s
                # every-superseded-row-points-at-its-successor meaning. A
                # key whose every generation is retired keeps NULL on the
                # last row: nothing supersedes it.
                successor = rows[i + 1]["id"] if i + 1 < len(rows) else None
                if successor is not None:
                    link = successor
                elif head_id is not None and head_id != r["id"]:
                    link = head_id
            if r["status"] != status or r["superseded_by"] != link:
                updates.append((status, link, r["id"]))
        # demotions before the head's promotion: applying the head first
        # would transiently put two live rows under the unique index
        for status, link, row_id in sorted(
            updates, key=lambda u: u[0] in LIVE_STATUSES
        ):
            self.conn.execute(
                "UPDATE lesson SET status=?, superseded_by=? WHERE id=?",
                (status, link, row_id),
            )


# ---- sync: pull -> import -> export -> commit ------------------------------


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=120
    )


def sync(
    store: LearnStore,
    push: bool = False,
) -> dict[str, Any]:
    """`omater learn sync` (design rev 2): git pull --ff-only → import the
    JSONL → export → commit. The export directory must live inside a git
    repo (Clay's setup: the private dotfiles repo). Commits carry the
    `omater-learn:` prefix so dotfiles history stays legible (Phase 2
    rider); a non-fast-forward pull fails loudly for the operator to
    resolve — sync never merges for you. `push` is opt-in."""
    if store.export_dir is None:
        raise LearnStoreError("no export directory configured")
    export_dir = store.export_dir
    # a fresh clone with no local lessons yet has the REPO but not the
    # export subdirectory; `git -C <missing dir>` would misreport a valid
    # repo as "not inside a git repository"
    export_dir.mkdir(parents=True, exist_ok=True)
    top = _git(export_dir, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        raise LearnStoreError(
            f"export directory {export_dir} is not inside a git repository"
        )
    repo = Path(top.stdout.strip())
    pre = _git(repo, "diff", "--cached", "--quiet")
    if pre.returncode == 1:
        # `git commit` commits the whole index: pre-existing staged edits in
        # the dotfiles repo would ride the omater-learn: commit, misfiling
        # unrelated work under lesson history. Refuse before touching git.
        raise LearnStoreError(
            "the export repo already has staged changes; commit or unstage "
            "them before `omater learn sync` (its commit must carry only "
            "the lessons export)"
        )
    if pre.returncode > 1:
        raise LearnStoreError(f"git diff failed: {pre.stderr.strip()}")
    remotes = _git(repo, "remote")
    if remotes.returncode != 0:
        # an error is not "no remotes": proceeding would silently skip the
        # pull and commit on top of an unknown repo state
        raise LearnStoreError(f"git remote failed: {remotes.stderr.strip()}")
    has_remote = bool(remotes.stdout.strip())
    if has_remote:
        pull = _git(repo, "pull", "--ff-only", "-q")
        if pull.returncode != 0:
            raise LearnStoreError(
                "git pull --ff-only failed - resolve the export repo by hand "
                f"before syncing: {pull.stderr.strip()}"
            )
    stats = store.import_dir()
    # the canonical artifact list, derived from DB scopes without a second
    # serialization pass (import_dir's write-through already produced the
    # files). Staging the whole directory instead would sweep strays
    # (editor backups, OS metadata) into a commit that must carry ONLY the
    # lessons export.
    export_files = store.export_paths()
    if not export_files:
        return {**stats.as_dict(), "committed": False, "pushed": False}
    add = _git(repo, "add", "--", *(str(p) for p in export_files))
    if add.returncode != 0:
        raise LearnStoreError(f"git add failed: {add.stderr.strip()}")
    staged = _git(repo, "diff", "--cached", "--quiet", "--", str(export_dir))
    # --quiet exit codes: 0 = no changes, 1 = changes, >1 = the diff itself
    # failed — conflating an error with "changes present" would commit blind
    if staged.returncode > 1:
        raise LearnStoreError(f"git diff failed: {staged.stderr.strip()}")
    committed = False
    if staged.returncode == 1:
        message = (
            f"omater-learn: sync ({stats.new} new, {stats.updated} updated)"
        )
        commit = _git(repo, "commit", "-q", "-m", message)
        if commit.returncode != 0:
            raise LearnStoreError(f"commit failed: {commit.stderr.strip()}")
        committed = True
    pushed = False
    if push and has_remote:
        proc = _git(repo, "push", "-q")
        if proc.returncode != 0:
            raise LearnStoreError(f"push failed: {proc.stderr.strip()}")
        pushed = True
    return {**stats.as_dict(), "committed": committed, "pushed": pushed}
