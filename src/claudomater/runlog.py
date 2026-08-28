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
import secrets
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

# Events that end a run; a run whose last event is none of these is live.
TERMINAL_EVENTS = {"run-complete", "run-aborted", "run-failed"}

CONTROL_ACTIONS = ("resume", "abort", "approve")


class RunError(Exception):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def runs_root(project_root: Path | str) -> Path:
    return Path(project_root) / OMATER_DIR / RUNS_DIR


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
        (run_dir / TRANSCRIPTS_DIR).mkdir()
        log = cls(run_dir, run_id)
        log._point_current()
        log.event("run", "run-created", {"run_id": run_id})
        return log

    @classmethod
    def adopt(cls, project_root: Path | str) -> "RunLog":
        """Attach to the current run (dead orchestrator recovery). The caller
        replays `events()` against reality; state is derived, never trusted."""
        current = runs_root(project_root) / CURRENT_LINK
        log = cls._attach(current)
        if log is None:
            raise RunError(f"no current run under {current}")
        if not log.is_live():
            # A finished run must stay finished — "adopting" it would flip it
            # live again and wedge one-live-run enforcement.
            raise RunError(
                f"run {log.run_id} already ended; start a new run instead"
            )
        log.event("run", "run-adopted", {"pid": os.getpid()})
        return log

    @classmethod
    def _attach(cls, current: Path) -> "RunLog | None":
        try:
            run_dir = current.resolve(strict=True)
        except OSError:
            return None
        if not run_dir.is_dir():
            return None
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

    def _read_jsonl_tolerant(self, filename: str) -> list[dict[str, Any]]:
        """Read an append-only jsonl file, tolerating a torn FINAL line (what
        a crash mid-append leaves behind; the write-ahead discipline means the
        action it described never committed). Corrupt middle lines raise
        RunError — that is damage, not a crash artifact."""
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
                out.append(json.loads(line))
            except json.JSONDecodeError:
                if i == len(lines) - 1:
                    break
                raise RunError(
                    f"corrupt {filename} at line {i + 1} in {self.run_dir}: "
                    "not valid JSON (only a torn FINAL line is recoverable)"
                ) from None
        return out

    def events(self) -> list[dict[str, Any]]:
        return self._read_jsonl_tolerant(EVENTS_JSONL)

    def is_live(self) -> bool:
        evs = self.events()
        return bool(evs) and evs[-1]["event"] not in TERMINAL_EVENTS

    def finish(self, status: str = "run-complete", detail: Any = None) -> None:
        if status not in TERMINAL_EVENTS:
            raise RunError(f"not a terminal event: {status}")
        self.event("run", status, detail)

    # ---- paths -----------------------------------------------------------

    def transcript_path(self, phase: str, attempt: int) -> Path:
        return self.run_dir / TRANSCRIPTS_DIR / f"{phase}-attempt-{attempt}.md"

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
        return self._read_jsonl_tolerant(CONTROL_JSONL)
