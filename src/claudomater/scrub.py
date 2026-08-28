"""Transcript scrubbing (security model §12).

Phase transcripts are retained under `.omater/runs/` for post-mortems but
pass this scrub before write: values of secrets named in the project's
`secrets_deny` list, `NAME=value` assignments of those names, and common
token shapes are redacted.
"""

from __future__ import annotations

import os
import re

# Common credential shapes, redacted regardless of secrets_deny.
_TOKEN_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}"),
]


def scrub_text(
    text: str,
    secret_names: list[str] | tuple[str, ...] = (),
    env: dict[str, str] | None = None,
) -> str:
    env = env if env is not None else dict(os.environ)
    out = text

    for name in secret_names:
        value = env.get(name)
        # Redact the live value anywhere it appears (only if long enough to
        # not shred unrelated text).
        if value and len(value) >= 6:
            out = out.replace(value, f"[REDACTED:{name}]")
        # Redact NAME=value / NAME: value assignments, including quoted
        # multi-word values ('NAME="two words"' redacts the whole value).
        pattern = re.compile(
            rf"({re.escape(name)})(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s\"']+)"
        )
        out = pattern.sub(rf"\1\2[REDACTED:{name}]", out)

    for pattern in _TOKEN_PATTERNS:
        out = pattern.sub("[REDACTED:token]", out)
    return out
