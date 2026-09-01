# Learning store

Cross-project lessons with two representations:

- a local SQLite index (`learning.db_path`, never committed) carrying FTS
  retrieval and the volatile use-counters, and
- a **deterministic JSONL export as the source of truth**
  (`learning.export_path`, git-carried): one file per scope, rows sorted,
  byte-reproducible, volatile counters excluded, written through on every
  DB write. The local index is fully reconstructible from the JSONL.

## Writes are classified

- `omater learn add` - refuses an existing live key.
- `omater learn refine` - merges better wording into an existing lesson.
- `omater learn supersede` - writes a new judgment; the old row stays as
  audit trail (superseded rows never surface in `list`/`search`).

Lesson content passes the same `secrets_deny` scrub as transcripts (pass
`--project` to name whose deny-list applies): the corpus outlives the run
that produced it.

## Sync

```bash
omater learn sync [--push]
```

Pull, import (latest `updated_at` wins, supersession chains relinked),
export, commit with an `omater-learn:` prefix. The commit subject reports
the STAGED export diff (`+A/-D lesson rows` - one JSONL line is one lesson
row), not the pull-side import counters: a sync that adds one lesson says
so.

## Injection and credit

`inject_lessons` closes the write-only-corpus loop: a phase receives its
scopes' promoted (always-loaded) lessons plus a domain-seeded FTS retrieval
(budgeted, refs-ranked), rendered as framed data with ids. The injected set
is logged before the agent exists; the result's `lessons_applied` is
validated against exactly that set, and only validated uses move the
`refs`/`sessions` counters.

## Promotion stays human-gated

`omater learn candidates` surfaces lessons used 3+ times across 2+ runs.
Only an operator's `omater learn promote` (scope-budgeted) makes a lesson
always-loaded. The tool never self-promotes - auto-promotion would be an
instruction-injection channel.
