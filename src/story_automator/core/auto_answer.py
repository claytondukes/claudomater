"""Pane-watcher: detect and auto-answer known interactive y/n prompts.

Closes the failure mode where bmad-advanced-elicitation renders an
interactive `Apply all N edits? (y/n)` prompt inside a spawned tmux
session and the orchestrator's polling loop sits idle until manually
nudged. See Story 24-10 and memory entry
feedback_yolo_elicitation_breaks_automation.md.

The whitelist below is conservative on purpose: every entry is a
literal prompt phrasing observed in the BMAD elicitation flow. A bare
`(y/n)?` catch-all would false-positive on diff hunks, story content,
or rendered documentation that happens to contain the three
characters. Adding new patterns is a code change reviewed against
Story 24-10 AC2.
"""

from __future__ import annotations

import re


# AC2 — conservative whitelist of literal prompt phrasings that are
# safe to auto-answer with `y` when overrides.autoAnswerElicitation is
# set. Tail-anchored against the last 10 visible pane lines to avoid
# matching the same literal that appears in scrollback (e.g. a
# code-review diff hunk).
KNOWN_PROMPTS: tuple[re.Pattern[str], ...] = (
    # `Apply all 3 edits? (y/n)` / `Apply all 17 edits (y/n)?` — the
    # bmad-advanced-elicitation confirmation we are specifically
    # closing.
    re.compile(r"Apply all \d+ edits?\??\s*\(y/n\)\??", re.IGNORECASE),
    # `Continue with these changes? (y/n)` — anticipated sibling
    # phrasing from elicitation variants.
    re.compile(r"Continue with these changes\?\s*\(y/n\)\??", re.IGNORECASE),
)


def match_known_prompt(pane_text: str) -> str | None:
    """Return the matched prompt text, or None if no whitelist hit.

    Args:
        pane_text: Raw output of `tmux capture-pane -p` for the
            session. MUST NOT be passed through `filter_input_box` —
            the prompt we are watching for may live inside Claude's
            input-box region, which the filter strips.

    Returns:
        The matched substring (for logging), or None.
    """
    if not pane_text:
        return None
    # AC2 — anchor to the last 10 lines of non-blank content. Strip
    # trailing blank lines first so a TUI that leaves whitespace
    # padding below the prompt (e.g. `tmux capture-pane` over a pane
    # where only one row has rendered content) still surfaces the
    # prompt for matching. Tail-anchoring keeps historical scrollback
    # (story files, diff hunks containing the literal phrase) from
    # false-positiving.
    lines = pane_text.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return None
    tail = "\n".join(lines[-10:])
    for pattern in KNOWN_PROMPTS:
        match = pattern.search(tail)
        if match:
            return match.group(0).strip()
    return None
