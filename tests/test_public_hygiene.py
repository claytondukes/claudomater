"""Public-repo hygiene: the shipped tree must not name the private
consumer projects it was proven against.

This repo is public. Its parity suites replay real merges from a private
consumer checkout, and early drafts leaked that consumer's name into a
CLI default, docstrings, and test fixtures. The leak that motivated this
guard was functional: `omater sprint --sprint-project` defaulted to the
private project's name, so a fresh install silently keyed every sprint
row under someone else's project.

The banned tokens are assembled from fragments at runtime so this file
never matches itself, and the scan covers every git-tracked text file -
a hardcoded directory list would miss the next new top-level dir.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_TEXT_SUFFIXES = {".py", ".md", ".yaml", ".yml", ".toml", ".sh", ".json", ".txt"}

# Fragment pairs, joined at runtime: private project / infra names that
# must never appear in shipped text. Matching is case-insensitive.
_BANNED_FRAGMENTS: tuple[tuple[str, str], ...] = (
    ("ui", "3"),
    ("logzi", "lla"),
    ("mant", "is"),
    ("bugt", "ool"),
    ("source", "code/"),
)


def _banned_tokens() -> tuple[str, ...]:
    return tuple("".join(pair).lower() for pair in _BANNED_FRAGMENTS)


def _tracked_text_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    files = [
        REPO_ROOT / rel
        for rel in out.splitlines()
        if Path(rel).suffix in _TEXT_SUFFIXES
    ]
    assert files, "git ls-files returned no text files - the scan is broken"
    return files


def test_no_private_consumer_references_in_tracked_text() -> None:
    tokens = _banned_tokens()
    offenders: list[str] = []
    for path in _tracked_text_files():
        text = path.read_text(encoding="utf-8", errors="replace").lower()
        if not any(tok in text for tok in tokens):
            continue
        rel = path.relative_to(REPO_ROOT)
        for line_no, line in enumerate(text.splitlines(), start=1):
            hits = [tok for tok in tokens if tok in line]
            if hits:
                offenders.append(f"{rel}:{line_no}: {', '.join(hits)}")
    assert not offenders, (
        "private consumer references in a public repo "
        f"({len(offenders)} line(s)):\n" + "\n".join(offenders)
    )
