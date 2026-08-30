"""Run log: `.omater/runs/<run-id>/progress.log` + `events.jsonl`.

Two invariants from the design:

- **Write-ahead**: every state transition is written to `events.jsonl` BEFORE
  the action executes, so a fresh orchestrator can adopt an orphaned run by
  replaying events against reality (git, forge, DB) — run state is derived,
  never trusted from a status field.
- **One live run per project**: `runs/current` points at the live run; a new
  run cannot start while it is live (adopt or abort it instead).

`tail -f .omater/runs/current/progress.log` is the human view.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

OMATER_DIR = ".omater"
RUNS_DIR = "runs"
CURRENT_LINK = "current"
PROGRESS_LOG = "progress.log"
EVENTS_JSONL = "events.jsonl"
CONTROL_JSONL = "control.jsonl"
TRANSCRIPTS_DIR = "transcripts"
CREATE_LOCK = ".create.lock"
CREATE_LOCK_STALE_S = 60

# Events that end a run; a run whose last event is none of these is live.
TERMINAL_EVENTS = {"run-complete", "run-aborted", "run-failed"}

CONTROL_ACTIONS = ("resume", "abort", "approve")


class RunError(Exception):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def runs_root(project_root: Path | str) -> Path:
    return Path(project_root) / OMATER_DIR / RUNS_DIR


def _remove_lock(lock: Path) -> None:
    """Remove the create lock whatever it is. rmtree alone silently no-ops
    on a file/symlink (tampering, older versions), leaving run creation
    permanently wedged on 'in progress'."""
    try:
        if lock.is_symlink() or lock.is_file():
            lock.unlink(missing_ok=True)
        else:
            shutil.rmtree(lock, ignore_errors=True)
    except OSError:
        pass


def validate_run_id(run_id: str) -> str:
    """A run id is a simple directory name. Anything with path separators
    (or dot-names) could escape `.omater/runs/` — a path-traversal hole and
    a breach of one-live-run bookkeeping."""
    if (
        not run_id
        or run_id in (".", "..", CURRENT_LINK)
        or "/" in run_id
        or "\\" in run_id
    ):
        raise RunError(f"invalid run id {run_id!r}: must be a simple name")
    return run_id


def _path_component(value: str) -> str:
    """A story key / phase name becomes part of a transcript filename; strip
    anything that could escape the transcripts dir or hide the file."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).lstrip(".")
    return cleaned or "unnamed"


# Bookkeeping phases: events under these carry no recoverable phase work, so
# attaching to a run that has ONLY them is a first attach, not an adoption.
_BOOKKEEPING_PHASES = {"run", "control", "notify"}


def _detail_summary(detail: Any) -> str:
    if detail is None:
        return ""
    if isinstance(detail, str):
        return detail
    return json.dumps(detail, sort_keys=True, ensure_ascii=False)


class RunLog:
    def __init__(self, run_dir: Path, run_id: str):
        self.run_dir = run_dir
        self.run_id = run_id

    # ---- lifecycle -------------------------------------------------------

    @classmethod
    def create(cls, project_root: Path | str, run_id: str | None = None) -> "RunLog":
        root = runs_root(project_root)
        root.mkdir(parents=True, exist_ok=True)
        # One-live-run is check-then-act; without a lock, two concurrent
        # starts both pass the liveness check and the last `current` repoint
        # wins, orphaning the other run. mkdir is the atomic mutex (same
        # pattern the statusline uses); a crash while holding it goes stale
        # and is broken after CREATE_LOCK_STALE_S.
        lock = root / CREATE_LOCK
        try:
            lock.mkdir()
        except FileExistsError:
            try:
                # lstat, not stat: a symlink-shaped lock must be aged by the
                # link itself, not whatever it points at (which may be fresh,
                # missing, or outside the repo entirely)
                age = time.time() - lock.lstat().st_mtime
            except OSError:
                age = 0.0
            if age <= CREATE_LOCK_STALE_S:
                raise RunError(
                    f"another run creation is in progress (lock {lock})"
                ) from None
            _remove_lock(lock)
            try:
                lock.mkdir()
            except FileExistsError:
                raise RunError(
                    f"another run creation is in progress (lock {lock})"
                ) from None
        try:
            return cls._create_locked(root, run_id)
        finally:
            _remove_lock(lock)

    @classmethod
    def _create_locked(cls, root: Path, run_id: str | None) -> "RunLog":
        current = root / CURRENT_LINK
        if current.exists() and not current.is_symlink():
            # Fail BEFORE creating the run dir — failing later (in
            # _point_current) leaks an orphan run folder per attempt.
            raise RunError(
                f"{current} exists but is not a symlink; remove it manually "
                "before starting a run"
            )
        if current.is_symlink() or current.exists():
            live = cls._attach(current)
            if live is not None and live.is_live():
                raise RunError(
                    f"a live run already exists ({live.run_id}); "
                    "adopt it or abort it before starting a new one"
                )
        run_id = validate_run_id(
            run_id
            or (time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2))
        )
        run_dir = root / run_id
        try:
            run_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            raise RunError(f"run id {run_id!r} already exists") from None
        log = cls(run_dir, run_id)
        try:
            (run_dir / TRANSCRIPTS_DIR).mkdir()
            log._point_current()
            log.event("run", "run-created", {"run_id": run_id})
        except BaseException:
            # Failing halfway must not strand an orphan run dir or leave
            # `current` dangling at a dir we are about to remove.
            shutil.rmtree(run_dir, ignore_errors=True)
            try:
                if current.is_symlink() and os.readlink(current) == run_id:
                    current.unlink()
            except OSError:
                pass
            (root / f".{CURRENT_LINK}.tmp").unlink(missing_ok=True)
            raise
        return log

    @classmethod
    def adopt(cls, project_root: Path | str) -> "RunLog":
        """Attach to the current run. The caller replays `events()` against
        reality; state is derived, never trusted.

        Two verbs, one mechanism: a run with phase activity logs
        `run-adopted` (dead-orchestrator recovery — there is orphaned state to
        derive), while a fresh run that has only bookkeeping events logs
        `run-attached` (`omater start` + a separate orchestrator process is
        the normal shape, not a crash)."""
        current = runs_root(project_root) / CURRENT_LINK
        log = cls._attach(current)
        if log is None:
            raise RunError(f"no current run under {current}")
        events = log.events()
        if not events or events[-1]["event"] in TERMINAL_EVENTS:
            # A finished (or never-committed) run must stay that way —
            # "adopting" it would flip it live again and wedge one-live-run
            # enforcement (is_live() says the same thing to create()).
            raise RunError(
                f"run {log.run_id} already ended; start a new run instead"
            )
        has_phase_activity = any(
            ev.get("phase") not in _BOOKKEEPING_PHASES for ev in events
        )
        verb = "run-adopted" if has_phase_activity else "run-attached"
        log.event("run", verb, {"pid": os.getpid()})
        return log

    @classmethod
    def attach(cls, project_root: Path | str) -> "RunLog":
        """Attach to the live run WITHOUT writing any bookkeeping event —
        the seam for sibling processes that append events to a run they do
        not own (a merge-phase driver logging per-round evidence, a control
        CLI). Epic 9's merge driver had to call `adopt()` once per event and
        stamped a `run-adopted` (dead-orchestrator recovery) event each time
        — noise that misdescribes what happened. Orchestrator takeover, with
        its recorded verb, stays `adopt()`.

        Raises RunError when there is no current run or it already ended —
        appending to a closed run would forge post-mortem history."""
        current = runs_root(project_root) / CURRENT_LINK
        log = cls._attach(current)
        if log is None:
            raise RunError(f"no current run under {current}")
        events = log.events()
        if not events or events[-1]["event"] in TERMINAL_EVENTS:
            raise RunError(
                f"run {log.run_id} already ended; nothing to attach to"
            )
        return log

    @classmethod
    def _attach(cls, current: Path) -> "RunLog | None":
        try:
            run_dir = current.resolve(strict=True)
        except OSError:
            return None
        if not run_dir.is_dir():
            return None
        # The current link must point at an immediate child of the runs
        # root — a hand-edited symlink to an arbitrary path would make
        # adopt/control write logs outside .omater/runs and defeat
        # one-live-run bookkeeping. Tampering fails loudly, never silently.
        if run_dir.parent != current.parent.resolve():
            raise RunError(
                f"{current} points outside the runs directory ({run_dir}); "
                "remove the symlink manually before continuing"
            )
        return cls(run_dir, run_dir.name)

    def _point_current(self) -> None:
        current = self.run_dir.parent / CURRENT_LINK
        if current.exists() and not current.is_symlink():
            # A stray real file/dir here would make replace() raise a bare
            # OSError (dir case) or be clobbered silently (file case) —
            # fail loudly instead of guessing what left it behind.
            raise RunError(
                f"{current} exists but is not a symlink; remove it manually "
                "before starting a run"
            )
        tmp = self.run_dir.parent / f".{CURRENT_LINK}.tmp"
        try:
            tmp.unlink(missing_ok=True)
            tmp.symlink_to(self.run_dir.name)
            tmp.replace(current)
        except OSError as exc:
            raise RunError(f"cannot point {current} at {self.run_id}: {exc}") from exc

    # ---- events ----------------------------------------------------------

    def event(
        self,
        phase: str,
        event: str,
        detail: Any = None,
        story_key: str | None = None,
    ) -> dict[str, Any]:
        """Append one event (jsonl + progress line), flushed to disk before
        returning — call this BEFORE performing the action it describes."""
        record = {
            "ts": _utc_now(),
            "run_id": self.run_id,
            "phase": phase,
            "event": event,
        }
        if story_key:
            record["story_key"] = story_key
        if detail is not None:
            record["detail"] = detail

        with open(self.run_dir / EVENTS_JSONL, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

        summary = _detail_summary(detail)
        line = f"{record['ts']} [{phase}] {event}"
        if story_key:
            line += f" story={story_key}"
        if summary:
            line += f": {summary}"
        with open(self.run_dir / PROGRESS_LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return record

    def _read_jsonl_tolerant(
        self, filename: str, required_key: str
    ) -> list[dict[str, Any]]:
        """Read an append-only jsonl file, tolerating a torn FINAL line (what
        a crash mid-append leaves behind; the write-ahead discipline means the
        action it described never committed). Corrupt middle lines raise
        RunError — that is damage, not a crash artifact. An entry missing
        `required_key` counts as corrupt: letting it through would surface
        later as a bare KeyError in is_live()/consumers instead of a clear
        message naming the damaged line."""
        path = self.run_dir / filename
        if not path.exists():
            return []
        # errors="replace", not strict: events carry ensure_ascii=False text,
        # so a torn append can cut a multi-byte UTF-8 sequence — a strict
        # decode would raise before line splitting and defeat the torn-tail
        # tolerance. The replacement char only mangles the torn line, which
        # the JSON parse below then rejects as usual.
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = [line for line in text.splitlines() if line.strip()]
        out = []
        for i, line in enumerate(lines):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                obj = None
            # Valid-but-non-object JSON (a torn line can parse as a bare
            # number or string) gets the same treatment as unparsable:
            # crashing later in is_live()/adoption with a KeyError would
            # hide WHERE the damage is.
            if not isinstance(obj, dict) or required_key not in obj:
                if i == len(lines) - 1:
                    break
                raise RunError(
                    f"corrupt {filename} at line {i + 1} in {self.run_dir}: "
                    f"not a JSON object with {required_key!r} "
                    "(only a torn FINAL line is recoverable)"
                ) from None
            out.append(obj)
        return out

    def events(self) -> list[dict[str, Any]]:
        return self._read_jsonl_tolerant(EVENTS_JSONL, "event")

    def is_live(self) -> bool:
        evs = self.events()
        return bool(evs) and evs[-1]["event"] not in TERMINAL_EVENTS

    def finish(self, status: str = "run-complete", detail: Any = None) -> None:
        if status not in TERMINAL_EVENTS:
            raise RunError(f"not a terminal event: {status}")
        self.event("run", status, detail)

    def park(
        self,
        reason: str,
        phase: str = "run",
        story_key: str | None = None,
    ) -> dict[str, Any]:
        """Record that the run is deliberately LIVE-AND-WAITING (guardrail
        pause, quota window, operator hold). Non-terminal by design: a parked
        run stays adoptable via `adopt`/`attach`. This is the core-owned fix
        for the Epic 9 incident where a driver terminated a paused run as
        `run-failed` with empty reasons — the runner parks, and whoever reads
        the log sees the run is waiting, not dead. Parking an ended run
        raises: that would misrepresent a closed run as waiting."""
        events = self.events()
        if events and events[-1]["event"] in TERMINAL_EVENTS:
            raise RunError(f"run {self.run_id} already ended; cannot park it")
        return self.event(
            phase,
            "run-parked",
            {"reason": reason, "note": "run left live/adoptable on purpose"},
            story_key=story_key,
        )

    # ---- paths -----------------------------------------------------------

    def transcript_path(
        self,
        phase: str,
        attempt: int,
        story_key: str | None = None,
        ts: str | None = None,
        suffix: str = ".md",
    ) -> Path:
        """Transcript file for one phase attempt. The name carries the story
        key and the phase-spawn timestamp: without them every story's
        `dev-attempt-1` overwrites the previous story's (a 5-story sandbox run
        kept exactly one dev transcript), and a crash-recovery re-drive of the
        same story overwrites its own attempt 1."""
        # Every caller-supplied component is sanitized — this is public API,
        # and an unsanitized ts/suffix would be a path escape the story_key
        # handling already closes.
        if not re.fullmatch(r"\.[A-Za-z0-9]+", suffix):
            raise RunError(f"invalid transcript suffix {suffix!r}")
        parts = []
        if story_key:
            parts.append(_path_component(story_key))
        parts.append(_path_component(phase))
        parts.append(f"attempt-{attempt}")
        if ts:
            # event timestamps are %Y-%m-%dT%H:%M:%SZ; strip the separators
            # that are hostile in filenames, then sanitize like any component
            parts.append(_path_component(re.sub(r"[:-]", "", ts)))
        return self.run_dir / TRANSCRIPTS_DIR / ("-".join(parts) + suffix)

    # ---- inbound control (omater resume | abort | approve) ---------------

    def write_control(self, action: str, detail: Any = None) -> dict[str, Any]:
        if action not in CONTROL_ACTIONS:
            raise RunError(f"unknown control action: {action}")
        record = {"ts": _utc_now(), "action": action}
        if detail is not None:
            record["detail"] = detail
        with open(self.run_dir / CONTROL_JSONL, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        self.event("control", f"control-{action}", detail)
        return record

    def read_controls(self) -> list[dict[str, Any]]:
        return self._read_jsonl_tolerant(CONTROL_JSONL, "action")
