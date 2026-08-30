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


def _escape_parity(text: str, idx: int) -> int:
    """1 when the char at idx is backslash-escaped (odd run before it)."""
    run = 0
    j = idx - 1
    while j >= 0 and text[j] == "\\":
        run += 1
        j -= 1
    return run % 2


def _lex_spans(text: str) -> list[tuple[str, int, int]]:
    """Data spans — ("quote", start, end) / ("comment", start, end) — from
    ONE scanner with shared lexical state. Both the masked-text renderer
    (_mask_data) and the heredoc pre-pass (_data_spans) draw from it: two
    divergent scanners cannot express bash here — a `#` inside quotes is
    data, a quote inside a comment is comment text, and the pre-pass
    disagreeing with the lexer on ANY rule (ANSI-C escaped quotes, the
    group-closing-`)` comment) let a fake heredoc introducer that bash
    treats as data eat real commands as its body.

    Quotes are escape-aware BY PARITY: a quote after an ODD backslash run
    is a literal char; after an EVEN run (`\\\\"` = escaped backslash +
    real quote) it delimits. Inside double quotes \\" does not end the
    span; a plain single-quoted span cannot contain escapes in bash;
    ANSI-C $'...' can. Unterminated quotes yield no span
    (deny-on-recognized).

    A comment starts at `#` at the start of a WORD: after whitespace, a
    shell metacharacter, the string start — or a GROUP-closing `)`
    (`(echo ok)# ignored`), but NOT a substitution-closing `)`
    (`$(printf x)#suffix` continues the word). Telling those apart needs
    pair matching: `(` openers are classified by their preceding char
    ($/</> = substitution, else grouping) and each `)` inherits its
    opener's kind. Comments INSIDE backticks are real comments, but the
    closing backtick terminates the substitution even mid-comment, so
    there a comment ends at newline-or-backtick. `file#1` and `${#var}`
    are not comments."""
    spans: list[tuple[str, int, int]] = []
    stack: list[str] = []
    in_backtick = False
    i, n = 0, len(text)

    def escaped(idx: int) -> bool:
        """An escaped space/metachar is part of the WORD — `echo foo\\ #bar`
        keeps #bar in the argument, not a comment."""
        return _escape_parity(text, idx) == 1

    def comment_end(idx: int) -> int:
        stops = "\n`" if in_backtick else "\n"
        while idx < n and text[idx] not in stops:
            idx += 1
        return idx

    while i < n:
        c = text[i]
        if c == "`" and not escaped(i):  # parity, like quotes: \\\\` opens
            in_backtick = not in_backtick
            i += 1
            continue
        if c == "#":
            # bash removes `\<newline>` continuations BEFORE recognizing
            # comments, so the boundary char is whatever precedes the
            # pair(s): `echo ok \<nl># x` joins to `echo ok # x` (comment —
            # leaving its text scannable falsely denied a redirect bash
            # never runs), while `echo x\<nl>#y` joins to the WORD `x#y`.
            j = i - 1
            while (
                j >= 1
                and text[j] == "\n"
                and text[j - 1] == "\\"
                and not escaped(j - 1)
            ):
                j -= 2
            prev = text[j] if j >= 0 else ""
            if j < 0 or (prev in " \t\n;&|(<>" and not escaped(j)):
                # BEFORE the quote branch: a quote in the comment is text
                end = comment_end(i)
                spans.append(("comment", i, end))
                i = end
                continue
        if c in "'\"" and not escaped(i):
            if c == "'":
                # ANSI-C quoting $'...' allows backslash-escaped quotes
                # inside; a plain single-quoted span cannot contain any.
                if i > 0 and text[i - 1] == "$" and not escaped(i - 1):
                    end = i + 1
                    while end < n and not (
                        text[end] == "'" and not escaped(end)
                    ):
                        end += 1
                    if end >= n:
                        end = -1
                else:
                    end = text.find("'", i + 1)
            else:
                end = i + 1
                while end < n and not (
                    text[end] == '"' and not escaped(end)
                ):
                    end += 1
                if end >= n:
                    end = -1
            if end != -1 and c == '"':
                # bash gives $(...) / ${...} / `...` their OWN quote
                # context inside double quotes, so a `"` in there does not
                # close the outer span — pairing with it left a QUOTED `>`
                # scannable and falsely denied the command. Recursive
                # parsing is frozen out (fence contract), so the misparse
                # SHAPE — an opener left unclosed in the scanned content,
                # or any backtick — classifies the remainder of the
                # command as unrecognized: one data span to the end, fail
                # open. Balanced substitutions with no inner quotes keep
                # exact pairing (and their real redirects keep denying).
                expect: list[str] = []
                hazard = False
                for j in range(i + 1, end):
                    ch = text[j]
                    if ch == "`" and not escaped(j):
                        hazard = True
                        break
                    if ch in "({" and not escaped(j):
                        if expect or (text[j - 1] == "$" and not escaped(j - 1)):
                            expect.append(")" if ch == "(" else "}")
                    elif expect and ch == expect[-1] and not escaped(j):
                        expect.pop()
                if hazard or expect:
                    spans.append(("quote", i, n))
                    break
            if end != -1:
                spans.append(("quote", i, end + 1))
                i = end + 1
                continue
        elif c == "(" and not in_backtick and not escaped(i):
            # \\( is word data, not a group open; parens inside backticks
            # belong to the substitution's own parser
            prev = text[i - 1] if i else ""
            stack.append("sub" if prev in ("$", "<", ">") else "group")
        elif c == ")" and not in_backtick and not escaped(i):
            # \\) is word data: `echo \\)#x > f` keeps its redirect
            kind = stack.pop() if stack else "group"
            if kind == "group" and i + 1 < n and text[i + 1] == "#":
                end = comment_end(i + 1)
                spans.append(("comment", i + 1, end))
                i = end
                continue
        i += 1
    return spans


def _data_spans(text: str) -> list[tuple[int, int]]:
    """Spans of RAW text that are data (quoted spans and comments) — keeps
    the heredoc pass from consuming a `<<` bash treats as data (`echo ok #
    <<EOF`, `echo '<<EOF'`). Same lexer as _mask_data by construction."""
    return [(start, end) for _, start, end in _lex_spans(text)]


def _arith_spans(
    text: str, data: list[tuple[int, int]] | None = None
) -> list[tuple[int, int]]:
    """Spans of arithmetic context — `$((...))` anywhere, `((...))` at a
    command position — where `<<` is a SHIFT operator, not a heredoc
    introducer: `$((1 << EOF))` must neither eat the following lines as a
    heredoc body nor raise the residual-<< wall. Detection is quote-,
    comment-, and escape-aware via _data_spans, so a `$((` that is data
    cannot open a span. Unbalanced arithmetic yields NO span: bash rejects
    the whole command before running anything, so nothing executes either
    way. A `<<` inside a command substitution NESTED in the arithmetic is
    also treated as a shift (its heredoc body stays scannable) — the
    parens have already voided the tracked cwd, and best-effort deny
    accepts that pathological shape.

    This runs synchronously in the PreToolUse hook on the RAW command,
    heredoc bodies included, so everything here is O(text): a per-index
    rescan of the data-span list made a generated heredoc full of quoted
    lines quadratic and stalled the phase."""
    if data is None:
        data = _data_spans(text)
    n = len(text)
    is_data = bytearray(n)
    for start, end in data:
        for k in range(start, min(end, n)):
            is_data[k] = 1
    # one stack pass pairs every live `(` with its `)` — the old
    # per-candidate depth walk was quadratic on runs of `((`
    match: dict[int, int] = {}
    stack: list[int] = []
    for k in range(n):
        if is_data[k]:
            continue
        if text[k] == "(" and not _escape_parity(text, k):
            stack.append(k)
        elif text[k] == ")" and not _escape_parity(text, k):
            if stack:
                match[stack.pop()] = k
    spans: list[tuple[int, int]] = []
    i = 0
    while i < n - 1:
        if is_data[i] or not text.startswith("((", i) or _escape_parity(text, i):
            i += 1
            continue
        dollar = i > 0 and text[i - 1] == "$" and not _escape_parity(text, i - 1)
        if not dollar:
            # bare (( is arithmetic only as a COMMAND, not mid-word
            # (`( (echo x) )` is a subshell, but its inner (( is adjacent
            # only when it, too, sits right at the command position)
            j = i - 1
            while j >= 0 and text[j] in " \t":
                j -= 1
            if not (j < 0 or (text[j] in ";&|\n(" and not _escape_parity(text, j))):
                i += 1
                continue
        # depth from i returns to zero exactly where the OUTER ( pairs
        close = match.get(i)
        if close is None:
            i += 1
            continue
        spans.append((i - 1 if dollar else i, close + 1))
        i = close + 1
    return spans


# Bash write patterns: redirections, tee, file-creating commands, copy/move
# targets. Only recognizable shapes — the deny is best-effort by design.
# group 1 = the heredoc intro line (kept scannable: `cat <<EOF > /etc/x`
# carries its redirect there); body + terminator are dropped as data.
# Terminators are EXACT lines: column zero for <<, leading TABS only for
# <<- (bash grammar). A lax `\s*` accepted an indented body line as the
# terminator and exposed the rest of the body to the scanner.
# The lookarounds keep NON-heredoc `<<` text out entirely: `<<<` is a
# here-string (matching from its second `<` invented a heredoc named by
# the here-string word), and arithmetic shifts are excluded by
# _arith_spans at the call site.
_HEREDOC = re.compile(
    r"((?<!<)<<(-)?(?!<)[ \t]*(?:(['\"])((?:(?!\3)[^\n])+)\3|([\w.+-]+))[^\n]*\n)"
    r".*?^(?(2)\t*)(?:\4|\5)$",
    re.DOTALL | re.MULTILINE,
)
def _mask_data(text: str) -> str:
    """Render the scan text from _lex_spans: quoted spans become the
    placeholder TOKEN (a bare space also erased argument structure —
    `cp "a b" /tmp/out` lost its source token), comments are blanked
    space-preservingly (offsets intact). Sharing the one lexer is the
    point — masking quotes in a pass BEFORE comment blanking let a quote
    that was comment text (`echo ok # "`) open a span that swallowed the
    executable lines after it, hiding a real out-of-tree write."""
    out: list[str] = []
    pos = 0
    for kind, start, end in _lex_spans(text):
        out.append(text[pos:start])
        out.append("_quoted_data_" if kind == "quote" else " " * (end - start))
        pos = end
    out.append(text[pos:])
    return "".join(out)
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
# Intra-command spacing is HORIZONTAL only ([ \t]) for the assignment and
# option prefixes: `\s` would swallow a newline and merge two separate
# commands into one chdir (`false && MODE=x\ncd /etc` guarded the wrong cd).
# The post-separator gap keeps `\s*` — a newline right after `&&`/`||`/`;`
# is a legal continuation of that operator.
_CHDIR = re.compile(
    r"(^|\|\||&&|[\n;&|(])\s*"
    r"(?:[A-Za-z_][A-Za-z0-9_]*=[^\s;|&<>()]*[ \t]+)*"
    r"(cd|pushd|popd)(?![^\s;&|)\n])[ \t]*"
    r"((?:--[ \t]+|-[A-Za-z]+[ \t]+)*)([^\s;|&<>()]*)"
)
# Redirection tokens legally follow a cd's target inside its own segment
# (`cd /etc > log 2>&1`); anything ELSE there is an extra argument bash
# rejects ("too many arguments") without moving, so the cd never happens.
_TRAILING_REDIRECT = re.compile(
    r"(?:\d*>{1,2}|&>{1,2}|\d*<{1,3})(?:[ \t]*&\d*-?)?[ \t]*[^\s;|&<>()]*"
)
# THE conservative write-target grammar (ratified fence contract: the
# fence is an accident seatbelt, not a security boundary against an
# evading agent). A target is RESOLVED only when it is a plain literal
# path. ANY construct not fully resolved — a quoted-span placeholder,
# parameter/command substitution ($ or backticks), an escape (backslash,
# remnant continuations included), glob/brace expansion chars, or a tilde
# surviving expanduser (~-, ~+, ~N, unknown users) — classifies the write
# as UNRECOGNIZED and the caller fails OPEN. Do not add per-construct
# resolution here: that path was five review rounds of bash-semantics
# whack-a-mole, each special case breeding the next.
_UNRESOLVED_CONSTRUCT = re.compile(r"[\\$*?\[{`]|_quoted_data_")


def _resolved_target(raw: str) -> str | None:
    """The expanded literal path for a fully resolved target token, else
    None (UNRECOGNIZED -> fail open). See _UNRESOLVED_CONSTRUCT."""
    if _UNRESOLVED_CONSTRUCT.search(raw):
        return None
    expanded = os.path.expanduser(raw)
    if expanded.startswith("~"):
        return None
    return expanded
# Any cd-ish token the pattern above did NOT positively match (quoted
# assignment values, `command cd`, unmodeled prefixes, prose...) voids the
# tracked cwd instead of being silently ignored — an unrecognized cd left
# untracked would resolve later relative targets against a STALE cwd, which
# is exactly the false-deny shape this resolver exists to close.
# A standalone shell WORD, not a regex word: `cd.log` contains \bcd\b but
# is a filename, and `./cd` runs a subprocess that cannot move the parent
# shell's cwd — neither may void tracking.
_CD_WORD = re.compile(r"(?<![^\s;&|(<>])(?:cd|pushd|popd)(?![^\s;&|)<>\n])")
# A command-position word containing the quote placeholder is a command we
# cannot name: bash concatenates quoted spans (`c""d server` runs cd, `"cd"
# server` runs cd), so the expanded command may move the cwd invisibly —
# void tracking (soft: later unconditional absolute cds recover). The
# lookahead keeps ASSIGNMENT-ONLY segments out: `HOME=$HOME` runs no
# command word at all (the cwd genuinely stays put — voiding it hid a
# recognized /etc write), while `HOME=x $CMD` still voids ($CMD can expand
# to `cd`, which then runs in the CURRENT shell).
_GLUED_COMMAND = re.compile(
    r"(?:^|[\n;&|(])\s*(?:[A-Za-z_][A-Za-z0-9_]*=[^\s;|&<>()]*\s+)*"
    r"(?![A-Za-z_][A-Za-z0-9_]*=)"
    r"[^\s;|&<>()]*(?:_quoted_data_|[\\$])[^\s;|&<>()]*"
)
# Words that can precede a cd token without changing WHICH shell executes
# it: assignments, redirections, wrapper options, and the current-shell
# prefixes themselves (`command cd` / `builtin cd` / `time cd` / `! cd`
# all move the cwd; `echo cd`, `env cd`, `xargs cd` cannot). `!` is the
# status-negation reserved word — rejecting it resolved a later relative
# write against the STALE pre-cd cwd (false deny).
# Words are pre-tokenized on UNESCAPED whitespace, so `.*` here: an
# escaped blank is part of the word (`MODE=a\ b` is ONE assignment).
_TRANSPARENT_PREFIX_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*|\d*[<>].*|&>.*|-.*")
_CD_WRAPPERS = frozenset({"command", "builtin", "time", "!"})
# A redirect operator with NO glued operand (`>`, `2>>`, `>&`, `&>`) takes
# the NEXT word as its file — `> /dev/null cd <root>` redirects and then
# RUNS cd. Checking `/dev/null` as an independent word kept a stale cwd
# and falsely denied the in-root write. Complete forms (`2>&1`, `2>&-`)
# do not consume an operand and stay ordinary transparent words.
_BARE_REDIRECT = re.compile(r"\d*[<>]{1,2}&?|&>{1,2}")
def _segment_boundary(text: str, start: int) -> int:
    """Position of the control operator ending the command segment at
    `start` (or len(text)). An `&` inside redirection syntax — `>&`/`<&`
    fd-duplication (`2>&1`) or the `&>` shorthand — is data, not control;
    treating it as control let a redirect-carrying cd read as backgrounded."""
    i, n = start, len(text)
    while i < n:
        c = text[i]
        if c in ";)|&" and _escape_parity(text, i) == 1:
            # \; \) \| \& are word data, same parity rule as the lexer —
            # `cd /etc \; cat > out.txt` is ONE command whose redirect
            # opens against the PRE-cd cwd; boundary at the escaped `;`
            # falsely denied ./out.txt as /etc/out.txt
            i += 1
            continue
        if c in ";\n)|":
            return i
        if c == "&":
            prev = text[i - 1] if i > 0 else ""
            nxt = text[i + 1] if i + 1 < n else ""
            if prev in (">", "<"):  # >& / <& — fd duplication
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


def _bare_ampersands(
    text: str, arith: list[tuple[int, int]] | None = None
) -> list[int]:
    out = []
    arith = arith or []
    ai = 0
    for m in re.finditer(r"&", text):
        i = m.start()
        prev = text[i - 1] if i > 0 else ""
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if prev in (">", "<", "&", "|") or nxt in ("&", ">"):
            continue
        if _escape_parity(text, i) == 1:  # \& is word data, not control
            continue
        # bitwise AND inside an arithmetic span (`$((x & 1))`) is data, not
        # a background op — voiding the cwd there hid a recognized write.
        # Cursor lookup: matches and spans are both ascending.
        while ai < len(arith) and arith[ai][1] <= i:
            ai += 1
        if ai < len(arith) and arith[ai][0] <= i:
            continue
        out.append(i)
    return out
# Constructs that make the effective cwd untrackable from here on: subshells
# and command substitution (both paren forms) and backticks.
_CWD_OPAQUE = re.compile(r"[()`]")
# `set -P` / `set -o physical` flips how every LATER cd resolves `..`
# through symlinks, and cds here read only their per-command -L/-P flags.
# Any command-position `set` that touches the physical option makes the
# DEFAULT mode unknowable from that point on (monotone — a later `set +P`
# could restore it, but option state is not modeled precisely): a flagless
# relative or dot-traversing cd afterwards fails open instead of tracking
# a logically-computed cwd the shell may not actually be in.
_SET_PHYSICAL = re.compile(
    r"(?:^|[\n;&|(])\s*set[ \t]+[^\n;&|()]*(?:[-+][A-Za-z]*P|[-+]o[ \t]+physical)"
)
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
    r"(?:if|then|elif|else|fi|for|while|until|do|done|case|esac|function)\b"
    # brace GROUPS only: `{ ...; }` at a command position (incl. after a
    # function's `()`). `${HOME}` and brace expansion `file{1,2}` are
    # expansions, not compound bodies — treating them as hard opacity let
    # a real /etc write pass unrecognized.
    r"|(?:^|[\n;&|()])\s*\{(?=[ \t\n])"
    r"|(?:^|[\n;&|])\s*\}"
)
# eval/source/. run current-shell code the scanner cannot see, but they
# EXECUTE AND RETURN: top-level flow demonstrably resumes after them, so
# their opacity is soft (an unconditional absolute cd afterwards recovers) —
# unlike a compound body, whose extent is unparseable (hard, above).
_SHELL_EXEC = re.compile(
    r"(?:^|[\n;&|(])\s*"
    r"(?:[A-Za-z_][A-Za-z0-9_]*=[^\s;|&<>()]*[ \t]+)*"  # MODE=x source ... is valid
    r"(?:(?:eval|source)\b|\.(?![^\s;&|)\n]))"
)


def _scannable(command: str) -> str:
    # Heredoc bodies and quoted strings are DATA (script contents, commit
    # messages, doc text) — scanning them produces false denies on
    # legitimate in-tree work. Quoted strings become a placeholder TOKEN,
    # not a bare space: erasing them entirely also erased argument
    # structure, so `cp "a b" /tmp/out` lost its source token and the
    # copy-target regex no longer saw the out-of-tree write. A quoted
    # redirect TARGET ('> "/x y"') reads as the placeholder, which the
    # resolver treats as non-literal and fails OPEN on (deny-on-recognized).
    def _heredoc_repl(m: re.Match) -> str:
        # keep the intro line scannable (its redirect is real) but blank the
        # `<<delim` marker: any << SURVIVING this pass is a heredoc shape the
        # pattern does not support, which the resolver treats as hard opacity
        intro = m.group(1)
        delim_end = m.end(4) if m.group(4) is not None else m.end(5)
        if m.group(3):
            delim_end += 1  # closing quote
        cut = delim_end - m.start(1)
        return " " * cut + intro[cut:]

    data = _data_spans(command)
    data = data + _arith_spans(command, data)

    def _outside_data(m: re.Match) -> str:
        # a << inside a comment, quoted span, or arithmetic expansion is
        # DATA (a comment/string starts no heredoc; in arithmetic << is a
        # SHIFT) — eating the following lines as a body hid real commands
        if any(start <= m.start(1) < end for start, end in data):
            return m.group(0)
        return _heredoc_repl(m)

    scannable = _HEREDOC.sub(_outside_data, command)
    # No padding around the placeholder: quotes glued to other characters
    # (`echo "x"#suffix`, `cp x"a b"y t`) must keep their word intact — a
    # space-padded placeholder invented a word boundary that turned #suffix
    # into a comment and erased the recognizable write after it.
    scannable = _mask_data(scannable)
    # Backslash-newline is a line CONTINUATION, not a boundary: `false && \
    # cd /etc` is one guarded list, and leaving the newline in made the cd
    # read as an unconditional new command. The pair is REMOVED (not spaced)
    # because bash joins the adjacent fragments into one token — `c\<nl>d`
    # is the word `cd`. Joined AFTER the data pass on purpose: a backslash
    # that was comment text (`# note \`) is blanked by then, so it cannot
    # join the next line into the comment — bash ends every comment at the
    # newline, continuation or not (removing the pair first fed `# note
    # cd /etc`-style joins to the comment blanker and hid the real cd).
    # Quoted spans are already placeholdered, so what remains is a real
    # continuation.
    return scannable.replace("\\\n", "")


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
    seen earlier, and a `||`-guarded cd (runs only on the failure path).
    A cd inside a pipeline segment or backgrounded with `&` runs in a
    subshell that moves NOTHING — the parent's tracked cwd is KEPT, not
    voided (voiding turned known cwds into unknown and hid recognized
    writes behind the fail-open path). One assumption is made on purpose: a tracked
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
    # An in-command HOME assignment changes what `~` means to bash, while
    # expanduser reads the HOOK's environment — resolving `~` through the
    # stale HOME falsely denied in-root writes (`HOME=<root>; echo > ~/f`
    # and `HOME=<root>; cd ~`). No HOME tracking (frozen): any `~`-leading
    # target in such a command is unresolved, fail open — position-
    # insensitive on purpose.
    home_touched = re.search(r"(?:^|[\s;&|({])HOME=", scannable) is not None
    arith = _arith_spans(scannable)
    events: list[tuple[int, str, Any]] = [
        (pos, "target", raw)
        for pos, raw in _positioned_write_targets(scannable)
        # `(( x > passwd ))` is an arithmetic COMPARISON — no write
        # happens, and the phantom target was falsely denied against the
        # (correctly) kept cwd. Redirects after the closing )) are real
        # and stay ((x)) > out carries its redirect outside the span).
        if not any(start <= pos < end for start, end in arith)
    ]
    # A paren inside PURE arithmetic — no nested command substitution
    # ($( or backtick) in the span — is the arithmetic's own delimiter
    # or grouping: evaluation cannot move the shell's cwd, and voiding
    # tracking over `((x++))` let `cd /etc; ((x++)); cat > passwd`
    # slip through against a "lost" cwd. Any nested substitution keeps
    # the span's full opacity (its parens included). Cursor lookup:
    # finditer and the spans are both ascending, and a per-paren rescan
    # of the span list would be the same quadratic _arith_spans shed.
    arith_pure = []
    for start, end in arith:
        body = scannable[start:end].lstrip("$")[2:-2]
        arith_pure.append("$(" not in body and "`" not in body)
    ai = 0
    for m in _CWD_OPAQUE.finditer(scannable):
        if _escape_parity(scannable, m.start()) == 1:
            continue  # \( \) \` are literal word chars, not delimiters
        while ai < len(arith) and arith[ai][1] <= m.start():
            ai += 1
        if ai < len(arith) and arith[ai][0] <= m.start() and arith_pure[ai]:
            continue
        events.append((m.start(), "opaque", None))
    def _real_anchor(m: re.Match) -> bool:
        # The leading command-position anchor must be REAL: a match whose
        # separator char is backslash-escaped anchors on word data —
        # `echo \; source x` runs source as an echo ARGUMENT, and the
        # spurious opacity cleared a known cwd (parity rule, same as
        # _real_separator / _segment_boundary; newlines stay unconditional
        # — \<newline> pairs were already removed by _scannable).
        first = scannable[m.start()]
        if first == "&" and m.start() > 0 and scannable[m.start() - 1] in "<>":
            return False  # the & of a >&/<& redirect, not a separator
        return first not in ";&|()" or _escape_parity(scannable, m.start()) == 0

    # Command-induced opacity (an unnameable/current-shell command) takes
    # effect at the SEGMENT boundary, not the command start: bash opens
    # redirects attached to the command BEFORE running it, so
    # `source env.sh > audit.log` resolves audit.log against the pre-source
    # cwd — clearing tracking first let a recognized out-of-tree write pass.
    for m in _GLUED_COMMAND.finditer(scannable):
        if _real_anchor(m):
            events.append((_segment_boundary(scannable, m.end()), "opaque", None))
    for m in _SHELL_EXEC.finditer(scannable):
        if _real_anchor(m):
            events.append((_segment_boundary(scannable, m.end()), "opaque", None))
    for m in _COMPOUND.finditer(scannable):
        # HARD opacity: a compound body's extent is unparseable here, so a
        # cd inside it must never recover tracking — `if false; then cd
        # /etc; cat > shadow; fi` runs neither the cd nor the write, and an
        # "absolute cd recovers" rule falsely denied the never-executed
        # redirect. Sticky for the rest of the command; soft opacity
        # (subshells, eval, bare &) stays recoverable because top-level
        # flow demonstrably resumes after those.
        if _real_anchor(m):
            events.append((m.start(), "hard", None))
    for pos in _bare_ampersands(scannable, arith):
        events.append((pos, "opaque", None))
    for m in _SET_PHYSICAL.finditer(scannable):
        if _real_anchor(m):
            events.append((m.end(), "setmode", None))
    # A `<<` surviving _scannable is a heredoc shape the parser does not
    # support (exotic delimiters: END@MARK, <<\EOF, ...) — its BODY stayed
    # scannable, and body text is DATA: neither cds nor redirects in it
    # execute, so from the end of the introducer line onward EVERYTHING
    # fails open, absolute targets included (a "wall", stronger than hard
    # opacity). Redirects on the introducer line itself are real and stay
    # enforceable. (`<<<` here-strings excluded; supported heredocs had
    # their marker blanked; a << in arithmetic is a SHIFT — `$((x<<2))`
    # walling off the rest of its own line hid a real redirect there.)
    for m in re.finditer(r"(?<!<)<<(?!<)", scannable):
        if any(start <= m.start() < end for start, end in arith):
            continue
        eol = scannable.find("\n", m.start())
        events.append((len(scannable) if eol == -1 else eol, "wall", None))
    # A cd is APPLIED on the assumption it succeeded; a later `||` in the
    # same list runs its RHS precisely when something failed — possibly that
    # cd — so the cwd there (and after) is unknowable. `;`/newline resets
    # the "a cd was applied in this list" state: a || in a LATER list says
    # nothing about an earlier list's cd.
    for m in re.finditer(r"\|\|", scannable):
        # `echo \|| true` is word data + a PIPE, not a || operator
        if _escape_parity(scannable, m.start()) == 0:
            events.append((m.start(), "orelse", None))
    for m in re.finditer(r"[;\n]", scannable):
        # an escaped ; is word data (newlines cannot be escaped here)
        if m.group() == "\n" or _escape_parity(scannable, m.start()) == 0:
            events.append((m.start(), "listsep", None))
    def _real_separator(m: re.Match) -> bool:
        # `echo \; cd /etc` never runs cd — an escaped metachar is word
        # data, not a command separator; the skipped cd then follows the
        # unmatched-token fail-open path below.
        sep = m.group(1)
        if len(sep) == 1 and sep in ";&|(":
            if (
                sep == "&"
                and m.start(1) > 0
                and scannable[m.start(1) - 1] in "<>"
            ):
                # `echo hi >& cd /etc` redirects to a FILE named cd — the
                # & belongs to the redirect, and anchoring a chdir on it
                # applied /etc and falsely denied the in-root write
                return False
            return _escape_parity(scannable, m.start(1)) == 0
        return True

    def _in_arith(pos: int) -> bool:
        # identifiers inside arithmetic are VARIABLES, not builtins:
        # `((cd /etc))` evaluates `cd / etc` and moves nothing — applying
        # it falsely denied an in-root write, and voiding on the token
        # would trade that for a miss. Neither: arithmetic cds are inert
        # (one in a NESTED substitution is subshell-scoped, and the impure
        # span's parens already void tracking).
        return any(start <= pos < end for start, end in arith)

    chdir_matches = [
        m
        for m in _CHDIR.finditer(scannable)
        if _real_separator(m) and not _in_arith(m.start(2))
    ]
    matched_verb_spans = [(m.start(2), m.end(2)) for m in chdir_matches]
    def _cd_word_can_execute(pos: int) -> bool:
        # An unmatched cd-ish token can move THIS shell's cwd only when
        # every word between its segment start and the token leaves it in
        # command position: assignments, redirects, options, and the
        # current-shell wrappers (`command cd`). Anything else (`echo cd`)
        # makes it an argument — voiding a correctly tracked cwd there hid
        # a recognized /etc write behind the fail-open path. A placeholder
        # prefix word (`"command" cd`) is caught by _GLUED_COMMAND instead.
        s = pos
        while s > 0:
            c = scannable[s - 1]
            if c in ";&|(\n" and _escape_parity(scannable, s - 1) == 0:
                if c == "&" and s >= 2 and scannable[s - 2] in "<>":
                    s -= 1  # the & of >&/<& — inside the segment, keep going
                    continue
                break
            s -= 1
        # split on UNESCAPED whitespace only: `MODE=a\ b` is ONE assignment
        # word — str.split() shattered it and the stray fragment read as a
        # command word, so the real cd neither tracked nor voided (stale
        # cwd, false deny)
        words: list[str] = []
        word_start = -1
        for j in range(s, pos):
            if scannable[j] in " \t\n" and _escape_parity(scannable, j) == 0:
                if word_start >= 0:
                    words.append(scannable[word_start:j])
                    word_start = -1
            elif word_start < 0:
                word_start = j
        if word_start >= 0:
            words.append(scannable[word_start:pos])
        k = 0
        while k < len(words):
            w = words[k]
            if _BARE_REDIRECT.fullmatch(w):
                k += 2  # the next word is this redirect's file operand
                continue
            if w in _CD_WRAPPERS or _TRANSPARENT_PREFIX_WORD.fullmatch(w):
                k += 1
                continue
            return False
        return True

    for m in _CD_WORD.finditer(scannable):
        # a cd-ish token the parser did not positively match voids tracking
        # (see _CD_WORD) — never leave an unrecognized cd silently untracked.
        # Effect at the segment boundary: a redirect attached to the same
        # command (`command cd /etc > ../escape.txt`) opens against the
        # PRE-command cwd and must still resolve (and deny).
        if (
            not any(start <= m.start() < end for start, end in matched_verb_spans)
            and not _in_arith(m.start())
            and _cd_word_can_execute(m.start())
        ):
            events.append((_segment_boundary(scannable, m.end()), "opaque", None))
    for m in chdir_matches:
        # The cd takes effect at its segment's END, not at the verb: a
        # redirect attached to the cd command itself (`cd /etc > cd.log`)
        # opens against the PRE-cd cwd. The segment boundary is ALSO where
        # the control operator lives — reading it right after the target
        # token instead let `cd /etc >/dev/null &` hide the `&` behind the
        # redirect.
        effect_pos = _segment_boundary(scannable, m.end())
        boundary = scannable[effect_pos : effect_pos + 2]
        # `cd /x &` (backgrounded) and `cd /x | ...` (pipeline segment,
        # either side — `|&` included) run the cd in a subshell that moves
        # NOTHING: the parent's tracked cwd stays valid, so these keep it
        # (voiding turned a known /etc into unknown and let `cd /etc;
        # true | cd /tmp; cat > passwd` write /etc/passwd unrecognized).
        in_pipeline = (
            (boundary.startswith("&") and not boundary.startswith("&&"))
            or (boundary.startswith("|") and not boundary.startswith("||"))
            or m.group(1) == "|"
            # `|&` pipes stderr: a cd on its right side runs in the pipeline
            # subshell, exactly like one after a plain `|`
            or (
                m.group(1) == "&"
                and m.start(1) > 0
                and scannable[m.start(1) - 1] == "|"
            )
        )
        # `cd /x || ...` makes the next branch the cd's FAILURE path (cwd
        # unchanged there) and everything after it ambiguous; a non-redirect
        # word after the target (`cd /etc \; cat`) is an extra argument bash
        # rejects without moving — applying the target would poison later
        # segments with a cwd the shell never entered (false deny). Both:
        # cwd unknowable, never apply.
        unusable = boundary.startswith("||") or bool(
            _TRAILING_REDIRECT.sub("", scannable[m.end() : effect_pos]).strip()
        )
        events.append(
            (
                effect_pos,
                "chdir",
                (m.group(1), m.group(2), m.group(3), m.group(4), unusable, in_pipeline),
            )
        )
        if m.group(1) == "&&" and not unusable and not in_pipeline:
            # A &&-guarded cd is CONDITIONAL: within its own && list every
            # later member runs only if the cd succeeded, so applying it is
            # sound — but past the list's end (`;`, newline, `)`) execution
            # resumes whether or not the guard passed, and the cwd there is
            # unknowable (`false && cd /etc; cat > out.txt` writes in the
            # ORIGINAL cwd). Void the tracked cwd at the list boundary.
            list_end = effect_pos
            while list_end < len(scannable) and (
                scannable[list_end] not in ";\n)"
                # an escaped ;/) is word data (`echo \;`), not the list's
                # terminator — voiding there cleared a still-guarded cwd
                or (
                    scannable[list_end] != "\n"
                    and _escape_parity(scannable, list_end) == 1
                )
            ):
                list_end += 1
            if list_end < len(scannable):
                events.append((list_end, "opaque", None))
    # Same-position ties: resolve targets first (pre-cd), apply the cd next,
    # then the ||/list bookkeeping, and let an opacity marker (e.g. the `)`
    # closing a subshell that both ends the cd's segment and discards its
    # effect) win last.
    priority = {
        "target": 0,
        "chdir": 1,
        "setmode": 1,
        "orelse": 2,
        "listsep": 3,
        "opaque": 4,
        "hard": 5,
        "wall": 6,
    }
    events.sort(key=lambda e: (e[0], priority[e[1]]))

    current: Path | None = cwd
    cd_applied_in_list = False
    dead = False  # hard opacity: no cd may recover tracking anymore
    walled = False  # unparseable remainder: even absolute targets fail open
    physical_mode: bool | None = False  # bash default -L; None = unknowable
    out: list[tuple[str, Path | None]] = []
    for _pos, kind, value in events:
        if kind == "target":
            raw = str(value)
            if walled:
                # inside/after an unsupported heredoc: this "target" is
                # almost certainly body DATA bash never executes
                out.append((raw, None))
                continue
            if _resolved_target(raw) is None or (
                home_touched and raw.startswith("~")
            ):
                out.append((raw, None))
            elif Path(os.path.expanduser(raw)).is_absolute():
                out.append((raw, _norm(raw, cwd)))
            elif current is None:
                out.append((raw, None))
            else:
                out.append((raw, _norm(raw, current)))
            continue
        if kind == "hard":
            current = None
            cd_applied_in_list = False
            dead = True
            continue
        if kind == "wall":
            current = None
            cd_applied_in_list = False
            dead = True
            walled = True
            continue
        if kind == "opaque":
            current = None
            cd_applied_in_list = False
            continue
        if kind == "orelse":
            # the RHS of || runs exactly when something failed — possibly a
            # cd applied earlier in this list on the success assumption
            if cd_applied_in_list:
                current = None
                cd_applied_in_list = False
            continue
        if kind == "listsep":
            cd_applied_in_list = False
            continue
        if kind == "setmode":
            physical_mode = None
            continue
        sep, verb, flags, target, unusable, in_pipeline = value
        if dead:
            # inside (or after) an unmodeled compound body: even an
            # absolute cd may never have executed — no recovery
            continue
        flags = str(flags or "")
        # -n manipulates the directory STACK without changing the working
        # directory — for BOTH stack builtins the cwd genuinely stays put
        # (checked before the generic popd-unknown). A trailing `-n` with no
        # operand lands in the TARGET slot (the flags group needs a trailing
        # space), so check both.
        if verb in ("pushd", "popd") and (
            "-n" in flags.split() or str(target or "") == "-n"
        ):
            continue
        letters = "".join(re.findall(r"-([A-Za-z]+)", flags))
        if any(ch not in ("LPe@" if verb == "cd" else "n") for ch in letters):
            # a flag the builtin rejects (`cd -Z /etc`, `pushd -P /etc`)
            # errors WITHOUT moving — applying the target tracked a cwd
            # the shell never entered (false deny). Version drift cuts the
            # other way too (a letter we call invalid may be accepted), so:
            # unknown, fail open — never apply.
            current = None
            continue
        if in_pipeline:
            # a pipeline/background subshell cd moves nothing in the parent:
            # the tracked cwd stays exactly as it was
            continue
        if sep == "||" or unusable or verb == "popd":
            current = None
            cd_applied_in_list = False
            continue
        target = str(target or "")
        # any dash-leading remnant is an option, `-` (OLDPWD), or a flag the
        # newline-bounded option group could not consume — never a literal
        # directory we can trust
        if (
            not target
            or target.startswith("-")
            or "$" in target
            or "_quoted_data_" in target
        ):
            current = None
            continue
        if (
            "\\" in target
            or any(ch in target for ch in "*?[{")
            or (verb == "pushd" and re.fullmatch(r"[+-]\d+", target))
        ):
            # Not a literal path: a backslash means the token was truncated
            # at an escaped character (`cd a\ b/c` scans as `a\`); glob and
            # brace chars expand at runtime (`cd ../proj*` can land right
            # back in-root); pushd +N/-N rotates the directory stack to an
            # entry this scan cannot know. All -> unknown, fail open.
            current = None
            continue
        if home_touched and target.startswith("~"):
            # same stale-HOME rule as write targets: `HOME=<root>; cd ~`
            # returns IN-root while expanduser tracked the old home
            current = None
            continue
        step = Path(os.path.expanduser(target))
        if str(step).startswith("~"):
            # a tilde expanduser could not resolve: directory-stack forms
            # (`~-` = $OLDPWD, `~+`, `~N`) or an unknown user — the real
            # destination is runtime state this scan cannot know
            current = None
            continue
        # Bash cds LOGICALLY by default (-L): `cd link && cd ..` returns to
        # the link's parent, not the symlink target's parent. Track the
        # logical cwd (lexical `..` handling, no realpath per hop) and
        # canonicalize only when a WRITE target is resolved (_norm) — or
        # when the hop's effective option ordering selects -P. A flagless
        # cd inherits the shell's option state (see _SET_PHYSICAL).
        lp = _last_lp_flag(flags)
        physical = physical_mode if lp is None else lp == "P"
        if physical is None and not (
            step.is_absolute() and ".." not in step.parts
        ):
            # default mode unknowable after set -P/-o physical: a relative
            # or dot-traversing hop lands somewhere mode-dependent — a
            # logically-computed cwd here allowed an out-of-tree write as
            # in-root through an in-root symlink. A plain absolute target
            # lands the same either way and keeps tracking.
            current = None
            continue
        if step.is_absolute():
            new_cwd = str(step)
        elif cdpath_active and not (
            target in (".", "..") or target.startswith(("./", "../"))
        ):
            # a relative target under an effective CDPATH may land ANYWHERE
            # on that search path — untrackable. Bash bypasses CDPATH only
            # for the exact ./.. components and ./ ../ anchored paths;
            # `.hidden` IS searched.
            current = None
            continue
        elif current is not None:
            new_cwd = posixpath.join(str(current), str(step))
        else:
            continue  # relative cd from an unknown cwd stays unknown
        if physical:
            # realpath the UNNORMALIZED join: lexically dropping `..` first
            # would erase the symlink hop -P exists to resolve
            # (`cd link && cd -P ..` lands in the link TARGET's parent)
            current = Path(os.path.realpath(new_cwd))
        else:
            current = Path(posixpath.normpath(new_cwd))
        cd_applied_in_list = True
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
