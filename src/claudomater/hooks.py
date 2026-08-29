"""PreToolUse write fence + hook provisioning.

The fence denies Write/Edit outside the project root outright, and scans
Bash commands for write shapes it can positively recognize (redirects, tee,
copy targets, ...), pointing the agent at the declared scratch dir instead.
The fence is a REDIRECTOR for tool-shaped writes, not a jail. Measured in
the Phase 0 sandbox proof: writes constructed inside quoted interpreter code
(`python -c` + tempfile) pass the Bash scan with zero denies, while the
Write/Edit fence behaved perfectly. That porosity is by construction — shell
is expressive enough to hide a write from any static scan — so the success
measure is the run report's permission-stall count and the CLI's
`permission_denials` capture (per-phase in `run_event.detail`) plus verifier
discipline, not a claim of completeness. A false DENY is as costly as a miss
(it stalls legitimate work), so unrecognized input always passes.

`omater init` provisions the hook into the consumer repo's
`.claude/settings.json`; `omater init --verify` is the drift check run at
every run start.
"""

from __future__ import annotations

import json
import os
import posixpath
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
# A bash comment starts at `#` at the start of a WORD: after whitespace or a
# shell metacharacter (`echo ok;# ignored` is a comment too) or the string
# start — while `file#1` and `${#var}` are not comments.
_COMMENT = re.compile(r"(?:(?<=[\s;&|()<>`])|^)#[^\n]*")
_REDIRECT = re.compile(r"(?<![<>&\d])\d?>{1,2}\s*([^\s;|&<>()]+)")
_TEE = re.compile(r"\btee\s+(?:-[a-zA-Z]+\s+)*([^\s;|&<>()]+)")
_CREATE = re.compile(r"\b(?:mkdir|touch)\s+(?:-[a-zA-Z=]+\s+)*([^\s;|&<>()]+)")
_DD_OF = re.compile(r"\bdd\b[^;|&]*\bof=([^\s;|&<>()]+)")
_COPY = re.compile(r"\b(?:cp|mv|rsync|install)\s+(?:-[^\s]+\s+)*(?:[^\s;|&<>()]+\s+)+([^\s;|&<>()]+)")


# cd/pushd/popd at a command position, optionally preceded by assignment
# words (`MODE=x cd /path` legally prefixes a builtin). The verb must end at
# a shell token boundary — `cd/etc` is a command NAMED cd/etc, not a cd.
# group 1 = the separator BEFORE it (distinguishes `&&`/`;` from a single
# `|`: a cd inside a pipeline segment runs in a subshell and moves NOTHING),
# group 2 = verb, group 3 = option/flag prefix, group 4 = target token
# ('' when bare / immediately followed by && etc).
_CHDIR = re.compile(
    r"(^|\|\||&&|[\n;&|(])\s*"
    r"(?:[A-Za-z_][A-Za-z0-9_]*=[^\s;|&<>()]*\s+)*"
    r"(cd|pushd|popd)(?![^\s;&|)\n])\s*"
    r"((?:--\s+|-[A-Za-z]+\s+)*)([^\s;|&<>()]*)"
)
# Any cd-ish token the pattern above did NOT positively match (quoted
# assignment values, `command cd`, unmodeled prefixes, prose...) voids the
# tracked cwd instead of being silently ignored — an unrecognized cd left
# untracked would resolve later relative targets against a STALE cwd, which
# is exactly the false-deny shape this resolver exists to close.
_CD_WORD = re.compile(r"\b(?:cd|pushd|popd)\b")
def _segment_boundary(text: str, start: int) -> int:
    """Position of the control operator ending the command segment at
    `start` (or len(text)). An `&` inside redirection syntax — `>&`/`<&`
    fd-duplication (`2>&1`) or the `&>` shorthand — is data, not control;
    treating it as control let a redirect-carrying cd read as backgrounded."""
    i, n = start, len(text)
    while i < n:
        c = text[i]
        if c in ";\n)|":
            return i
        if c == "&":
            prev = text[i - 1] if i > 0 else ""
            nxt = text[i + 1] if i + 1 < n else ""
            if prev in "><":  # >& / <& — fd duplication
                i += 1
                continue
            if nxt == ">":  # &> redirect shorthand
                i += 2
                continue
            return i  # & or &&
        i += 1
    return n


# A standalone `&` backgrounds the ENTIRE list before it (`cd /x && true &`
# runs the whole list in a subshell), so any cd applied earlier in that list
# never reaches the foreground shell — after a bare `&` the tracked cwd is
# unknowable. Skips the same redirect forms as _segment_boundary and both
# halves of `&&`.
def _last_lp_flag(flags: str) -> str | None:
    """The effective -L/-P choice: bash honors the LAST one given
    (bundled letters included)."""
    last = None
    for token in re.findall(r"-([A-Za-z]+)", flags):
        for ch in token:
            if ch in "LP":
                last = ch
    return last


def _bare_ampersands(text: str) -> list[int]:
    out = []
    for m in re.finditer(r"&", text):
        i = m.start()
        prev = text[i - 1] if i > 0 else ""
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if prev in "><&" or nxt in "&>":
            continue
        out.append(i)
    return out
# Constructs that make the effective cwd untrackable from here on: subshells
# and command substitution (both paren forms) and backticks.
_CWD_OPAQUE = re.compile(r"[()`]")
# Compound control flow this scanner does not model: a cd inside
# `if false; then cd /etc; fi`, a function body, or a zero-iteration loop
# may never execute, so applying it would guess-deny. `eval`, `source`, and
# `.` run current-shell code the scanner cannot see (a quoted `eval 'cd x'`
# is placeholdered as data), so the cwd after them is unknowable too. Any of
# these at a command position (or a brace group) makes the cwd untrackable
# from that point on — same monotone fail-open rule as subshells; an
# absolute cd afterwards recovers tracking.
_COMPOUND = re.compile(
    r"(?:^|[\n;&|(])\s*"
    r"(?:(?:if|then|elif|else|fi|for|while|until|do|done|case|esac|function"
    r"|eval|source)\b"
    r"|\.(?![^\s;&|)\n]))"
    r"|[{}]"
)


def _scannable(command: str) -> str:
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
    # Unquoted comments are data too: `cd x  # note; cd /etc` must not have
    # its comment text scanned as commands. Same-length spaces keep every
    # event offset in this string consistent.
    return _COMMENT.sub(lambda m: " " * len(m.group(0)), scannable)


def _positioned_write_targets(scannable: str) -> list[tuple[int, str]]:
    targets: list[tuple[int, str]] = []
    for pattern in (_REDIRECT, _TEE, _CREATE, _DD_OF, _COPY):
        for m in pattern.finditer(scannable):
            target = m.group(1).strip("'\"")
            if target and not target.startswith("&"):
                targets.append((m.start(1), target))
    return targets


def bash_write_targets(command: str) -> list[str]:
    """The raw write-shaped targets the scan recognizes (shape only; see
    resolved_bash_targets for where each one actually lands)."""
    return [raw for _, raw in _positioned_write_targets(_scannable(command))]


def resolved_bash_targets(
    command: str, cwd: Path, env: dict[str, str] | None = None
) -> list[tuple[str, Path | None]]:
    """Each recognized write target paired with the path it resolves to,
    honoring in-command `cd`/`pushd` when resolving RELATIVE targets.

    Measured false deny this exists to close (Phase 0.5, bugtool):
    `cd <root>/server && cat > ../.omater/scratch/probe.py` resolved the
    redirect against the SESSION cwd (the repo root), landing one level
    above the repo — denied, though the real target was in-root scratch.

    A None path means the target is relative but the effective cwd is no
    longer knowable, so the caller must fail OPEN (deny-on-recognized: an
    unresolvable relative target is not a *recognized* out-of-tree write,
    and a guess would falsely deny). Unknowable covers: cd to a variable /
    quoted value / `-` / bare, popd, any subshell or command substitution
    seen earlier, a cd inside a pipeline segment or backgrounded with `&`
    (both run in subshells and move nothing), and a `||`-guarded cd (runs
    only on the failure path). One assumption is made on purpose: a tracked
    cd is assumed to SUCCEED — `cd x && write` even guarantees it, and the
    happy path is the universal agent idiom; a cd that failed mid-`;`-chain
    can still mis-resolve a later relative target, which deny-on-recognized
    accepts. Absolute targets never depend on the cwd and always resolve."""
    scannable = _scannable(command)
    # CDPATH rewires where a bare relative cd target lands (`CDPATH=/x cd t`
    # goes to /x/t; an inherited CDPATH does the same to every relative cd).
    # When one is plausibly in effect, CDPATH-eligible targets (relative,
    # not ./ or ../ anchored) become untrackable. Absolute and dot-anchored
    # targets bypass CDPATH per bash and keep tracking.
    env = env if env is not None else dict(os.environ)
    cdpath_active = bool(env.get("CDPATH")) or "CDPATH" in scannable
    events: list[tuple[int, str, Any]] = [
        (pos, "target", raw) for pos, raw in _positioned_write_targets(scannable)
    ]
    for m in _CWD_OPAQUE.finditer(scannable):
        events.append((m.start(), "opaque", None))
    for m in _COMPOUND.finditer(scannable):
        events.append((m.start(), "opaque", None))
    for pos in _bare_ampersands(scannable):
        events.append((pos, "opaque", None))
    chdir_matches = list(_CHDIR.finditer(scannable))
    matched_verb_spans = [(m.start(2), m.end(2)) for m in chdir_matches]
    for m in _CD_WORD.finditer(scannable):
        # a cd-ish token the parser did not positively match voids tracking
        # (see _CD_WORD) — never leave an unrecognized cd silently untracked
        if not any(start <= m.start() < end for start, end in matched_verb_spans):
            events.append((m.start(), "opaque", None))
    for m in chdir_matches:
        # The cd takes effect at its segment's END, not at the verb: a
        # redirect attached to the cd command itself (`cd /etc > cd.log`)
        # opens against the PRE-cd cwd. The segment boundary is ALSO where
        # the control operator lives — reading it right after the target
        # token instead let `cd /etc >/dev/null &` hide the `&` behind the
        # redirect.
        effect_pos = _segment_boundary(scannable, m.end())
        boundary = scannable[effect_pos : effect_pos + 2]
        # `cd /x &` (backgrounded) and `cd /x | ...` (pipeline segment) run
        # the cd in a subshell that moves nothing; `cd /x || ...` makes the
        # next branch the cd's FAILURE path (cwd unchanged there) and
        # everything after it ambiguous. All three: never apply.
        unusable = (
            (boundary.startswith("&") and not boundary.startswith("&&"))
            or (boundary.startswith("|") and not boundary.startswith("||"))
            or boundary.startswith("||")
        )
        events.append(
            (
                effect_pos,
                "chdir",
                (m.group(1), m.group(2), m.group(3), m.group(4), unusable),
            )
        )
        if m.group(1) == "&&" and not unusable:
            # A &&-guarded cd is CONDITIONAL: within its own && list every
            # later member runs only if the cd succeeded, so applying it is
            # sound — but past the list's end (`;`, newline, `)`) execution
            # resumes whether or not the guard passed, and the cwd there is
            # unknowable (`false && cd /etc; cat > out.txt` writes in the
            # ORIGINAL cwd). Void the tracked cwd at the list boundary.
            list_end = effect_pos
            while list_end < len(scannable) and scannable[list_end] not in ";\n)":
                list_end += 1
            if list_end < len(scannable):
                events.append((list_end, "opaque", None))
    # Same-position ties: resolve targets first (pre-cd), apply the cd next,
    # and let an opacity marker (e.g. the `)` closing a subshell that both
    # ends the cd's segment and discards its effect) win last.
    priority = {"target": 0, "chdir": 1, "opaque": 2}
    events.sort(key=lambda e: (e[0], priority[e[1]]))

    current: Path | None = cwd
    out: list[tuple[str, Path | None]] = []
    for _pos, kind, value in events:
        if kind == "target":
            raw = str(value)
            if Path(os.path.expanduser(raw)).is_absolute():
                out.append((raw, _norm(raw, cwd)))
            elif current is None:
                out.append((raw, None))
            else:
                out.append((raw, _norm(raw, current)))
            continue
        if kind == "opaque":
            current = None
            continue
        sep, verb, flags, target, unusable = value
        if sep == "|" or sep == "||" or unusable or verb == "popd":
            current = None
            continue
        flags = str(flags or "")
        if verb == "pushd" and "-n" in flags.split():
            # pushd -n updates the directory STACK without changing the
            # working directory — the cwd genuinely stays where it is
            continue
        target = str(target or "")
        if not target or target == "-" or "$" in target or "_quoted_data_" in target:
            current = None
            continue
        if "\\" in target or (verb == "pushd" and re.fullmatch(r"[+-]\d+", target)):
            # Not a literal path: a backslash means the token was truncated
            # at an escaped character (`cd a\ b/c` scans as `a\`), and
            # pushd +N/-N rotates the directory stack to an entry this scan
            # cannot know. Both -> unknown, fail open.
            current = None
            continue
        step = Path(os.path.expanduser(target))
        # Bash cds LOGICALLY by default (-L): `cd link && cd ..` returns to
        # the link's parent, not the symlink target's parent. Track the
        # logical cwd (lexical `..` handling, no realpath per hop) and
        # canonicalize only when a WRITE target is resolved (_norm) — or
        # when the hop's effective option ordering selects -P.
        physical = _last_lp_flag(flags) == "P"
        if step.is_absolute():
            new_cwd = str(step)
        elif cdpath_active and not target.startswith("."):
            # a bare relative target under an effective CDPATH may land
            # ANYWHERE on that search path — untrackable
            current = None
            continue
        elif current is not None:
            new_cwd = posixpath.join(str(current), str(step))
        else:
            continue  # relative cd from an unknown cwd stays unknown
        new_cwd = posixpath.normpath(new_cwd)
        current = Path(os.path.realpath(new_cwd)) if physical else Path(new_cwd)
    return out


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
    # A relative cwd would make relative tool paths resolve against the hook
    # PROCESS's working directory — environment-dependent decisions and an
    # avoidable bypass surface. Only an absolute cwd is trusted; anything
    # else falls back to the project root.
    raw_cwd = payload.get("cwd")
    cwd = root
    if isinstance(raw_cwd, str) and raw_cwd:
        candidate = Path(os.path.expanduser(raw_cwd))
        if candidate.is_absolute():
            cwd = candidate
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
        for raw, path in resolved_bash_targets(command, cwd, env=env):
            if path is None:
                # Relative target, untrackable effective cwd: not a
                # RECOGNIZED out-of-tree write — fail open, never guess-deny
                # (the false-deny cost is a stalled phase; the miss is
                # covered by verifiers + permission_denials accounting).
                continue
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
