"""Learning store (design §6): local index + deterministic JSONL exports.

The acceptance spine for slice A: exports are byte-reproducible (export
twice = byte-equal; import into a FRESH db then export = byte-equal),
volatile counters never travel, lesson content is scrubbed on every write,
and the classified write path (add / refine / supersede) keeps the
supersession audit trail intact across machines.
"""

from __future__ import annotations

import itertools
import json
import sqlite3
import subprocess

import pytest

from claudomater.cli import EXIT_ERROR, EXIT_OK, main
from claudomater.learnstore import (
    EXPORT_FIELDS,
    LearnStore,
    LearnStoreError,
    sync,
)


def ticking_now():
    """Distinct, ordered timestamps per call — determinism without sleeping."""
    counter = itertools.count()

    def now():
        t = next(counter)
        return f"2026-08-30T{t // 3600:02d}:{(t // 60) % 60:02d}:{t % 60:02d}Z"

    return now


@pytest.fixture
def store(tmp_path, monkeypatch):
    # secrets_deny entries are env-var NAMES; the scrub redacts their VALUES
    monkeypatch.setenv("LESSON_TEST_TOKEN", "hunter2-secret")
    s = LearnStore.open(
        tmp_path / "learning.db",
        export_dir=tmp_path / "lessons",
        secrets_deny=("LESSON_TEST_TOKEN",),
        now=ticking_now(),
    )
    yield s
    s.close()


def seed(s, topic="copilot-suppressed-block", scope="global", domain="review"):
    return s.add(scope, domain, topic, "read the suppressed block", "it hid 342 bugs")


class TestOpen:
    def test_open_creates_schema_and_reopen_is_idempotent(self, tmp_path):
        for _ in range(2):
            s = LearnStore.open(tmp_path / "l.db")
            tables = {
                r[0]
                for r in s.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            s.close()
        assert {"lesson", "story", "run_event"} <= tables

    def test_corrupt_db_fails_loudly_and_names_the_recovery(self, tmp_path):
        db = tmp_path / "l.db"
        LearnStore.open(db).close()
        # clobber the header: deterministic corruption every sqlite detects
        data = bytearray(db.read_bytes())
        data[:100] = b"\xff" * 100
        db.write_bytes(bytes(data))
        with pytest.raises(LearnStoreError, match="omater learn import"):
            LearnStore.open(db)

    def test_fts_rot_is_rebuilt_at_open(self, tmp_path):
        """The design declares the sync triggers because the refine path
        rewrites rule text; simulate rot (an update slipping past a dropped
        trigger) and prove open() detects and rebuilds."""
        db = tmp_path / "l.db"
        s = LearnStore.open(db, now=ticking_now())
        seed(s)
        s.conn.execute("DROP TRIGGER lesson_au")
        s.conn.execute("UPDATE lesson SET rule='rebuilt wording'")
        s.conn.commit()
        s.close()
        s2 = LearnStore.open(db)
        hits = s2.search("rebuilt", ["global"])
        assert len(hits) == 1 and hits[0]["rule"] == "rebuilt wording"
        s2.close()


class TestClassifiedWrites:
    def test_add_then_live_retrieval(self, store):
        seed(store)
        (row,) = store.lessons(["global"])
        assert row["status"] == "active" and row["topic"] == "copilot-suppressed-block"

    def test_add_refuses_an_existing_live_key(self, store):
        seed(store)
        with pytest.raises(LearnStoreError, match="classify"):
            seed(store)

    def test_refine_merges_into_the_same_row(self, store):
        lesson_id = seed(store)
        assert store.refine("global", "review", "copilot-suppressed-block",
                            rule="ALWAYS read the suppressed block") == lesson_id
        (row,) = store.lessons(["global"])
        assert row["rule"] == "ALWAYS read the suppressed block"
        assert row["why"] == "it hid 342 bugs"  # untouched half survives
        assert row["updated_at"] > row["created_at"]

    def test_refine_and_supersede_require_an_existing_lesson(self, store):
        with pytest.raises(LearnStoreError, match="add it first"):
            store.refine("global", "review", "ghost", rule="x")
        with pytest.raises(LearnStoreError, match="use add"):
            store.supersede("global", "review", "ghost", "x", "y")

    def test_supersede_keeps_the_audit_trail(self, store):
        old_id = seed(store)
        new_id = store.supersede(
            "global", "review", "copilot-suppressed-block",
            "union the blocks of EVERY review", "zero recurrence measured",
        )
        old = store.conn.execute("SELECT * FROM lesson WHERE id=?", (old_id,)).fetchone()
        assert old["status"] == "superseded" and old["superseded_by"] == new_id
        (live,) = store.lessons(["global"])  # retrieval hides superseded
        assert live["id"] == new_id and live["rule"].startswith("union")

    def test_empty_key_components_are_refused(self, store):
        with pytest.raises(LearnStoreError, match="non-empty"):
            store.add("", "review", "t", "r", "w")
        with pytest.raises(LearnStoreError, match="non-empty"):
            store.add("global", "  ", "t", "r", "w")


class TestScrubDiscipline:
    """Clay's Phase 2 rider: the corpus outlives the run that produced it,
    so deny-listed values must never land in the DB or the export."""

    def test_denied_values_never_reach_db_or_export(self, store, tmp_path):
        store.add("global", "ci", "token-handling",
                  "never echo hunter2-secret in CI logs",
                  "the hunter2-secret token leaked once")
        (row,) = store.lessons(["global"])
        assert "hunter2-secret" not in row["rule"] + row["why"]
        exported = (tmp_path / "lessons" / "global.jsonl").read_text(encoding="utf-8")
        assert "hunter2-secret" not in exported

    def test_import_rescrubs_with_the_local_deny_list(self, store, tmp_path):
        """This machine's deny list may know values the exporting machine's
        did not."""
        foreign = tmp_path / "foreign"
        foreign.mkdir()
        row = {
            "scope": "global", "domain": "ci", "topic": "t",
            "rule": "value hunter2-secret arrived from another machine",
            "why": "w", "status": "active",
            "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
        }
        (foreign / "global.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
        store.import_dir(foreign)
        (imported,) = store.lessons(["global"])
        assert "hunter2-secret" not in imported["rule"]


class TestFtsGhosts:
    def test_refine_leaves_no_ghost_hits(self, store):
        seed(store)
        store.refine("global", "review", "copilot-suppressed-block",
                     rule="entirely rewritten wording")
        assert store.search("suppressed", ["global"]) == []  # old text: gone
        assert len(store.search("rewritten", ["global"])) == 1

    def test_search_scopes_and_hides_superseded(self, store):
        seed(store, scope="ui3-like")
        assert store.search("suppressed", ["global"]) == []  # wrong scope
        store.supersede("ui3-like", "review", "copilot-suppressed-block",
                        "new judgment", "newer evidence")
        hits = store.search("judgment OR suppressed", ["ui3-like"])
        assert [h["rule"] for h in hits] == ["new judgment"]

    def test_invalid_fts_query_fails_loudly(self, store):
        seed(store)
        with pytest.raises(LearnStoreError, match="FTS query"):
            store.search('"unbalanced', ["global"])

    def test_ranked_by_refs(self, store):
        seed(store, topic="a")
        seed(store, topic="b")
        store.conn.execute("UPDATE lesson SET refs=9 WHERE topic='b'")
        store.conn.commit()
        hits = store.search("suppressed", ["global"])
        assert [h["topic"] for h in hits] == ["b", "a"]


class TestDeterministicExport:
    def test_export_twice_is_byte_equal(self, store, tmp_path):
        seed(store, topic="a")
        seed(store, topic="b", domain="ci")
        first = (tmp_path / "lessons" / "global.jsonl").read_bytes()
        store.export()
        assert (tmp_path / "lessons" / "global.jsonl").read_bytes() == first

    def test_volatile_counters_never_travel(self, store, tmp_path):
        seed(store)
        before = (tmp_path / "lessons" / "global.jsonl").read_bytes()
        store.conn.execute("UPDATE lesson SET refs=refs+5, sessions=sessions+2")
        store.conn.commit()
        store.export()
        after = (tmp_path / "lessons" / "global.jsonl").read_bytes()
        assert before == after
        assert b"refs" not in after and b"sessions" not in after

    def test_one_file_per_scope_and_write_through(self, store, tmp_path):
        """Every DB write exports (write-through) — no explicit export call
        in this test."""
        seed(store, scope="global")
        seed(store, scope="stack-ts")
        assert (tmp_path / "lessons" / "global.jsonl").exists()
        assert (tmp_path / "lessons" / "stack-ts.jsonl").exists()

    def test_rows_are_sorted_and_fields_are_exactly_the_contract(self, store, tmp_path):
        seed(store, topic="zzz")
        seed(store, topic="aaa")
        lines = (tmp_path / "lessons" / "global.jsonl").read_text().splitlines()
        rows = [json.loads(l) for l in lines]
        assert [r["topic"] for r in rows] == ["aaa", "zzz"]
        assert all(tuple(sorted(r)) == tuple(sorted(EXPORT_FIELDS)) for r in rows)

    def test_superseded_rows_export_too(self, store, tmp_path):
        """The audit trail is the point: lifecycle rows travel."""
        seed(store)
        store.supersede("global", "review", "copilot-suppressed-block", "new", "why")
        lines = (tmp_path / "lessons" / "global.jsonl").read_text().splitlines()
        assert [json.loads(l)["status"] for l in lines] == ["superseded", "active"]


class TestImportRoundTrip:
    def test_fresh_db_round_trips_byte_exact(self, store, tmp_path):
        """The slice A acceptance: import into a FRESH index, export, and
        the bytes are indistinguishable — the DB is fully reconstructible
        from the JSONL source of truth."""
        seed(store, topic="a")
        store.supersede("global", "review", "a", "new judgment", "newer why")
        seed(store, topic="b", scope="stack-ts", domain="ci")
        originals = {
            p.name: p.read_bytes() for p in (tmp_path / "lessons").glob("*.jsonl")
        }
        fresh_dir = tmp_path / "fresh-lessons"
        fresh = LearnStore.open(tmp_path / "fresh.db", export_dir=fresh_dir)
        stats = fresh.import_dir(tmp_path / "lessons")
        assert stats.new == 3 and stats.updated == 0
        fresh.export()
        assert {
            p.name: p.read_bytes() for p in fresh_dir.glob("*.jsonl")
        } == originals
        # the chain is rebuilt with LOCAL ids: superseded row points at head
        old = fresh.conn.execute(
            "SELECT * FROM lesson WHERE topic='a' AND status='superseded'"
        ).fetchone()
        head = fresh.conn.execute(
            "SELECT * FROM lesson WHERE topic='a' AND status='active'"
        ).fetchone()
        assert old["superseded_by"] == head["id"]
        fresh.close()

    def test_latest_updated_at_wins(self, store, tmp_path):
        seed(store)
        foreign = tmp_path / "foreign"
        foreign.mkdir()
        (row,) = store.lessons(["global"])
        newer = {f: row[f] for f in EXPORT_FIELDS}
        newer["rule"], newer["updated_at"] = "the other machine refined this", "2027-01-01T00:00:00Z"
        (foreign / "global.jsonl").write_text(
            json.dumps(newer, sort_keys=True) + "\n", encoding="utf-8"
        )
        stats = store.import_dir(foreign)
        assert stats.updated == 1
        (live,) = store.lessons(["global"])
        assert live["rule"] == "the other machine refined this"
        # importing the now-older original back is a no-op
        stats2 = store.import_dir(foreign)
        assert stats2.updated == 0 and stats2.unchanged == 1

    def test_two_machine_head_conflict_resolves_to_one_live_row(self, store, tmp_path):
        """Both machines superseded the same key with different judgments:
        after import there is exactly ONE live head (latest updated_at) and
        the other generation is superseded into the chain."""
        seed(store)
        store.supersede("global", "review", "copilot-suppressed-block",
                        "local judgment", "local why")
        foreign = tmp_path / "foreign"
        foreign.mkdir()
        remote_head = {
            "scope": "global", "domain": "review", "topic": "copilot-suppressed-block",
            "rule": "remote judgment", "why": "remote why", "status": "active",
            "created_at": "2027-06-01T00:00:00Z", "updated_at": "2027-06-01T00:00:00Z",
        }
        (foreign / "global.jsonl").write_text(
            json.dumps(remote_head, sort_keys=True) + "\n", encoding="utf-8"
        )
        store.import_dir(foreign)
        (live,) = store.lessons(["global"])
        assert live["rule"] == "remote judgment"  # newer updated_at won
        chain = store.conn.execute(
            "SELECT status FROM lesson WHERE topic='copilot-suppressed-block'"
        ).fetchall()
        assert sorted(r["status"] for r in chain) == ["active", "superseded", "superseded"]

    def test_malformed_lines_fail_loudly_with_location(self, store, tmp_path):
        bad = tmp_path / "bad"
        bad.mkdir()
        (bad / "global.jsonl").write_text('{"scope": "global"}\n', encoding="utf-8")
        with pytest.raises(LearnStoreError, match="global.jsonl:1"):
            store.import_dir(bad)
        (bad / "global.jsonl").write_text("not json\n", encoding="utf-8")
        with pytest.raises(LearnStoreError, match="not valid JSON"):
            store.import_dir(bad)


def git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    )


class TestSync:
    @pytest.fixture
    def synced_store(self, tmp_path):
        """An export dir inside a git repo with a bare 'origin'."""
        origin = tmp_path / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
        repo = tmp_path / "dotfiles"
        subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True)
        git(repo, "config", "user.email", "t@t")
        git(repo, "config", "user.name", "t")
        (repo / "seed.txt").write_text("x", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "seed")
        git(repo, "push", "-q")
        s = LearnStore.open(
            tmp_path / "l.db",
            export_dir=repo / "omater" / "lessons",
            now=ticking_now(),
        )
        yield s, repo
        s.close()

    def test_sync_commits_with_the_omater_learn_prefix(self, synced_store):
        s, repo = synced_store
        seed(s)
        result = sync(s)
        assert result["committed"] is True and result["pushed"] is False
        subject = git(repo, "log", "-1", "--format=%s").stdout.strip()
        assert subject.startswith("omater-learn: sync (")
        body = git(repo, "log", "-1", "--format=%b").stdout
        assert "Co-Authored" not in body and "Generated with" not in body

    def test_sync_without_changes_makes_no_commit(self, synced_store):
        s, repo = synced_store
        seed(s)
        sync(s)
        before = git(repo, "rev-parse", "HEAD").stdout
        result = sync(s)
        assert result["committed"] is False
        assert git(repo, "rev-parse", "HEAD").stdout == before

    def test_push_is_opt_in_and_works(self, synced_store):
        s, repo = synced_store
        seed(s)
        result = sync(s, push=True)
        assert result["pushed"] is True
        remote = git(repo, "ls-remote", "origin", "HEAD").stdout.split()[0]
        assert remote == git(repo, "rev-parse", "HEAD").stdout.strip()

    def test_sync_pulls_the_other_machines_lessons_first(self, synced_store, tmp_path):
        s, repo = synced_store
        seed(s)
        sync(s, push=True)
        # "machine B" adds a lesson and pushes
        other = tmp_path / "machine-b"
        subprocess.run(
            ["git", "clone", "-q", git(repo, "remote", "get-url", "origin").stdout.strip(), str(other)],
            check=True,
        )
        git(other, "config", "user.email", "b@b")
        git(other, "config", "user.name", "b")
        sb = LearnStore.open(
            tmp_path / "b.db", export_dir=other / "omater" / "lessons",
        )
        sb.add("global", "ci", "from-machine-b", "b's rule", "b's why")
        sb.close()
        git(other, "add", "-A")
        git(other, "commit", "-q", "-m", "omater-learn: sync (1 new, 0 updated)")
        git(other, "push", "-q")
        # machine A syncs: pull brings b's lesson into A's local index
        sync(s)
        topics = {r["topic"] for r in s.lessons(["global"])}
        assert "from-machine-b" in topics

    def test_non_git_export_dir_fails_loudly(self, tmp_path):
        s = LearnStore.open(tmp_path / "l.db", export_dir=tmp_path / "plain")
        with pytest.raises(LearnStoreError, match="not inside a git repository"):
            sync(s)
        s.close()


class TestLearnCli:
    def _args(self, tmp_path, *rest):
        return [
            "learn", *rest,
            "--user-config", str(tmp_path / "missing.yaml"),
            "--db", str(tmp_path / "cli.db"),
            "--export-dir", str(tmp_path / "cli-lessons"),
        ]

    def test_add_list_export_wiring(self, tmp_path, capsys):
        rc = main(self._args(
            tmp_path, "add", "--scope", "global", "--domain", "ci",
            "--topic", "t1", "--rule", "r", "--why", "w",
        ))
        assert rc == EXIT_OK
        assert main(self._args(tmp_path, "list", "--scope", "global")) == EXIT_OK
        out = capsys.readouterr().out
        assert "global/ci/t1" in out and "1 live lesson" in out
        assert (tmp_path / "cli-lessons" / "global.jsonl").exists()

    def test_add_conflict_is_a_cli_error_not_a_traceback(self, tmp_path, capsys):
        args = self._args(
            tmp_path, "add", "--scope", "global", "--domain", "ci",
            "--topic", "t1", "--rule", "r", "--why", "w",
        )
        assert main(args) == EXIT_OK
        assert main(args) == EXIT_ERROR
        assert "classify" in capsys.readouterr().err

    def test_project_secrets_deny_scrubs_cli_writes(self, tmp_path, capsys):
        project = tmp_path / "proj"
        project.mkdir()
        (project / ".omater.yaml").write_text(
            "project: demo\nsecrets_deny:\n  - MY_TOKEN\n", encoding="utf-8"
        )
        import os
        os.environ["MY_TOKEN"] = "tok-123-secret"
        try:
            rc = main(self._args(
                tmp_path, "add", "--scope", "global", "--domain", "ci",
                "--topic", "tok", "--rule", "value tok-123-secret leaked",
                "--why", "w", "--project", str(project),
            ))
        finally:
            del os.environ["MY_TOKEN"]
        assert rc == EXIT_OK
        exported = (tmp_path / "cli-lessons" / "global.jsonl").read_text()
        assert "tok-123-secret" not in exported


class TestImportBoundaryHardening:
    """PR #11 round 1: the import path is the untrusted boundary."""

    def test_unconfigured_import_never_falls_back_to_cwd(self, tmp_path, monkeypatch):
        """With no directory configured anywhere, import must fail loudly -
        the cwd may well CONTAIN importable jsonl (a silent wrong-corpus
        load), which is exactly why it must not be used."""
        trap = tmp_path / "cwd-trap"
        trap.mkdir()
        (trap / "global.jsonl").write_text(
            json.dumps({
                "scope": "global", "domain": "ci", "topic": "trap",
                "rule": "r", "why": "w", "status": "active",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
            }) + "\n", encoding="utf-8",
        )
        monkeypatch.chdir(trap)
        s = LearnStore.open(tmp_path / "bare.db")
        with pytest.raises(LearnStoreError, match="no import directory configured"):
            s.import_dir()
        assert s.lessons(["global"]) == []
        s.close()

    def test_import_is_write_through_like_every_other_db_write(self, store, tmp_path):
        foreign = tmp_path / "foreign"
        foreign.mkdir()
        (foreign / "global.jsonl").write_text(
            json.dumps({
                "scope": "global", "domain": "ci", "topic": "from-foreign",
                "rule": "r", "why": "w", "status": "active",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
            }, sort_keys=True) + "\n", encoding="utf-8",
        )
        store.import_dir(foreign)  # no explicit export() call
        exported = (tmp_path / "lessons" / "global.jsonl").read_text(encoding="utf-8")
        assert "from-foreign" in exported

    def test_malformed_timestamps_fail_closed_with_location(self, store, tmp_path):
        """Winner selection and chain order are lexicographic - only sound
        for the exact ISO-Z format, so anything else is refused by name."""
        bad = tmp_path / "bad-ts"
        bad.mkdir()
        row = {
            "scope": "global", "domain": "ci", "topic": "t",
            "rule": "r", "why": "w", "status": "active",
            "created_at": "yesterday", "updated_at": "2026-01-01T00:00:00Z",
        }
        (bad / "global.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
        with pytest.raises(LearnStoreError, match=r"global\.jsonl:1: created_at"):
            store.import_dir(bad)


class TestSyncGitErrorHandling:
    """PR #11 round 1: exit codes >1 from `git diff --quiet` are errors, not
    'changes present', and a failed `git add` must never be shrugged off."""

    def _sync_with_patched_git(self, synced_store, monkeypatch, fail_on):
        import subprocess as sp

        from claudomater import learnstore

        s, repo = synced_store
        seed(s)
        real_git = learnstore._git

        def flaky_git(cwd, *args):
            if args[0] == fail_on:
                return sp.CompletedProcess(args, 2, stdout="", stderr="simulated failure")
            return real_git(cwd, *args)

        monkeypatch.setattr(learnstore, "_git", flaky_git)
        return s

    @pytest.fixture
    def synced_store(self, tmp_path):
        origin = tmp_path / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
        repo = tmp_path / "dotfiles"
        subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True)
        git(repo, "config", "user.email", "t@t")
        git(repo, "config", "user.name", "t")
        (repo / "seed.txt").write_text("x", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "seed")
        git(repo, "push", "-q")
        s = LearnStore.open(
            tmp_path / "l.db", export_dir=repo / "omater" / "lessons",
            now=ticking_now(),
        )
        yield s, repo
        s.close()

    def test_diff_error_is_not_treated_as_changes(self, synced_store, monkeypatch):
        s = self._sync_with_patched_git(synced_store, monkeypatch, fail_on="diff")
        with pytest.raises(LearnStoreError, match="git diff failed"):
            sync(s)

    def test_add_failure_fails_loudly(self, synced_store, monkeypatch):
        s = self._sync_with_patched_git(synced_store, monkeypatch, fail_on="add")
        with pytest.raises(LearnStoreError, match="git add failed"):
            sync(s)


class TestTopicScrubIdentity:
    """PR #11 round 2: the topic is part of the KEY, so its scrubbed form
    must be the one identity everywhere - lookup, storage, import, and
    error text. Scrubbing only at insert split the identity: refine missed
    the row, a second add hit the unique index as an uncaught
    IntegrityError, and the error string echoed the secret."""

    SECRET_TOPIC = "leak-hunter2-secret-endpoint"

    def test_scrub_altered_topic_is_one_lesson_across_verbs(self, store):
        store.add("global", "ci", self.SECRET_TOPIC, "r", "w")
        # the same raw topic addresses the same lesson for every verb
        store.refine("global", "ci", self.SECRET_TOPIC, rule="refined")
        new_id = store.supersede("global", "ci", self.SECRET_TOPIC, "superseded judgment", "why")
        (live,) = store.lessons(["global"])
        assert live["id"] == new_id
        assert "hunter2-secret" not in live["topic"]

    def test_second_add_is_a_classify_error_that_never_echoes_the_secret(self, store):
        store.add("global", "ci", self.SECRET_TOPIC, "r", "w")
        with pytest.raises(LearnStoreError, match="classify") as exc_info:
            store.add("global", "ci", self.SECRET_TOPIC, "r2", "w2")
        assert "hunter2-secret" not in str(exc_info.value)

    def test_import_scrubs_the_topic_too(self, store, tmp_path):
        foreign = tmp_path / "foreign"
        foreign.mkdir()
        row = {
            "scope": "global", "domain": "ci", "topic": self.SECRET_TOPIC,
            "rule": "r", "why": "w", "status": "active",
            "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
        }
        (foreign / "global.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
        store.import_dir(foreign)
        (imported,) = store.lessons(["global"])
        assert "hunter2-secret" not in imported["topic"]
        exported = (tmp_path / "lessons" / "global.jsonl").read_text(encoding="utf-8")
        assert "hunter2-secret" not in exported


class TestRound3Hardening:
    """PR #11 round 3: the terminal is a retention surface too; empty
    filters select nothing; sync's commit carries ONLY the lessons export."""

    def test_cli_success_message_echoes_the_scrubbed_topic(
        self, tmp_path, capsys, monkeypatch
    ):
        project = tmp_path / "proj"
        project.mkdir()
        (project / ".omater.yaml").write_text(
            "project: demo\nsecrets_deny:\n  - MY_TOKEN\n", encoding="utf-8"
        )
        monkeypatch.setenv("MY_TOKEN", "tok-123-secret")
        rc = main([
            "learn", "add", "--scope", "global", "--domain", "ci",
            "--topic", "rotate-tok-123-secret-monthly", "--rule", "r", "--why", "w",
            "--user-config", str(tmp_path / "missing.yaml"),
            "--db", str(tmp_path / "cli.db"),
            "--export-dir", str(tmp_path / "cli-lessons"),
            "--project", str(project),
        ])
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        assert "tok-123-secret" not in out
        assert "[REDACTED:MY_TOKEN]" in out

    def test_empty_domain_filter_selects_nothing(self, store):
        seed(store)
        assert store.lessons(["global"], domains=[]) == []
        assert len(store.lessons(["global"], domains=None)) == 1
        assert len(store.lessons(["global"], domains=["review"])) == 1

    def test_sync_refuses_a_dirty_index(self, tmp_path):
        """Pre-existing staged edits in the dotfiles repo must never ride
        the omater-learn: commit - and the refusal leaves them untouched."""
        origin = tmp_path / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
        repo = tmp_path / "dotfiles"
        subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True)
        git(repo, "config", "user.email", "t@t")
        git(repo, "config", "user.name", "t")
        (repo / "seed.txt").write_text("x", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "seed")
        (repo / "unrelated-wip.txt").write_text("half-done dotfile edit", encoding="utf-8")
        git(repo, "add", "unrelated-wip.txt")
        s = LearnStore.open(
            tmp_path / "l.db", export_dir=repo / "omater" / "lessons",
            now=ticking_now(),
        )
        seed(s)
        head_before = git(repo, "rev-parse", "HEAD").stdout
        with pytest.raises(LearnStoreError, match="already has staged changes"):
            sync(s)
        assert git(repo, "rev-parse", "HEAD").stdout == head_before  # no commit
        staged = git(repo, "diff", "--cached", "--name-only").stdout.split()
        assert staged == ["unrelated-wip.txt"]  # untouched, and nothing else
        s.close()


class TestScopeFilenameContract:
    """PR #11 round 4: scope names map 1:1 to export files, so unsafe names
    fail CLOSED - sanitizing let distinct scopes ('a/b', 'a?b') collide onto
    one filename and silently mix corpora."""

    def test_unsafe_scopes_are_refused_at_the_write(self, store):
        for bad in ("a/b", "a?b", ".hidden", "sp ace"):
            with pytest.raises(LearnStoreError, match="filename-safe"):
                store.add(bad, "ci", "t", "r", "w")

    def test_import_refuses_empty_or_unsafe_keys_with_location(self, store, tmp_path):
        bad = tmp_path / "bad-keys"
        bad.mkdir()
        base = {
            "rule": "r", "why": "w", "status": "active",
            "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
        }
        (bad / "x.jsonl").write_text(
            json.dumps({**base, "scope": "global", "domain": "ci", "topic": "  "}) + "\n",
            encoding="utf-8",
        )
        with pytest.raises(LearnStoreError, match=r"x\.jsonl:1: topic"):
            store.import_dir(bad)
        (bad / "x.jsonl").write_text(
            json.dumps({**base, "scope": "a/b", "domain": "ci", "topic": "t"}) + "\n",
            encoding="utf-8",
        )
        with pytest.raises(LearnStoreError, match=r"x\.jsonl:1: scope"):
            store.import_dir(bad)
