"""Verifiers: the orchestrator checks a phase's claims against reality
(files, git, commands) before advancing. An agent's structured result is a
claim; the verifier verdict is what decides pass/fail.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Any, Callable


@dataclass
class Verdict:
    name: str
    ok: bool
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass
class VerifierContext:
    project_root: Path
    result: dict[str, Any] = field(default_factory=dict)


Verifier = Callable[[VerifierContext], Verdict]


class VerifierError(Exception):
    pass


# ---- built-in verifier factories ----------------------------------------


def files_exist(*patterns: str) -> Verifier:
    """Every pattern (path or glob, relative to the project root) matches
    at least one existing file."""

    def check(ctx: VerifierContext) -> Verdict:
        missing = []
        for pattern in patterns:
            if any(ctx.project_root.glob(pattern)):
                continue
            if (ctx.project_root / pattern).exists():
                continue
            missing.append(pattern)
        if missing:
            return Verdict("files_exist", False, f"missing: {', '.join(missing)}")
        return Verdict("files_exist", True, f"all present: {', '.join(patterns)}")

    return check


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, timeout=60
    )


def git_branch_exists(branch: str) -> Verifier:
    def check(ctx: VerifierContext) -> Verdict:
        proc = _git(
            ctx.project_root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"
        )
        ok = proc.returncode == 0
        return Verdict(
            "git_branch_exists",
            ok,
            f"branch {branch!r} {'exists' if ok else 'not found'}",
        )

    return check


def git_worktree_clean() -> Verifier:
    """Commit-first is law: a finished phase leaves no uncommitted work."""

    def check(ctx: VerifierContext) -> Verdict:
        proc = _git(ctx.project_root, "status", "--porcelain")
        if proc.returncode != 0:
            return Verdict("git_worktree_clean", False, proc.stderr.strip())
        dirty = proc.stdout.strip()
        if dirty:
            return Verdict(
                "git_worktree_clean", False, f"uncommitted changes:\n{dirty}"
            )
        return Verdict("git_worktree_clean", True, "worktree clean")

    return check


def result_field(name: str, expected: Any = ...) -> Verifier:
    """The structured result has field `name` (and equals `expected` if given)."""

    def check(ctx: VerifierContext) -> Verdict:
        if name not in ctx.result:
            return Verdict("result_field", False, f"result missing field {name!r}")
        if expected is not ... and ctx.result[name] != expected:
            return Verdict(
                "result_field",
                False,
                f"result[{name!r}] == {ctx.result[name]!r}, expected {expected!r}",
            )
        return Verdict("result_field", True, f"result[{name!r}] ok")

    return check


def result_file_exists(
    name: str, artifact_roots: str | list[str] | tuple[str, ...] = ()
) -> Verifier:
    """The file the agent NAMED in result field `name` exists inside the
    project root — the result-aware companion to `files_exist`. `files_exist`
    checks a glob the spec author guessed in advance; this checks the path
    the agent actually claimed, so a result that names a file which was never
    written (or landed outside the project) fails on its own words.
    Containment is part of the check: a path resolving outside the project
    root fails even if it exists — an out-of-tree artifact is a fence
    escape, not a deliverable.

    `artifact_roots` is the declared exception (parity finding F1): an
    in-tree directory that is deliberately a symlink to another checkout
    (the artifact checkout is a separate git repo) resolves outside the
    project root and failed containment for a legitimate artifact — a $12
    live failure. Each entry names an in-tree RELATIVE path whose resolved
    location is trusted; a claimed path resolving under one passes
    containment. The declaration is narrow by construction: an absolute or
    `..`-carrying entry is refused (the policy is "this in-tree directory
    may point elsewhere", never "also allow that other location"), and
    nothing outside the declared roots gains anything."""
    # str | list | tuple ONLY: any other iterable (a dict from a YAML
    # misdeclaration would silently become its KEYS, a set would reorder)
    # is refused outright — containment exceptions must be legible.
    if isinstance(artifact_roots, str):
        roots = [artifact_roots]
    elif isinstance(artifact_roots, (list, tuple)):
        roots = list(artifact_roots)
    else:
        raise VerifierError(
            "artifact_roots must be a string, or a list/tuple of strings, "
            f"got {type(artifact_roots).__name__}"
        )
    for ar in roots:
        # A misdeclared spec must fail closed at build time, not silently
        # widen containment: run_verifiers turns this raise into a failed
        # verifier-error verdict.
        if not isinstance(ar, str) or not ar:
            raise VerifierError(
                f"artifact root must be a non-empty string, got {ar!r}"
            )
        parts = PurePath(ar).parts
        if PurePath(ar).is_absolute() or ".." in parts:
            raise VerifierError(
                f"artifact root {ar!r} must be a relative in-tree path "
                "without '..'"
            )

    def check(ctx: VerifierContext) -> Verdict:
        if name not in ctx.result:
            return Verdict(
                "result_file_exists", False, f"result missing field {name!r}"
            )
        value = ctx.result[name]
        if not isinstance(value, str) or not value:
            return Verdict(
                "result_file_exists",
                False,
                f"result[{name!r}] is not a path string: {value!r}",
            )
        root = ctx.project_root.resolve()
        try:
            # `root / value` keeps `value` intact when it is absolute
            path = (root / value).resolve()
        except (OSError, RuntimeError) as exc:  # symlink loops included
            return Verdict("result_file_exists", False, f"{value!r}: {exc}")
        allowed = [root]
        for ar in roots:
            try:
                allowed.append((root / ar).resolve())
            except (OSError, RuntimeError) as exc:
                # Silently skipping the root would fail closed but LIE about
                # why (the containment message would blame the artifact
                # path) — and a misleading verifier message is itself a
                # hazard (F3). Name the actual problem.
                return Verdict(
                    "result_file_exists",
                    False,
                    f"declared artifact root {ar!r} cannot be resolved: {exc}",
                )
        if not any(path == base or base in path.parents for base in allowed):
            where = "the project root"
            if roots:
                where += f" and declared artifact roots {roots}"
            return Verdict(
                "result_file_exists",
                False,
                f"result[{name!r}] = {value!r} resolves outside {where}",
            )
        # is_file, not exists: naming a directory (".", the project root...)
        # would let an agent pass without producing any artifact at all
        if not path.is_file():
            return Verdict(
                "result_file_exists",
                False,
                f"result[{name!r}] = {value!r} is not an existing file",
            )
        return Verdict("result_file_exists", True, f"result[{name!r}] -> {value!r} exists")

    return check


def command_ok(*argv: Any, timeout: int = 1800) -> Verifier:
    """An arbitrary reality check (test gauntlet, linter) exits 0.

    Accepts both `command_ok("pytest", "-q")` (what the declarative
    `{command_ok: [...]}` form splats into) and `command_ok(["pytest", "-q"])`.
    """
    if len(argv) == 1 and isinstance(argv[0], (list, tuple)):
        argv = tuple(argv[0])
    argv = [str(a) for a in argv]

    def check(ctx: VerifierContext) -> Verdict:
        try:
            proc = subprocess.run(
                argv,
                cwd=ctx.project_root,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return Verdict("command_ok", False, f"{argv}: {exc}")
        ok = proc.returncode == 0
        tail = (proc.stdout + proc.stderr)[-2000:]
        return Verdict(
            "command_ok", ok, f"{' '.join(argv)} -> exit {proc.returncode}\n{tail}"
        )

    return check


BUILTINS: dict[str, Callable[..., Verifier]] = {
    "files_exist": files_exist,
    "git_branch_exists": git_branch_exists,
    "git_worktree_clean": git_worktree_clean,
    "result_field": result_field,
    "result_file_exists": result_file_exists,
    "command_ok": command_ok,
}


def build(entry: Any) -> Verifier:
    """Build a verifier from a declarative entry:
    a callable passes through; a string names a no-arg builtin;
    {"name": [args]} or {"name": {kwargs}} parameterizes one."""
    if callable(entry):
        return entry
    if isinstance(entry, str):
        if entry not in BUILTINS:
            raise VerifierError(f"unknown verifier {entry!r}")
        return BUILTINS[entry]()
    if isinstance(entry, dict) and len(entry) == 1:
        name, args = next(iter(entry.items()))
        if name not in BUILTINS:
            raise VerifierError(f"unknown verifier {name!r}")
        if isinstance(args, dict):
            return BUILTINS[name](**args)
        if isinstance(args, list):
            return BUILTINS[name](*args)
        return BUILTINS[name](args)
    raise VerifierError(f"cannot build a verifier from {entry!r}")


def run_verifiers(
    entries: list[Any], ctx: VerifierContext
) -> tuple[bool, list[Verdict]]:
    """A verifier that cannot run is a FAILED check, never an exception that
    kills the orchestrator mid-phase (which would leave a phase-spawn event
    with no verdict — a lying run log)."""
    verdicts = []
    for entry in entries:
        try:
            verdicts.append(build(entry)(ctx))
        except Exception as exc:  # noqa: BLE001 — containment is the contract
            verdicts.append(
                Verdict("verifier-error", False, f"{entry!r} could not run: {exc}")
            )
    return all(v.ok for v in verdicts), verdicts
