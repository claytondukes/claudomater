"""Pre-commit read-only guard: the run's declared COMMIT scope, enforced
at the git layer.

The write fence (hooks.py) redirects tool-shaped writes; this guard closes
a different door - a phase agent COMMITTING paths the run never declared
(process files, unrelated code, another repo's artifacts swept into a
commit). It composes with the fence at the git layer and does not extend
bash parsing: the fence parser is frozen by standing decision, and a git
hook sees the staged file list exactly as git will commit it, no parsing
required.

Same P1-1 discipline as the fence:
- RUN-SCOPED: `run.start_run` arms it, teardown removes it; between runs
  the hook file does not exist.
- AGENT-SCOPED: the installed script exits 0 unless OMATER_PHASE_AGENT is
  exactly "1" (the marker only ClaudeCliExecutor injects), so operator
  commits never even invoke omater.

Unlike the fence, the guard FAILS CLOSED while gated - deliberately. The
fence fails open because a false deny stalls legitimate work and its scan
is heuristic; the guard's input is exact (git's own staged list), a gated
commit is always an agent and never a human, and a blocked commit is a
loud, recoverable stall that surfaces in the run report. A guard that
silently passes on a missing scope file or a missing omater binary is not
a guard. The `exec omater` in the installed script carries this through
the last mile: command-not-found is exit 127, and git aborts the commit on
any nonzero hook exit.

Scope declaration: `.omater.yaml` `commit_scope` maps a repo - "." for the
project root, or an `artifact_roots` entry for a symlinked artifact repo -
to the repo-relative path prefixes a phase agent may commit there. A repo
armed with no declared scope blocks every gated commit (fail-closed
default) and the deny message says where to declare one. Drivers may
re-arm with per-run additions (arm() is an idempotent overwrite).

Bypass honesty (same contract as the fence): `git commit --no-verify`
skips pre-commit hooks entirely. The guard is a seatbelt against an agent
drifting out of scope, not a security boundary against one evading it -
the backstop is verifiers and the run report, as everywhere else.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from claudomater.hooks import AGENT_ENV, fence_active

GUARD_MARKER = "omater commit guard"
SCOPE_BASENAME = "omater-commit-scope.json"
# All paths a gated agent may commit - an explicit, declared "this repo is
# entirely the run's to write" (e.g. a dedicated artifacts repo).
SCOPE_ALL = "."

_SCRIPT = f"""\
#!/bin/sh
# {GUARD_MARKER} (run-scoped: armed by claudomater's start_run, removed by
# teardown; do not edit - verify() checks these exact bytes).
# Gated to omater phase agents only: a session without the marker is a
# human's, and guarding it would invert the sandbox contract (P1-1).
[ "${AGENT_ENV}" = "1" ] || exit 0
# Fail-closed from here: if omater is missing, exec fails nonzero (127)
# and git aborts the commit - a gated commit must never pass unchecked.
exec omater hook pre-commit
"""


class GuardError(Exception):
    """Arming, disarming, or verifying the guard cannot proceed honestly."""


def _git(root: Path, *args: str) -> str:
    """Run git in `root` with the INHERITED environment. cwd - not `-C` -
    on purpose: inside a pre-commit hook git exports GIT_INDEX_FILE (the
    temp index of a `git commit -- pathspec`, measured relative for plain
    commits), and both resolve correctly only from the repo toplevel the
    hook runs in."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
        )
    except OSError as exc:  # git missing / root gone: fail loudly, typed
        raise GuardError(f"cannot run git in {root}: {exc}") from exc
    if proc.returncode != 0:
        raise GuardError(
            f"git {args[0]} failed in {root}: {proc.stderr.strip() or proc.returncode}"
        )
    return proc.stdout


def git_dir(repo_root: Path | str) -> Path:
    """The repo's absolute git dir; GuardError when `repo_root` is not a
    git repository (a symlinked artifact root resolves through its link,
    same as every git command would)."""
    return Path(_git(Path(repo_root), "rev-parse", "--absolute-git-dir").strip())


def is_git_repo(path: Path | str) -> bool:
    """True when `path` is inside a git repository git can actually read.
    False also covers a missing git binary: with no git there are no
    commits to guard, so 'nothing to arm/disarm here' is vacuously true,
    not fail-open."""
    try:
        git_dir(path)
        return True
    except GuardError:
        return False


def _hook_path(gd: Path) -> Path:
    return gd / "hooks" / "pre-commit"


def _scope_path(gd: Path) -> Path:
    return gd / SCOPE_BASENAME


def _effective_hooks_dir(root: Path) -> Path:
    """Where git will actually LOOK for hooks (honors core.hooksPath)."""
    raw = _git(root, "rev-parse", "--git-path", "hooks").strip()
    p = Path(raw)
    if not p.is_absolute():
        p = root / p
    return Path(os.path.realpath(p))


def _require_own_hooks_dir(root: Path, gd: Path) -> None:
    """The guard installs into the repo's OWN `<git-dir>/hooks`, so git
    must actually read hooks from there. A core.hooksPath pointing AT that
    directory is fine (ui3 sets exactly this); one redirecting to a shared
    hooks dir is refused - installing a run-scoped guard into a directory
    other repos read is the P1-1 shape, and silently arming a hook git
    would never run is the silent-disarm shape. Both exits are loud."""
    effective = _effective_hooks_dir(root)
    own = Path(os.path.realpath(_hook_path(gd).parent))
    if effective != own:
        raise GuardError(
            f"git reads hooks for {root} from {effective}, not the repo's "
            f"own {own} (core.hooksPath) - refusing to arm the commit "
            "guard there; unset core.hooksPath for this repo or point it "
            "back at the default"
        )


def normalize_scope(scope: list[str]) -> list[str]:
    """Validated, normalized scope entries. Entries are repo-relative path
    prefixes matched on segment boundaries; "." declares the whole repo.
    Absolute paths and `..` fail loudly - a scope entry that silently
    matched nothing (or everything) would read exactly like a declared one.
    """
    out: list[str] = []
    for raw in scope:
        if not isinstance(raw, str) or not raw.strip():
            raise GuardError(f"commit scope entries must be path strings, got {raw!r}")
        entry = raw.strip().replace("\\", "/")
        if entry.startswith("./"):
            entry = entry[2:]
        entry = entry.rstrip("/")
        if entry == "" or entry == SCOPE_ALL:
            out.append(SCOPE_ALL)
            continue
        if entry.startswith("/") or entry.startswith("~"):
            raise GuardError(
                f"commit scope entries are repo-relative, got absolute {raw!r}"
            )
        if ".." in entry.split("/"):
            raise GuardError(f"commit scope entries must not traverse with '..': {raw!r}")
        out.append(entry)
    return out


def in_scope(path: str, scope: list[str]) -> bool:
    """Segment-boundary prefix match: entry `ui` covers `ui` and `ui/x`,
    never `ui2/x` (a bare startswith let a sibling directory ride a
    declared prefix)."""
    for entry in scope:
        if entry == SCOPE_ALL:
            return True
        if path == entry or path.startswith(entry + "/"):
            return True
    return False


def staged_paths(repo_root: Path | str) -> list[str]:
    """Paths of every staged change, exactly as git will commit them.
    --no-renames on purpose: a rename out of scope must show BOTH sides
    (the deletion of the old path and the creation of the new), or moving
    a guarded file out of scope would read as a single in-scope add.
    NUL-separated so filenames carrying newlines cannot smuggle a path
    past the split."""
    raw = _git(
        Path(repo_root),
        "diff",
        "--cached",
        "--name-only",
        "--no-renames",
        "-z",
    )
    return [p for p in raw.split("\0") if p]


def arm(repo_root: Path | str, scope: list[str]) -> bool:
    """Install the guard: scope file + hook script, canonical bytes.
    Idempotent overwrite of our OWN hook (re-arming with per-run scope
    additions is the supported driver seam). Returns True if anything
    changed.

    Refuses, loudly, anything it cannot own honestly:
    - a foreign pre-commit hook (no marker): clobbering someone's hook to
      install a seatbelt is worse than not arming;
    - a core.hooksPath redirecting hooks away from the repo's own
      `<git-dir>/hooks` (see _require_own_hooks_dir)."""
    root = Path(repo_root)
    normalized = normalize_scope(scope)
    gd = git_dir(root)
    _require_own_hooks_dir(root, gd)
    hook = _hook_path(gd)
    if hook.exists():
        try:
            existing = hook.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise GuardError(f"cannot read existing hook {hook}: {exc}") from exc
        if GUARD_MARKER not in existing:
            raise GuardError(
                f"cannot arm the commit guard in {root}: a foreign pre-commit "
                f"hook already exists at {hook} - refusing to replace it"
            )
    scope_file = _scope_path(gd)
    payload = json.dumps({"scope": normalized}, indent=2) + "\n"
    changed = False
    # The idempotence comparisons read with _read_or_none: these files are
    # OUR run state and arm() is authoritative, so one that cannot be read
    # (permissions, undecodable bytes) simply compares as "changed" and is
    # rewritten - the rewrite raises typed if the filesystem really is the
    # problem. Only the FOREIGN-hook check above must raise on an
    # unreadable file, because a hook we cannot read is one we must not
    # judge ours to clobber.
    if _read_or_none(scope_file) != payload:
        try:
            scope_file.write_text(payload, encoding="utf-8")
        except OSError as exc:
            raise GuardError(f"cannot write {scope_file}: {exc}") from exc
        changed = True
    if _read_or_none(hook) != _SCRIPT:
        try:
            hook.parent.mkdir(parents=True, exist_ok=True)
            hook.write_text(_SCRIPT, encoding="utf-8")
        except OSError as exc:
            raise GuardError(f"cannot write {hook}: {exc}") from exc
        changed = True
    # chmod unconditionally: the bytes may already match while the mode
    # does not, and a non-executable hook is silently skipped by git -
    # the exact silent-disarm shape this module refuses everywhere else.
    try:
        os.chmod(hook, 0o755)
    except OSError as exc:
        raise GuardError(f"cannot make {hook} executable: {exc}") from exc
    return changed


def _read_or_none(path: Path) -> str | None:
    """The file's text, or None when it does not exist or cannot be read
    (missing, permissions, undecodable bytes). For idempotence comparisons
    only - None never equals the canonical content, so the caller rewrites."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def disarm(repo_root: Path | str) -> bool:
    """Remove OUR hook + scope file; never a foreign hook. Returns True if
    anything was removed.

    A foreign hook found WITH our scope file still present means someone
    replaced the guard while it was armed - that is drift to report, not
    a state to tidy over: raising keeps teardown's contract ('our guard is
    GONE, and we know what we left behind') honest."""
    root = Path(repo_root)
    gd = git_dir(root)
    hook = _hook_path(gd)
    scope_file = _scope_path(gd)
    hook_exists = hook.exists()
    ours = False
    if hook_exists:
        try:
            ours = GUARD_MARKER in hook.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise GuardError(f"cannot read {hook}: {exc}") from exc
    if hook_exists and not ours and scope_file.exists():
        raise GuardError(
            f"commit-guard scope file {scope_file} exists but the pre-commit "
            f"hook at {hook} is not ours - the guard was replaced while "
            "armed; resolve by hand"
        )
    changed = False
    try:
        if hook_exists and ours:
            hook.unlink()
            changed = True
        if scope_file.exists():
            scope_file.unlink()
            changed = True
    except OSError as exc:
        # a readonly hooks dir must surface as teardown's typed error,
        # not a traceback (same contract as every write in this module)
        raise GuardError(f"cannot remove the commit guard from {root}: {exc}") from exc
    return changed


def verify(repo_root: Path | str, require: bool = True) -> list[str]:
    """Drift detection, same contract as hooks.verify: empty list =
    healthy. require=True is the run path (armed guard must be present and
    canonical); require=False is the between-runs check, where absent is
    the healthy state and only leftovers report. A directory that is not a
    git repo is healthy between runs (nothing can be armed there) and a
    problem on the run path (arming cannot have succeeded)."""
    root = Path(repo_root)
    try:
        gd = git_dir(root)
    except GuardError as exc:
        return [str(exc)] if require else []
    hook = _hook_path(gd)
    scope_file = _scope_path(gd)
    problems: list[str] = []
    if require:
        # A core.hooksPath flipped mid-run redirects git away from our
        # hook - the guard file sits untouched while git stops reading
        # it. That is precisely the drift this check exists to catch.
        try:
            _require_own_hooks_dir(root, gd)
        except GuardError as exc:
            problems.append(str(exc))
    if not hook.exists():
        if require:
            problems.append(
                f"commit-guard pre-commit hook missing in {root} - guard not armed"
            )
        if scope_file.exists():
            problems.append(
                f"commit-guard scope file {scope_file} left behind without its "
                "hook - remove it with `omater teardown`"
            )
        return problems
    try:
        content = hook.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [f"cannot read {hook}: {exc}"]
    if GUARD_MARKER not in content:
        # A foreign hook is not ours to police between runs; on the run
        # path it means arming was subverted since start.
        if require:
            problems.append(
                f"pre-commit hook in {root} is not the omater commit guard - "
                "the guard was replaced while armed"
            )
        return problems
    if not require:
        problems.append(
            f"commit-guard hook left behind in {root} (run-scoped - remove "
            "it with `omater teardown`)"
        )
    if content != _SCRIPT:
        problems.append(
            f"commit-guard hook in {root} drifted from the provisioned form - "
            "remove it with `omater teardown` (the next run re-arms the "
            "canonical hook)"
        )
    if not os.access(hook, os.X_OK):
        problems.append(
            f"commit-guard hook in {root} is not executable - git would "
            "silently skip it"
        )
    if not scope_file.exists():
        problems.append(
            f"commit-guard scope file missing in {root} - gated commits "
            "would be blocked with no declared scope (fail-closed)"
        )
    else:
        try:
            data = json.loads(scope_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("scope"), list):
                raise ValueError("missing 'scope' list")
        except (OSError, ValueError) as exc:
            problems.append(f"commit-guard scope file {scope_file} unreadable: {exc}")
    return problems


def evaluate(repo_root: Path | str) -> tuple[bool, str | None]:
    """(allow, deny_reason) for the staged changeset in `repo_root`.
    FAIL-CLOSED: any trouble reading the scope or the staged list is a
    deny that says so - see the module docstring for why this guard
    inverts the fence's fail-open rule."""
    root = Path(repo_root)
    try:
        gd = git_dir(root)
    except GuardError as exc:
        return False, f"commit guard cannot locate the git dir ({exc}); fail-closed"
    scope_file = _scope_path(gd)
    try:
        data = json.loads(scope_file.read_text(encoding="utf-8"))
        scope = data.get("scope") if isinstance(data, dict) else None
        if not isinstance(scope, list) or not all(isinstance(s, str) for s in scope):
            raise ValueError(f"'scope' must be a list of strings, got {scope!r}")
    except FileNotFoundError:
        return False, (
            f"commit guard is armed but its scope file is missing "
            f"({scope_file}); fail-closed. Re-arm via the run driver or "
            "remove the guard with `omater teardown`."
        )
    except (OSError, ValueError) as exc:  # JSONDecodeError is a ValueError
        return False, (
            f"commit guard cannot read its scope file {scope_file} ({exc}); "
            "fail-closed"
        )
    try:
        staged = staged_paths(root)
    except GuardError as exc:
        return False, f"commit guard cannot list staged paths ({exc}); fail-closed"
    violations = [p for p in staged if not in_scope(p, scope)]
    if not violations:
        return True, None
    listed = "\n".join(f"  - {p}" for p in violations)
    declared = (
        ", ".join(scope)
        if scope
        else "(empty - this run declared no committable paths for this repo)"
    )
    return False, (
        f"omater commit guard: BLOCKED - {len(violations)} staged path(s) "
        f"outside this run's declared write scope:\n{listed}\n"
        f"declared scope: {declared}\n"
        "Unstage the out-of-scope paths, or declare them in .omater.yaml "
        "commit_scope (the run re-arms the guard at start)."
    )


def repo_scopes(cfg: Any) -> dict[str, list[str]]:
    """The per-repo scope map a run arms: "." (the project root) plus every
    artifact_roots entry, each with its declared commit_scope or [] -
    an armed repo with an empty scope blocks every gated commit, which is
    the fail-closed default until the project declares one."""
    scopes: dict[str, list[str]] = {SCOPE_ALL: list(cfg.commit_scope.get(SCOPE_ALL, []))}
    for entry in cfg.artifact_roots:
        scopes[entry] = list(cfg.commit_scope.get(entry, []))
    return scopes


def _repo_path(root: Path, entry: str) -> Path:
    if entry == SCOPE_ALL:
        return root
    p = Path(os.path.expanduser(entry))
    return p if p.is_absolute() else root / entry


def _repo_targets(root: Path, cfg: Any) -> list[tuple[str, Path, list[str]]]:
    """(entry, repo path, declared scope) for every guarded repo that IS a
    git repository. A plain directory (the project root included) has no
    commits to guard - skipping it is vacuous truth, not fail-open."""
    root = Path(root)
    out: list[tuple[str, Path, list[str]]] = []
    for entry, scope in repo_scopes(cfg).items():
        repo = _repo_path(root, entry)
        if is_git_repo(repo):
            out.append((entry, repo, scope))
    return out


def arm_for_config(root: Path | str, cfg: Any) -> dict[str, list[str]]:
    """Arm the guard in the project root and in every artifact root that is
    a git repository. Returns {repo entry: normalized scope} for the run
    log. Any repo that cannot be armed honestly raises - a run must not
    start half-guarded and silent about it."""
    armed: dict[str, list[str]] = {}
    for entry, repo, scope in _repo_targets(Path(root), cfg):
        arm(repo, scope)
        armed[entry] = normalize_scope(scope)
    return armed


def disarm_for_config(root: Path | str, cfg: Any) -> list[str]:
    """Disarm everywhere arm_for_config arms. Returns the entries whose
    guard was actually removed."""
    removed: list[str] = []
    for entry, repo, _ in _repo_targets(Path(root), cfg):
        if disarm(repo):
            removed.append(entry)
    return removed


def verify_for_config(root: Path | str, cfg: Any, require: bool = True) -> list[str]:
    """verify() across the project root and every artifact-root git repo."""
    problems: list[str] = []
    for _, repo, _scope in _repo_targets(Path(root), cfg):
        problems.extend(verify(repo, require=require))
    return problems


def hook_main(cwd: Path | str | None = None) -> int:
    """`omater hook pre-commit` entrypoint. Gated like the fence hook: a
    session without the agent marker exits 0 before touching anything
    (the installed script already gates, but the command must be safe to
    invoke by hand too). While gated, EVERY failure path blocks."""
    if not fence_active():
        return 0
    root = Path(cwd) if cwd is not None else Path.cwd()
    try:
        allow, reason = evaluate(root)
    except Exception as exc:  # noqa: BLE001 - gated errors must block, not pass
        print(
            f"omater commit guard: internal error ({exc}); fail-closed",
            file=sys.stderr,
            flush=True,
        )
        return 1
    if allow:
        return 0
    print(reason, file=sys.stderr, flush=True)
    return 1
