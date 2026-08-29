"""Subagent phase runner.

Each phase is a spawned agent with an explicit contract: model, prompt,
structured JSON result, and verifiers the orchestrator runs against reality
before advancing. An agent that ends without its JSON result is a failed
phase — retried once, then escalated. No pane scraping, no idle-verb regex.

Survival contracts honored here:
- write-ahead: the spawn intent is logged BEFORE the executor runs;
- commit-first: salvaging a crashed phase's work means COMMITTING it (as
  `wip(phase-crash)`) — stashing or restoring files is forbidden, and the
  salvage is the orchestrator's job, not the replacement agent's guess;
- everything retained or sent outward (transcripts, verifier output in run
  events, escalation notifications) is scrubbed against `secrets_deny`.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from claudomater import notify as notify_mod
from claudomater.config import UserConfig
from claudomater.guardrails import Decision, model_for_phase
from claudomater.runlog import RunLog
from claudomater.scrub import scrub_text
from claudomater.verifiers import VerifierContext, run_verifiers

DEFAULT_TIMEOUT_S = 3600


@dataclass
class PhaseSpec:
    name: str  # create | dev | sr-review | test | merge | close | ...
    model: str
    prompt: str
    required_fields: tuple[str, ...] = ()
    verifiers: list[Any] = field(default_factory=list)
    timeout_s: int = DEFAULT_TIMEOUT_S
    retries: int = 1  # retried once, then escalated
    story_key: str | None = None
    escalated: bool = False  # story has failure history: never runs degraded


@dataclass
class ExecutionResult:
    text: str
    returncode: int = 0
    token_usage: dict[str, Any] | None = None  # cost accounting per phase


class Executor(Protocol):
    def run(self, spec: PhaseSpec, model: str) -> ExecutionResult: ...


class PhaseTimeout(Exception):
    def __init__(self, message: str, partial_text: str | None = None):
        super().__init__(message)
        # whatever the agent produced before the timeout — the attempts most
        # in need of a post-mortem must still leave a transcript
        self.partial_text = partial_text


class ClaudeCliExecutor:
    """Spawns a phase agent as a headless `claude -p` run.

    Named decisions (not accidents):
    - `cwd` is the project root, so the agent's relative paths and git
      operations land in the project it is working on;
    - permissions default to `bypassPermissions` — headless runs would
      otherwise deny every un-granted tool and no dev phase could write a
      file. The containment for bypassed permissions is the PreToolUse write
      fence provisioned by `omater init` (design §3/§12), which is why
      `omater start` refuses to run when that hook has drifted.
    - `--output-format json` gives us the result text plus token usage for
      the run report's cost accounting.
    """

    def __init__(
        self,
        claude_bin: str = "claude",
        extra_args: list[str] | None = None,
        cwd: Path | str | None = None,
        permission_mode: str | None = "bypassPermissions",
    ):
        self.claude_bin = claude_bin
        self.extra_args = extra_args or []
        self.cwd = Path(cwd) if cwd else None
        self.permission_mode = permission_mode

    def build_argv(self, spec: PhaseSpec, model: str) -> list[str]:
        argv = [
            self.claude_bin,
            "-p",
            spec.prompt,
            "--model",
            model,
            "--output-format",
            "json",
        ]
        if self.permission_mode:
            argv += ["--permission-mode", self.permission_mode]
        return argv + list(self.extra_args)

    def run(self, spec: PhaseSpec, model: str) -> ExecutionResult:
        try:
            proc = subprocess.run(
                self.build_argv(spec, model),
                capture_output=True,
                text=True,
                timeout=spec.timeout_s,
                cwd=self.cwd,
            )
        except subprocess.TimeoutExpired as exc:
            partial = exc.stdout
            if isinstance(partial, bytes):
                partial = partial.decode("utf-8", errors="replace")
            raise PhaseTimeout(
                f"phase {spec.name!r} exceeded {spec.timeout_s}s",
                partial_text=partial,
            ) from exc
        text, usage = proc.stdout, None
        try:
            payload = json.loads(proc.stdout)
            if isinstance(payload, dict):
                result_text = payload.get("result")
                # string check, not truthiness: an EMPTY result must stay
                # empty — falling back to the raw JSON wrapper pollutes the
                # transcript and can be mis-parsed as the phase result
                if isinstance(result_text, str):
                    text = result_text
                usage = payload.get("usage")
        except (json.JSONDecodeError, ValueError):
            pass
        return ExecutionResult(text=text, returncode=proc.returncode, token_usage=usage)


_JSON_FENCE = re.compile(r"```json\s*\n(.*?)```", re.DOTALL)


def extract_json_result(text: str) -> dict[str, Any] | None:
    """The phase contract: the agent's output must carry a JSON object result.
    Takes the LAST ```json fence, else the last balanced top-level object."""
    fences = _JSON_FENCE.findall(text)
    for candidate in reversed(fences):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError):
            continue

    # Fallback: scan for the last balanced {...} that parses as an object.
    decoder = json.JSONDecoder()
    last: dict[str, Any] | None = None
    idx = 0
    while True:
        start = text.find("{", idx)
        if start == -1:
            break
        try:
            obj, end = decoder.raw_decode(text[start:])
            if isinstance(obj, dict):
                last = obj
                idx = start + end
                continue
        except (json.JSONDecodeError, ValueError):
            pass
        idx = start + 1
    return last


def salvage_uncommitted(project_root: Path, message: str = "wip(phase-crash)") -> bool:
    """Commit any uncommitted work before a respawn — salvage means a COMMIT,
    never a stash or a file restore. Returns True if a commit was made.

    Run logs never ride along: dirtiness is judged with `.omater` excluded,
    and anything under it is unstaged before the commit (`git add -A` with an
    exclude pathspec exits 1 on gitignored dirs, hence add-then-reset)."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(project_root), "status", "--porcelain", "--", ":(exclude).omater"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if proc.returncode != 0 or not proc.stdout.strip():
        return False
    def _unstage() -> None:
        # Side-effect-free unless salvage succeeds: mixed reset unstages
        # whatever `add -A` managed before failing, never touching files.
        subprocess.run(
            ["git", "-C", str(project_root), "reset", "-q"],
            capture_output=True,
            timeout=60,
        )

    add = subprocess.run(
        ["git", "-C", str(project_root), "add", "-A"],
        capture_output=True,
        timeout=60,
    )
    if add.returncode != 0:
        _unstage()
        return False
    subprocess.run(
        ["git", "-C", str(project_root), "reset", "-q", "--", ".omater"],
        capture_output=True,
        timeout=60,
    )
    commit = subprocess.run(
        [
            "git",
            "-C",
            str(project_root),
            "commit",
            "-m",
            f"{message}: salvage uncommitted work before phase retry",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if commit.returncode != 0:
        _unstage()
        return False
    return True


@dataclass
class PhaseOutcome:
    phase: str
    status: str  # verified | escalated | paused | skipped
    result: dict[str, Any] | None = None
    model: str | None = None
    attempts: int = 0
    verdicts: list[dict[str, Any]] = field(default_factory=list)
    failure_reasons: list[str] = field(default_factory=list)
    pause_reason: str | None = None


class PhaseRunner:
    def __init__(
        self,
        project_root: Path | str,
        runlog: RunLog,
        executor: Executor,
        notifier: notify_mod.Notifier | None = None,
        user_config: UserConfig | None = None,
        secrets_deny: tuple[str, ...] | list[str] = (),
        guardrail_check: Callable[[], Decision] | None = None,
        project: str | None = None,
    ):
        self.project_root = Path(project_root)
        self.runlog = runlog
        self.executor = executor
        self.notifier = notifier
        self.user_config = user_config or UserConfig()
        self.secrets_deny = tuple(secrets_deny)
        self.guardrail_check = guardrail_check
        self.project = project

    # ---- helpers ---------------------------------------------------------

    def _scrub(self, text: str) -> str:
        return scrub_text(text, self.secrets_deny)

    def _notify(self, kind: str, message: str, detail: dict | None = None) -> None:
        if not self.notifier:
            return
        delivered = self.notifier.notify(kind, message, project=self.project, detail=detail)
        if not delivered:
            self.runlog.event(
                "notify", "notify-failed", {"kind": kind, "error": self.notifier.last_error}
            )

    def _gate(self, spec: PhaseSpec) -> tuple[str | None, str | None]:
        """No NEW phase spawns after a threshold trips. A running phase is
        allowed to finish — this gate runs only at spawn time."""
        if not self.guardrail_check:
            return spec.model, None
        decision = self.guardrail_check()
        self.runlog.event(
            spec.name, "guardrail-check", decision.as_dict(), story_key=spec.story_key
        )
        model, reason = model_for_phase(
            spec.model, decision, self.user_config, escalated=spec.escalated
        )
        if model is None:
            self.runlog.event(
                spec.name, "phase-paused", {"reason": reason}, story_key=spec.story_key
            )
            self._notify(
                notify_mod.PAUSED_QUOTA,
                f"phase {spec.name!r} paused: {reason}",
                detail={"resets_at": decision.resets_at},
            )
        elif model != spec.model:
            self.runlog.event(
                spec.name,
                "phase-degraded",
                {"from": spec.model, "to": model, "reason": reason},
                story_key=spec.story_key,
            )
            self._notify(notify_mod.DEGRADED, f"phase {spec.name!r}: {reason}")
        return model, reason

    def _salvage(self, spec: PhaseSpec) -> None:
        """Commit-first salvage, write-ahead: the intent is logged before git
        runs so an adopting orchestrator knows a salvage may exist."""
        self.runlog.event(spec.name, "salvage-attempt", story_key=spec.story_key)
        if salvage_uncommitted(self.project_root):
            self.runlog.event(
                spec.name,
                "salvage-committed",
                {"commit": "wip(phase-crash)"},
                story_key=spec.story_key,
            )

    # ---- the runner ------------------------------------------------------

    def run_phase(self, spec: PhaseSpec) -> PhaseOutcome:
        if spec.model == "skip":
            self.runlog.event(spec.name, "phase-skipped", story_key=spec.story_key)
            return PhaseOutcome(phase=spec.name, status="skipped")

        outcome = PhaseOutcome(phase=spec.name, status="escalated")
        max_attempts = spec.retries + 1

        for attempt in range(1, max_attempts + 1):
            # Every spawn — retries included — passes the guardrail gate:
            # no NEW phase agent starts after a threshold trips, and a
            # degrade that lands between attempts applies to the respawn.
            model, gate_reason = self._gate(spec)
            if model is None:
                outcome.status = "paused"
                outcome.pause_reason = gate_reason
                return outcome
            outcome.model = model
            outcome.attempts = attempt

            if attempt > 1:
                # A retry always starts from the branch's last committed
                # state; salvage happens before the respawn.
                self._salvage(spec)

            # Write-ahead: intent hits the log BEFORE the agent spawns.
            self.runlog.event(
                spec.name,
                "phase-spawn",
                {"model": model, "attempt": attempt, "timeout_s": spec.timeout_s},
                story_key=spec.story_key,
            )

            failure: str | None = None
            exec_result: ExecutionResult | None = None
            transcript_text: str | None = None
            try:
                exec_result = self.executor.run(spec, model)
                transcript_text = exec_result.text
            except PhaseTimeout as exc:
                failure = f"timeout: {exc}"
                transcript_text = exc.partial_text

            # Empty output is still a captured transcript ("the agent
            # produced nothing" is post-mortem information); only a timeout
            # that yielded no partial text leaves no file.
            if transcript_text is not None:
                path = self.runlog.transcript_path(spec.name, attempt)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(self._scrub(transcript_text), encoding="utf-8")

            result: dict[str, Any] | None = None
            if exec_result is not None and exec_result.returncode != 0:
                # A JSON blob in the output of a FAILED executor run is not a
                # result — the agent errored; treat the attempt as failed.
                failure = f"executor-failed: exit {exec_result.returncode}"
            elif exec_result is not None:
                result = extract_json_result(exec_result.text)
                if result is None:
                    failure = "no-structured-result: agent ended without its JSON result"
                else:
                    missing = [f for f in spec.required_fields if f not in result]
                    if missing:
                        failure = f"result missing required fields: {missing}"

            if failure is None and result is not None:
                ok, verdicts = run_verifiers(
                    spec.verifiers,
                    VerifierContext(project_root=self.project_root, result=result),
                )
                # Verifier output can quote command stdout/stderr — scrub it
                # before it reaches the run log or a notification.
                outcome.verdicts = [
                    {**v.as_dict(), "detail": self._scrub(v.detail)} for v in verdicts
                ]
                detail: dict[str, Any] = {
                    "attempt": attempt,
                    "verdicts": outcome.verdicts,
                }
                if exec_result and exec_result.token_usage:
                    detail["token_usage"] = exec_result.token_usage
                if ok:
                    self.runlog.event(
                        spec.name, "phase-verified", detail, story_key=spec.story_key
                    )
                    outcome.status = "verified"
                    outcome.result = result
                    return outcome
                failed = [v for v in verdicts if not v.ok]
                failure = "verifier-failed: " + "; ".join(
                    self._scrub(f"{v.name}: {v.detail}") for v in failed
                )

            outcome.failure_reasons.append(self._scrub(failure or "unknown failure"))
            fail_detail: dict[str, Any] = {
                "attempt": attempt,
                "reason": outcome.failure_reasons[-1],
            }
            if exec_result and exec_result.token_usage:
                fail_detail["token_usage"] = exec_result.token_usage
            self.runlog.event(
                spec.name, "phase-failed", fail_detail, story_key=spec.story_key
            )

        # The final attempt may also have died mid-write; leave the branch
        # committed for whoever picks the story up.
        self._salvage(spec)
        self.runlog.event(
            spec.name,
            "phase-escalated",
            {"attempts": outcome.attempts, "reasons": outcome.failure_reasons},
            story_key=spec.story_key,
        )
        self._notify(
            notify_mod.ESCALATED,
            f"phase {spec.name!r} escalated after {outcome.attempts} attempts: "
            + "; ".join(outcome.failure_reasons),
        )
        return outcome
