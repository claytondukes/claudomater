"""PreToolUse write fence + hook provisioning.

The fence denies Write/Edit outside the project root outright, and scans
Bash commands for write shapes it can positively recognize (redirects, tee,
copy targets, ...), pointing the agent at the declared scratch dir instead.
It is best-effort by construction — shell is expressive enough to hide a
write from any static scan — so the success measure is the run report's
permission-stall count, not a claim of completeness. A false DENY is as
costly as a miss (it stalls legitimate work), so unrecognized input always
passes.

`omater init` provisions the hook into the consumer repo's
`.claude/settings.json`; `omater init --verify` is the drift check run at
every run start.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
HOOK_MATCHER = "|".join((*WRITE_TOOLS, "Bash"))
HOOK_COMMAND = 'omater hook pre-tool-use --root "$CLAUDE_PROJECT_DIR"'
HOOK_MARKER = "omater hook pre-tool-use"
SCRATCH_SUBDIR = ".omater/scratch"
SCRATCH_ENV = "OMATER_SCRATCH_DIR"

# Paths that are never a stall risk.
_ALWAYS_ALLOWED = ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty")
# Claude Code harness scratchpads live under these prefixes.
_SCRATCHPAD_PREFIX = re.compile(r"^(/private)?/tmp/claude-[^/]*/")


def _norm(path: str, cwd: Path) -> Path:
    p = Path(os.path.expanduser(path))
    if not p.is_absolute():
        p = cwd / p
    # realpath, not normpath: /tmp vs /private/tmp (macOS) and symlinked
    # checkouts must compare equal, or every in-tree write gets falsely denied
    return Path(os.path.realpath(p))


def _allowed(path: Path, root: Path, scratch_dirs: list[Path]) -> bool:
    s = str(path)
    if s in _ALWAYS_ALLOWED:
        return True
    if _SCRATCHPAD_PREFIX.match(s + "/"):
        return True
    for base in (root, *scratch_dirs):
        try:
            path.relative_to(base)
            return True
        except ValueError:
            continue
    return False


# Bash write patterns: redirections, tee, file-creating commands, copy/move
# targets. Only recognizable shapes — the deny is best-effort by design.
# group 1 = the heredoc intro line (kept scannable: `cat <<EOF > /etc/x`
# carries its redirect there); body + terminator are dropped as data.
_HEREDOC = re.compile(
    r"(<<-?\s*(['\"]?)(\w+)\2[^\n]*\n).*?^\s*\3\s*$", re.DOTALL | re.MULTILINE
)
_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
_REDIRECT = re.compile(r"(?<![<>&\d])\d?>{1,2}\s*([^\s;|&<>()]+)")
_TEE = re.compile(r"\btee\s+(?:-[a-zA-Z]+\s+)*([^\s;|&<>()]+)")
_CREATE = re.compile(r"\b(?:mkdir|touch)\s+(?:-[a-zA-Z=]+\s+)*([^\s;|&<>()]+)")
_DD_OF = re.compile(r"\bdd\b[^;|&]*\bof=([^\s;|&<>()]+)")
_COPY = re.compile(r"\b(?:cp|mv|rsync|install)\s+(?:-[^\s]+\s+)*(?:[^\s;|&<>()]+\s+)+([^\s;|&<>()]+)")


def bash_write_targets(command: str) -> list[str]:
    # Heredoc bodies and quoted strings are DATA (script contents, commit
    # messages, doc text) — scanning them produces false denies on
    # legitimate in-tree work. Quoted strings become a placeholder TOKEN,
    # not a bare space: erasing them entirely also erased argument
    # structure, so `cp "a b" /tmp/out` lost its source token and the
    # copy-target regex no longer saw the out-of-tree write. The remaining
    # cost is that a quoted redirect TARGET ('> "/x y"') reads as the
    # (relative, in-tree) placeholder, which the deny-on-recognized
    # contract accepts.
    scannable = _HEREDOC.sub(lambda m: m.group(1), command)
    scannable = _QUOTED.sub(" _quoted_data_ ", scannable)
    targets: list[str] = []
    for pattern in (_REDIRECT, _TEE, _CREATE, _DD_OF, _COPY):
        for m in pattern.finditer(scannable):
            target = m.group(1).strip("'\"")
            if target and not target.startswith("&"):
                targets.append(target)
    return targets


def scratch_dirs_for(root: Path, env: dict[str, str] | None = None) -> list[Path]:
    env = env if env is not None else dict(os.environ)
    dirs = [root / SCRATCH_SUBDIR]
    extra = env.get(SCRATCH_ENV)
    if extra:
        p = Path(os.path.expanduser(extra))
        if not p.is_absolute():
            # Anchor to the project root, not the hook process CWD — a
            # relative value resolved against CWD would deny legitimate
            # scratch writes and point the deny hint at the wrong place.
            p = root / p
        dirs.append(p)
    return dirs


def evaluate_pre_tool_use(
    payload: dict[str, Any],
    root: Path | str,
    env: dict[str, str] | None = None,
) -> tuple[bool, str | None]:
    """Returns (allow, deny_reason). Unrecognized input allows — the fence
    denies only what it can positively recognize as an out-of-tree write.
    That contract includes TYPES: a payload shape we don't understand must
    allow, never raise (an exception here stalls or silently disarms the
    fence, hook exit codes being what they are)."""
    if not isinstance(payload, dict):
        return True, None
    root = Path(os.path.realpath(Path(root).expanduser()))
    scratch = [
        Path(os.path.realpath(d)) for d in scratch_dirs_for(root, env)
    ]
    raw_cwd = payload.get("cwd")
    cwd = Path(raw_cwd) if isinstance(raw_cwd, str) and raw_cwd else root
    tool = payload.get("tool_name", "")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    # Name every allowed scratch location, including an operator-declared
    # OMATER_SCRATCH_DIR — a hint pointing only at the default sends the
    # agent to the wrong place when a declared dir exists.
    redirect_hint = (
        f"write inside the project ({root}) or a declared scratch dir "
        f"({', '.join(str(d) for d in scratch)})"
    )

    if tool in WRITE_TOOLS:
        raw = tool_input.get("file_path") or tool_input.get("notebook_path")
        if not raw or not isinstance(raw, str):
            return True, None
        path = _norm(str(raw), cwd)
        if _allowed(path, root, scratch):
            return True, None
        return False, (
            f"{tool} outside the project root denied: {path}. {redirect_hint}."
        )

    if tool == "Bash":
        command = tool_input.get("command")
        if not isinstance(command, str):
            return True, None
        for raw in bash_write_targets(command):
            path = _norm(raw, cwd)
            if not _allowed(path, root, scratch):
                return False, (
                    f"Bash out-of-tree write denied: {raw!r} resolves to {path}. "
                    f"{redirect_hint}."
                )
        return True, None

    return True, None


def hook_response(allow: bool, reason: str | None) -> dict[str, Any] | None:
    if allow:
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason or "denied by omater write fence",
        }
    }


# ---- provisioning ---------------------------------------------------------


def settings_path(project_root: Path | str) -> Path:
    return Path(project_root) / ".claude" / "settings.json"


def _load_settings(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError) as exc:
        raise HookProvisionError(f"cannot parse {path}: {exc}") from exc
    except OSError as exc:  # unreadable settings must report, not crash verify
        raise HookProvisionError(f"cannot read {path}: {exc}") from exc


class HookProvisionError(Exception):
    pass


def _our_entry() -> dict[str, Any]:
    return {
        "matcher": HOOK_MATCHER,
        "hooks": [{"type": "command", "command": HOOK_COMMAND}],
    }


def _find_entry(settings: dict[str, Any]) -> dict[str, Any] | None:
    hooks_cfg = settings.get("hooks")
    if not isinstance(hooks_cfg, dict):
        return None
    entries = hooks_cfg.get("PreToolUse")
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for hook in entry.get("hooks") or []:
            if isinstance(hook, dict) and HOOK_MARKER in (hook.get("command") or ""):
                return entry
    return None


def provision(project_root: Path | str) -> bool:
    """Merge the write-fence hook into `.claude/settings.json` (idempotent,
    preserves everything else). Returns True if the file changed."""
    path = settings_path(project_root)
    settings = _load_settings(path)
    existing = _find_entry(settings)
    if existing == _our_entry():
        return False
    if "hooks" in settings and not isinstance(settings["hooks"], dict):
        raise HookProvisionError(
            f"{path}: 'hooks' is not a mapping — fix it by hand before provisioning"
        )
    hooks = settings.setdefault("hooks", {})
    pre = hooks.setdefault("PreToolUse", [])
    if not isinstance(pre, list):
        raise HookProvisionError(
            f"{path}: hooks.PreToolUse is not a list — fix it by hand before provisioning"
        )
    if existing is not None:
        pre.remove(existing)
    pre.append(_our_entry())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return True


def verify(project_root: Path | str) -> list[str]:
    """Drift detection, run at every run start. Empty list = healthy."""
    problems: list[str] = []
    path = settings_path(project_root)
    if not path.exists():
        return [f"{path} does not exist — run `omater init`"]
    try:
        settings = _load_settings(path)
    except HookProvisionError as exc:
        return [str(exc)]
    entry = _find_entry(settings)
    if entry is None:
        problems.append("write-fence PreToolUse hook missing — run `omater init`")
    elif entry != _our_entry():
        problems.append(
            "write-fence PreToolUse hook drifted from the provisioned form — "
            "run `omater init` to restore it"
        )
    return problems
