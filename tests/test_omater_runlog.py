"""Run log: write-ahead events, one live run per project, adoption, control."""

from __future__ import annotations

import json

import pytest

from claudomater.runlog import RunError, RunLog, runs_root


class TestRunLifecycle:
    def test_create_writes_both_files_and_current_symlink(self, tmp_path):
        log = RunLog.create(tmp_path)
        assert (log.run_dir / "events.jsonl").exists()
        assert (log.run_dir / "progress.log").exists()
        current = runs_root(tmp_path) / "current"
        assert current.resolve() == log.run_dir.resolve()

    def test_one_live_run_per_project(self, tmp_path):
        RunLog.create(tmp_path, run_id="run-a")
        with pytest.raises(RunError, match="live run already exists"):
            RunLog.create(tmp_path, run_id="run-b")

    def test_finished_run_allows_a_new_one(self, tmp_path):
        log = RunLog.create(tmp_path, run_id="run-a")
        log.finish("run-complete")
        log2 = RunLog.create(tmp_path, run_id="run-b")
        assert log2.run_id == "run-b"
        current = runs_root(tmp_path) / "current"
        assert current.resolve().name == "run-b"

    def test_concurrent_create_is_locked(self, tmp_path):
        """A held (fresh) create lock blocks a second create instead of
        letting last-repoint-wins orphan the first run."""
        import os
        import time as _time

        from claudomater.runlog import CREATE_LOCK, runs_root as rr

        rr(tmp_path).mkdir(parents=True)
        lock = rr(tmp_path) / CREATE_LOCK
        lock.mkdir()  # another process is mid-create
        with pytest.raises(RunError, match="in progress"):
            RunLog.create(tmp_path)
        # a stale lock (crashed holder) is broken and create proceeds
        old = _time.time() - 300
        os.utime(lock, (old, old))
        log = RunLog.create(tmp_path)
        assert log.is_live()
        assert not lock.exists()  # released after create

    def test_file_shaped_lock_cannot_wedge_create_forever(self, tmp_path):
        """rmtree silently no-ops on a file lock — a stale FILE at the lock
        path must still be broken, not wedge creation permanently."""
        import os
        import time as _time

        from claudomater.runlog import CREATE_LOCK, runs_root as rr

        rr(tmp_path).mkdir(parents=True)
        lock = rr(tmp_path) / CREATE_LOCK
        lock.write_text("tampered", encoding="utf-8")  # file, not dir
        old = _time.time() - 300
        os.utime(lock, (old, old))
        log = RunLog.create(tmp_path)
        assert log.is_live()
        assert not lock.exists()

    def test_symlink_lock_is_aged_by_the_link_not_the_target(self, tmp_path):
        """A symlink-shaped lock pointing at a FRESH file elsewhere must
        still break as stale when the link itself is old (lstat, not stat)."""
        import os
        import time as _time

        from claudomater.runlog import CREATE_LOCK, runs_root as rr

        rr(tmp_path).mkdir(parents=True)
        fresh_target = tmp_path / "fresh-file"
        fresh_target.write_text("x", encoding="utf-8")
        lock = rr(tmp_path) / CREATE_LOCK
        lock.symlink_to(fresh_target)
        old = _time.time() - 300
        os.utime(lock, (old, old), follow_symlinks=False)
        log = RunLog.create(tmp_path)  # stale link broken, create proceeds
        assert log.is_live()
        assert not lock.is_symlink()

    def test_failed_create_leaves_no_orphans(self, tmp_path, monkeypatch):
        """An IO failure after mkdir must not strand an orphan run dir or a
        dangling current link — repeated attempts would accumulate them."""
        from claudomater.runlog import runs_root as rr

        monkeypatch.setattr(
            RunLog,
            "_point_current",
            lambda self: (_ for _ in ()).throw(OSError("disk full")),
        )
        with pytest.raises(OSError, match="disk full"):
            RunLog.create(tmp_path, run_id="doomed")
        assert not (rr(tmp_path) / "doomed").exists()
        assert not (rr(tmp_path) / "current").is_symlink()

    def test_finish_rejects_non_terminal_event(self, tmp_path):
        log = RunLog.create(tmp_path)
        with pytest.raises(RunError, match="not a terminal"):
            log.finish("phase-verified")


class TestEvents:
    def test_events_are_structured_and_ordered(self, tmp_path):
        log = RunLog.create(tmp_path, run_id="r1")
        log.event("create", "phase-spawn", {"model": "m", "attempt": 1}, story_key="s-1")
        log.event("create", "phase-verified", {"attempt": 1})
        events = log.events()
        assert [e["event"] for e in events] == [
            "run-created",
            "phase-spawn",
            "phase-verified",
        ]
        spawn = events[1]
        assert spawn["run_id"] == "r1"
        assert spawn["phase"] == "create"
        assert spawn["story_key"] == "s-1"
        assert spawn["detail"]["model"] == "m"
        assert spawn["ts"].endswith("Z")

    def test_progress_log_is_human_readable(self, tmp_path):
        log = RunLog.create(tmp_path)
        log.event("dev", "phase-spawn", {"model": "claude-opus-5"})
        lines = (log.run_dir / "progress.log").read_text().splitlines()
        assert any("[dev] phase-spawn" in line and "claude-opus-5" in line for line in lines)

    def test_events_jsonl_is_one_json_object_per_line(self, tmp_path):
        log = RunLog.create(tmp_path)
        log.event("dev", "phase-spawn")
        for line in (log.run_dir / "events.jsonl").read_text().splitlines():
            json.loads(line)

    def test_torn_final_line_is_dropped_not_fatal(self, tmp_path):
        """A crash mid-append leaves a torn tail — exactly when adoption
        runs. Write-ahead means the torn event's action never committed, so
        the tail is dropped and the run stays adoptable."""
        log = RunLog.create(tmp_path)
        log.event("dev", "phase-spawn", {"model": "m"})
        with open(log.run_dir / "events.jsonl", "a", encoding="utf-8") as fh:
            fh.write('{"ts": "2026-08-28T21:00:00Z", "event": "phase-ver')
        events = log.events()
        assert [e["event"] for e in events] == ["run-created", "phase-spawn"]
        assert log.is_live()
        adopted = RunLog.adopt(tmp_path)
        assert adopted.run_id == log.run_id

    def test_torn_multibyte_tail_is_recoverable(self, tmp_path):
        """A torn append can cut a multi-byte UTF-8 char (events are written
        ensure_ascii=False); the decode must not defeat torn-tail tolerance."""
        log = RunLog.create(tmp_path)
        log.event("dev", "phase-spawn")
        with open(log.run_dir / "events.jsonl", "ab") as fh:
            fh.write('{"note": "caf'.encode() + b"\xc3")  # é cut in half
        assert [e["event"] for e in log.events()] == ["run-created", "phase-spawn"]
        assert log.is_live()

    def test_object_missing_event_key_counts_as_corrupt(self, tmp_path):
        """A valid JSON object without 'event' must not reach is_live() as a
        KeyError — torn tail drops, middle line is a clear RunError. (Both
        lines written directly: an API append would now REPAIR a damaged
        tail instead of burying it, so genuine middle damage is the only way
        this state exists.)"""
        log = RunLog.create(tmp_path)
        with open(log.run_dir / "events.jsonl", "a", encoding="utf-8") as fh:
            fh.write('{"ts": "2026-08-28T23:00:00Z"}\n')
        assert log.is_live()  # tail dropped, no KeyError
        with open(log.run_dir / "events.jsonl", "a", encoding="utf-8") as fh:
            fh.write(
                '{"ts": "2026-08-28T23:00:01Z", "run_id": "x", '
                '"phase": "dev", "event": "phase-spawn"}\n'
            )
        with pytest.raises(RunError, match="corrupt"):
            log.events()

    def test_non_object_json_lines_get_the_same_treatment(self, tmp_path):
        log = RunLog.create(tmp_path)
        log.event("dev", "phase-spawn")
        with open(log.run_dir / "events.jsonl", "a", encoding="utf-8") as fh:
            fh.write("42\n")  # valid JSON, not an object — torn tail case
        assert [e["event"] for e in log.events()] == ["run-created", "phase-spawn"]
        log2 = RunLog(log.run_dir, log.run_id)
        with open(log.run_dir / "events.jsonl", "a", encoding="utf-8") as fh:
            fh.write('{"event": "phase-failed", "phase": "dev", "ts": "t", "run_id": "r"}\n')
        with pytest.raises(RunError, match="not a JSON object"):
            log2.events()  # the bare 42 is now a MIDDLE line -> damage

    def test_tampered_current_symlink_fails_loudly(self, tmp_path):
        """A hand-edited runs/current pointing outside the runs dir must not
        let adopt/control write logs elsewhere."""
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        from claudomater.runlog import runs_root as rr

        rr(tmp_path).mkdir(parents=True)
        (rr(tmp_path) / "current").symlink_to(outside)
        with pytest.raises(RunError, match="points outside"):
            RunLog.adopt(tmp_path)
        with pytest.raises(RunError, match="points outside"):
            RunLog.create(tmp_path)

    def test_corrupt_middle_line_is_a_run_error(self, tmp_path):
        """Damage in the MIDDLE of history stays a loud error (only a torn
        FINAL line is recoverable). Written directly — an API append now
        repairs a damaged tail rather than burying it under new events."""
        log = RunLog.create(tmp_path)
        with open(log.run_dir / "events.jsonl", "a", encoding="utf-8") as fh:
            fh.write("garbage not json\n")
            fh.write(
                '{"ts": "2026-08-28T23:00:01Z", "run_id": "x", '
                '"phase": "dev", "event": "phase-spawn"}\n'
            )
        with pytest.raises(RunError, match="corrupt"):
            log.events()


class TestAdoption:
    def test_adopt_attaches_to_current_and_replays_events(self, tmp_path):
        log = RunLog.create(tmp_path, run_id="orphan")
        log.event("dev", "phase-spawn", {"model": "m", "attempt": 1})
        # Orchestrator dies here. A fresh session adopts:
        adopted = RunLog.adopt(tmp_path)
        assert adopted.run_id == "orphan"
        events = [e["event"] for e in adopted.events()]
        # write-ahead means the spawn intent is present even though no
        # phase-verified/failed followed — the adopter replays against reality
        assert "phase-spawn" in events
        assert events[-1] == "run-adopted"

    def test_adopt_without_current_raises(self, tmp_path):
        with pytest.raises(RunError, match="no current run"):
            RunLog.adopt(tmp_path)

    def test_adopt_refuses_a_finished_run(self, tmp_path):
        """Adopting a completed run would flip it live again and wedge
        one-live-run enforcement forever."""
        log = RunLog.create(tmp_path)
        log.finish("run-complete")
        with pytest.raises(RunError, match="already ended"):
            RunLog.adopt(tmp_path)
        # and a new run can still start afterwards
        RunLog.create(tmp_path, run_id="fresh")

    def test_stray_non_symlink_current_is_a_run_error(self, tmp_path):
        from claudomater.runlog import runs_root as rr

        stray = rr(tmp_path) / "current"
        stray.mkdir(parents=True)
        with pytest.raises(RunError, match="not a symlink"):
            RunLog.create(tmp_path)
        # fail-fast: no orphan run dir may be left behind by the attempt
        assert sorted(p.name for p in rr(tmp_path).iterdir()) == ["current"]

    def test_run_id_path_traversal_is_rejected(self, tmp_path):
        # ("" is absent: falsy run_id means auto-generate, which is valid)
        for bad in ("../evil", "a/b", "..", ".", "current", "back\\slash"):
            with pytest.raises(RunError, match="simple name|invalid run id"):
                RunLog.create(tmp_path, run_id=bad)
        # nothing escaped .omater/runs or got created by the attempts
        from claudomater.runlog import runs_root as rr

        assert not (tmp_path / "evil").exists()
        assert not rr(tmp_path).exists() or list(rr(tmp_path).iterdir()) == []

    def test_duplicate_run_id_is_a_run_error(self, tmp_path):
        log = RunLog.create(tmp_path, run_id="dup")
        log.finish("run-complete")
        with pytest.raises(RunError, match="already exists"):
            RunLog.create(tmp_path, run_id="dup")


class TestControl:
    def test_control_events_round_trip(self, tmp_path):
        log = RunLog.create(tmp_path)
        log.write_control("resume")
        log.write_control("approve", {"gate": "promotion"})
        controls = log.read_controls()
        assert [c["action"] for c in controls] == ["resume", "approve"]
        # control writes also land in the run's event stream
        assert "control-resume" in [e["event"] for e in log.events()]

    def test_unknown_control_action_raises(self, tmp_path):
        log = RunLog.create(tmp_path)
        with pytest.raises(RunError, match="unknown control action"):
            log.write_control("skip")

    def test_torn_final_control_line_is_dropped(self, tmp_path):
        log = RunLog.create(tmp_path)
        log.write_control("resume")
        with open(log.run_dir / "control.jsonl", "a", encoding="utf-8") as fh:
            fh.write('{"ts": "2026-08-28T22:00:00Z", "action": "abo')
        assert [c["action"] for c in log.read_controls()] == ["resume"]

    def test_corrupt_middle_control_line_is_a_run_error(self, tmp_path):
        log = RunLog.create(tmp_path)
        with open(log.run_dir / "control.jsonl", "a", encoding="utf-8") as fh:
            fh.write("garbage\n")
        log.write_control("resume")
        with pytest.raises(RunError, match="corrupt"):
            log.read_controls()

    def test_control_on_an_ended_run_is_refused(self, tmp_path):
        """Round-3 finding (suppressed): `omater resume|abort|approve` could
        append control-* after a terminal event and flip is_live() back on.
        An ended run accepts no control — and the command channel stays
        clean of dead commands too."""
        log = RunLog.create(tmp_path)
        log.finish("run-complete")
        with pytest.raises(RunError, match="nothing to act on"):
            log.write_control("resume")
        assert not log.is_live()
        assert log.read_controls() == []

    def test_refused_control_leaves_the_command_channel_clean(self, tmp_path):
        """Round-4 finding: the terminal check, the control.jsonl write, and
        the event append share ONE _append_lock critical section (which
        finish() also takes), so 'check passed, owner finished, dead command
        persisted' can no longer interleave. Contract pin: a refused control
        leaves control.jsonl untouched."""
        log = RunLog.create(tmp_path)
        log.event("dev", "phase-spawn", {"model": "m", "attempt": 1})
        sibling = RunLog.attach(tmp_path)
        log.finish("run-complete")
        with pytest.raises(RunError, match="nothing to act on"):
            sibling.write_control("resume")
        assert sibling.read_controls() == []


class TestTranscriptPaths:
    """Report rough edge #1: transcript_path(phase, attempt) had no story key,
    so every story's dev-attempt-1.md overwrote the previous story's — a
    5-story sandbox run kept exactly ONE dev transcript."""

    def test_distinct_stories_get_distinct_paths(self, tmp_path):
        log = RunLog.create(tmp_path)
        a = log.transcript_path("dev", 1, story_key="OM-1")
        b = log.transcript_path("dev", 1, story_key="OM-2")
        assert a != b
        assert "OM-1" in a.name and "OM-2" in b.name

    def test_redrives_of_the_same_attempt_get_distinct_paths(self, tmp_path):
        """A crash-recovery (or escalation) re-drive re-runs the same
        story/phase/attempt — the spawn timestamp keeps both transcripts."""
        log = RunLog.create(tmp_path)
        a = log.transcript_path("dev", 1, story_key="OM-3", ts="2026-08-28T22:21:30Z")
        b = log.transcript_path("dev", 1, story_key="OM-3", ts="2026-08-28T22:24:05Z")
        assert a != b

    def test_story_key_cannot_escape_the_transcripts_dir(self, tmp_path):
        log = RunLog.create(tmp_path)
        p = log.transcript_path("dev", 1, story_key="../../evil")
        assert p.parent == log.run_dir / "transcripts"
        assert not p.name.startswith(".")

    def test_suffix_is_a_knob(self, tmp_path):
        """Full-session stream captures are jsonl, final-message ones are md."""
        log = RunLog.create(tmp_path)
        assert log.transcript_path("dev", 1, suffix=".jsonl").suffix == ".jsonl"
        assert log.transcript_path("dev", 1).suffix == ".md"

    def test_ts_and_suffix_cannot_escape_either(self, tmp_path):
        """Public API: EVERY caller-supplied filename component is contained,
        not just story_key."""
        log = RunLog.create(tmp_path)
        p = log.transcript_path("dev", 1, ts="../../etc/evil")
        assert p.parent == log.run_dir / "transcripts"
        for bad_suffix in ("/../../evil", ".md/x", "md", ""):
            with pytest.raises(RunError, match="invalid transcript suffix"):
                log.transcript_path("dev", 1, suffix=bad_suffix)


class TestAttachVsAdopt:
    """Report rough edge #9: `omater start` + a separate orchestrator process
    made the very FIRST attach log `run-adopted` — attach and crash-recovery
    shared one verb, so every healthy run started with a recovery event."""

    def test_first_attach_of_a_fresh_run_logs_run_attached(self, tmp_path):
        log = RunLog.create(tmp_path)
        log.event("run", "policy", {"deployment_type": "sandbox"})
        attached = RunLog.adopt(tmp_path)
        assert [e["event"] for e in attached.events()][-1] == "run-attached"

    def test_adoption_with_phase_activity_logs_run_adopted(self, tmp_path):
        log = RunLog.create(tmp_path)
        log.event("dev", "phase-spawn", {"model": "m", "attempt": 1})
        # orchestrator dies here; the unanswered spawn is the orphan shape
        adopted = RunLog.adopt(tmp_path)
        assert [e["event"] for e in adopted.events()][-1] == "run-adopted"


class TestAttachSeam:
    """Epic 9 rough edge #3: the merge-phase driver appended events via
    `adopt()` and stamped a `run-adopted` (crash-recovery) bookkeeping event
    per invocation. `attach()` is the first-class append seam: same run,
    zero bookkeeping noise."""

    def test_attach_writes_no_bookkeeping_event(self, tmp_path):
        log = RunLog.create(tmp_path)
        log.event("dev", "phase-spawn", {"model": "m", "attempt": 1})
        before = [e["event"] for e in log.events()]
        sibling = RunLog.attach(tmp_path)
        assert [e["event"] for e in sibling.events()] == before

    def test_attached_sibling_appends_into_the_same_run(self, tmp_path):
        log = RunLog.create(tmp_path)
        log.event("dev", "phase-spawn", {"model": "m", "attempt": 1})
        sibling = RunLog.attach(tmp_path)
        sibling.event("merge", "copilot-round", {"round": 1})
        assert log.events()[-1]["event"] == "copilot-round"
        assert sibling.run_id == log.run_id

    def test_attach_refuses_an_ended_run(self, tmp_path):
        """Appending to a closed run would forge post-mortem history."""
        log = RunLog.create(tmp_path)
        log.finish("run-complete")
        with pytest.raises(RunError, match="already ended"):
            RunLog.attach(tmp_path)

    def test_attached_sibling_cannot_append_after_the_owner_finishes(self, tmp_path):
        """Round-1 finding: check-at-attach alone is a race — the owner can
        finish AFTER the sibling attached, and a late append would land past
        the terminal record and flip is_live() back on. Liveness is
        re-verified at every append, under the append lock."""
        log = RunLog.create(tmp_path)
        log.event("dev", "phase-spawn", {"model": "m", "attempt": 1})
        sibling = RunLog.attach(tmp_path)  # valid at attach time
        log.finish("run-complete")  # owner ends the run afterwards
        with pytest.raises(RunError, match="has ended"):
            sibling.event("merge", "copilot-round", {"round": 1})
        assert not log.is_live()  # the terminal record stayed terminal

    def test_attach_without_a_current_run_raises(self, tmp_path):
        with pytest.raises(RunError, match="no current run"):
            RunLog.attach(tmp_path)

    def test_attach_by_run_id_with_the_same_liveness_rules(self, tmp_path):
        """Named attach serves the control CLI's --run flag: same containment
        and liveness rules as the current-link path."""
        log = RunLog.create(tmp_path, run_id="run-a")
        log.event("dev", "phase-spawn", {"model": "m", "attempt": 1})
        sibling = RunLog.attach(tmp_path, run_id="run-a")
        sibling.event("merge", "copilot-round", {"round": 1})
        log.finish("run-complete")
        with pytest.raises(RunError, match="already ended"):
            RunLog.attach(tmp_path, run_id="run-a")
        with pytest.raises(RunError, match="no run"):
            RunLog.attach(tmp_path, run_id="run-x")

    def test_named_attach_rejects_symlink_aliases(self, tmp_path):
        """Round-8 finding: an in-tree alias (run-b -> run-a) passed the
        containment check, opened run A, and stamped run_id 'run-b' into A's
        history. Named runs must be real directories; only the dedicated
        'current' link is followed."""
        log = RunLog.create(tmp_path, run_id="run-a")
        log.event("dev", "phase-spawn", {"model": "m", "attempt": 1})
        (runs_root(tmp_path) / "run-b").symlink_to("run-a")
        with pytest.raises(RunError, match="symlink"):
            RunLog.attach(tmp_path, run_id="run-b")

    def test_append_after_a_torn_tail_repairs_instead_of_corrupting(self, tmp_path):
        """Round-3 finding: events() tolerates a torn FINAL line (crash
        artifact), but an append landing after it would turn the fragment
        into corrupt MIDDLE history and every later events() call would
        raise. Appends now truncate a recoverable torn tail under the append
        lock and record the repair."""
        log = RunLog.create(tmp_path)
        log.event("dev", "phase-spawn", {"model": "m", "attempt": 1})
        with open(log.run_dir / "events.jsonl", "a", encoding="utf-8") as fh:
            fh.write('{"ts": "2026-08-30T00:00:00Z", "event": "phase-ver')
        sibling = RunLog.attach(tmp_path)  # tolerant read: run is live
        sibling.event("merge", "copilot-round", {"round": 1})
        names = [e["event"] for e in log.events()]  # raised pre-fix (corrupt)
        assert "torn-tail-repaired" in names
        assert names[-1] == "copilot-round"
        assert "phase-spawn" in names  # intact history untouched

    def test_append_after_a_missing_final_newline_does_not_concatenate(
        self, tmp_path
    ):
        """Round-4 finding (suppressed): a crash can land exactly between a
        record's JSON bytes and its newline — the record is complete and
        readable, but a plain append would concatenate onto it, silently
        merging (and then losing) BOTH records. Appends normalize the
        delimiter; nothing is discarded, so no repair event either."""
        log = RunLog.create(tmp_path)
        with open(log.run_dir / "events.jsonl", "a", encoding="utf-8") as fh:
            fh.write(
                '{"ts": "2026-08-30T00:00:00Z", "run_id": "x", '
                '"phase": "dev", "event": "phase-spawn"}'
            )  # complete record, missing only its newline
        log.event("merge", "copilot-round", {"round": 1})
        names = [e["event"] for e in log.events()]
        assert names[-2:] == ["phase-spawn", "copilot-round"]
        assert "torn-tail-repaired" not in names


class TestPark:
    """Epic 9 rough edge #4 (lifecycle half): a pause PARKS the run — live
    and adoptable — it never ends it. The severity run died as `run-failed`
    with empty reasons because nothing owned this stance."""

    def test_park_is_non_terminal_and_keeps_the_run_adoptable(self, tmp_path):
        log = RunLog.create(tmp_path)
        log.event("lessons", "phase-spawn", {"model": "m", "attempt": 1})
        record = log.park("paused: guardrail", phase="lessons", story_key="s-1")
        assert record["event"] == "run-parked"
        assert log.is_live()
        adopted = RunLog.adopt(tmp_path)  # a parked run is precisely adoptable
        assert adopted.run_id == log.run_id

    def test_park_names_its_reason_in_the_event(self, tmp_path):
        log = RunLog.create(tmp_path)
        log.park("paused: 'lessons' guardrail spawn gate: stale-cache ...")
        ev = log.events()[-1]
        assert ev["detail"]["reason"].startswith("paused:")
        assert "adoptable" in ev["detail"]["note"]

    def test_parking_an_ended_run_raises(self, tmp_path):
        """A park on a closed run would misrepresent it as waiting."""
        log = RunLog.create(tmp_path)
        log.finish("run-failed", {"reason": "x"})
        with pytest.raises(RunError, match="ended"):
            log.park("too late")

    def test_torn_fragment_after_the_terminal_record_cannot_reopen_the_run(
        self, tmp_path
    ):
        """Round-6 finding (thread + suppressed twin): the repair marker is
        itself an append, and recording it BEFORE the terminal check let a
        torn fragment after run-complete smuggle the marker in as the new
        last event — reopening post-mortem history through the very
        mechanism meant to protect it. Truncate, validate the repaired
        prefix, only then record."""
        log = RunLog.create(tmp_path)
        log.finish("run-complete")
        with open(log.run_dir / "events.jsonl", "a", encoding="utf-8") as fh:
            fh.write('{"ts": "2026-08-30T00:00:00Z", "event": "phase-ver')
        with pytest.raises(RunError, match="post-mortem"):
            log.event("dev", "phase-spawn", {"model": "m", "attempt": 1})
        with pytest.raises(RunError, match="nothing to act on"):
            log.write_control("resume")
        names = [e["event"] for e in log.events()]
        assert names[-1] == "run-complete"  # the marker did not reopen it
        assert not log.is_live()
        assert log.read_controls() == []

    def test_no_handle_may_append_after_the_run_ends(self, tmp_path):
        """Round-5 finding: terminal enforcement was scoped to attach()
        handles, but ownership does not make post-mortem history safe — the
        owner (or an adopted handle) could event() after finish(), append a
        non-terminal record, and flip is_live() back on. EVERY handle now
        refuses, and a double finish() is a loud error instead of a second
        terminal record."""
        log = RunLog.create(tmp_path)
        log.finish("run-complete")
        with pytest.raises(RunError, match="post-mortem"):
            log.event("dev", "phase-spawn", {"model": "m", "attempt": 1})
        with pytest.raises(RunError, match="post-mortem"):
            log.finish("run-failed", {"reason": "double finish"})
        assert [e["event"] for e in log.events()][-1] == "run-complete"

    def test_failing_a_parked_run_is_refused(self, tmp_path):
        """Round-1 finding: the park record alone did not stop a driver from
        calling finish('run-failed') right after — the incident path with a
        nicer log. While the LAST event is run-parked, run-failed is refused;
        an operator abort stays available."""
        log = RunLog.create(tmp_path)
        log.event("lessons", "phase-spawn", {"model": "m", "attempt": 1})
        log.park("paused: guardrail", phase="lessons")
        with pytest.raises(RunError, match="parked"):
            log.finish("run-failed", {"reason": "lessons did not verify"})
        assert log.is_live()  # still adoptable after the refused finish
        log.finish("run-aborted", {"reason": "operator decision"})  # allowed

    def test_a_resumed_run_can_fail_again(self, tmp_path):
        """A progress signal after the park (a re-driven phase spawn) unparks
        — a phase that then genuinely fails may fail the run."""
        log = RunLog.create(tmp_path)
        log.park("paused: guardrail", phase="lessons")
        log.event("lessons", "phase-spawn", {"model": "m", "attempt": 1})
        log.finish("run-failed", {"reason": "lessons did not verify: [gate]"})
        assert not log.is_live()

    def test_bookkeeping_and_sibling_events_do_not_unpark(self, tmp_path):
        """Round-2 finding: adopt() appends run-adopted, so 'last event is
        run-parked' stopped holding the moment anyone adopted the parked run
        — and a sibling's evidence append would unpark it too. Parked is a
        lifecycle fact: only phase-spawn / control-resume clear it."""
        log = RunLog.create(tmp_path)
        log.event("lessons", "phase-spawn", {"model": "m", "attempt": 1})
        log.park("paused: guardrail", phase="lessons")
        adopted = RunLog.adopt(tmp_path)  # run-adopted: bookkeeping
        sibling = RunLog.attach(tmp_path)
        sibling.event("merge", "copilot-round", {"round": 1})  # evidence
        with pytest.raises(RunError, match="parked"):
            adopted.finish("run-failed", {"reason": "x"})
        assert log.is_live()

    def test_operator_resume_unparks(self, tmp_path):
        """control-resume is an explicit progress signal — after it, a
        genuine failure may fail the run."""
        log = RunLog.create(tmp_path)
        log.park("paused: guardrail", phase="lessons")
        log.write_control("resume")
        log.finish("run-failed", {"reason": "resumed work failed: [gate]"})
        assert not log.is_live()

    def test_park_racing_a_concurrent_finish_is_refused(self, tmp_path):
        """Round-2 finding (suppressed): park's ended-check lived OUTSIDE the
        append lock, so a terminal event landing between check and append let
        run-parked follow it and flip is_live() back on. The check now runs
        inside event()'s locked section — this pins the observable contract;
        the interleaving itself is closed by construction (one critical
        section)."""
        log = RunLog.create(tmp_path)
        log.event("dev", "phase-spawn", {"model": "m", "attempt": 1})
        sibling = RunLog.attach(tmp_path)
        log.finish("run-complete")
        with pytest.raises(RunError):
            sibling.park("too late")
        assert not log.is_live()
