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
        KeyError — torn tail drops, middle line is a clear RunError."""
        log = RunLog.create(tmp_path)
        with open(log.run_dir / "events.jsonl", "a", encoding="utf-8") as fh:
            fh.write('{"ts": "2026-08-28T23:00:00Z"}\n')
        assert log.is_live()  # tail dropped, no KeyError
        log.event("dev", "phase-spawn")  # now the damaged line is in the middle
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
        log = RunLog.create(tmp_path)
        with open(log.run_dir / "events.jsonl", "a", encoding="utf-8") as fh:
            fh.write("garbage not json\n")
        log_path_ok = log.event  # appending a valid event after the garbage
        log_path_ok("dev", "phase-spawn")
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

    def test_attach_without_a_current_run_raises(self, tmp_path):
        with pytest.raises(RunError, match="no current run"):
            RunLog.attach(tmp_path)


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
        with pytest.raises(RunError, match="cannot park"):
            log.park("too late")
