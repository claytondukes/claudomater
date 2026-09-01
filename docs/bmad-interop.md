# claudomater and BMAD

claudomater's story pipeline grew up inside a BMAD project, so its artifact
conventions are BMAD-shaped - but the v2 `omater` tool has **no dependency on
BMAD**: no BMAD install, no `_bmad/` directory, no BMAD skills. This page
answers the two setup questions directly.

## Fresh repo on a new system: do I need any BMAD files?

**No.** The core needs only:

```bash
pipx install .        # or pip install -e .
omater init           # writes .omater.yaml + gitignores the runs dir
omater start          # arms the fence + commit guard, opens the run log
```

- `~/.omater/config.yaml` (operator config: usage thresholds, degrade paths,
  Slack webhook, learning paths) is optional - a missing file yields spec
  defaults.
- Runs, the write fence, the commit guard, usage guardrails, phase running,
  the learning store, `omater sweep`, and notifications are fully
  self-contained.

The **story-pipeline features** (`omater sprint`, `omater gate`, the QA-board
finish flow, `omater report`) operate on artifact files that follow specific
conventions. Those conventions originated in BMAD but need no BMAD tooling -
you author the files yourself:

| Feature | What it needs from you |
|---|---|
| `omater sprint …` | A `sprint-status.yaml` with a `development_status:` block. **Seed the first epic block by hand** - the tool refuses to author the first block into a document it did not write (it has no basis to guess placement), and refuses to import a file with zero entries. From one hand-written epic block on, `add-epic` / `set` / `import` / `export` do the rest. |
| `omater sprint add-epic` | An epic markdown file carrying a `## Definition of Done` section (registration refuses without it). |
| `omater gate completion` | A story markdown with a `## Tasks / Subtasks` checkbox section and a `### File List` section (one bullet per repo-relative path, at most one flat parenthetical note - no nested parens). |
| Story-file status flips | One `Status:` line in the story file (`Status:`, `**Status:**`, or `**Status**:` - the colon is mandatory). |
| QA-board finish flow | Only if you wire `adapters.qa_board` in `.omater.yaml`; `null` means no board and the flow is skipped. |

Minimal seed for a fresh `sprint-status.yaml`:

```yaml
# Status definitions
# (write vocabulary: backlog ready-for-dev in-progress review done deferred scrapped superseded)

development_status:

  epic-1: backlog
  1-1-first-story: backlog
  epic-1-retrospective: fable-review-required
```

The **legacy v1 `story-automator`** (bundled until cutover) is the opposite:
it is a BMAD skill and hard-requires a BMAD project (`_bmad/` directory,
BMAD config). If you are starting fresh, use v2 and ignore v1. See
[legacy-story-automator.md](legacy-story-automator.md).

## Repo that previously used BMAD: migrate or run alongside?

**It runs alongside, in place. Nothing is migrated or converted.**

- `omater sprint import path/to/sprint-status.yaml` seeds claudomater's local
  SQLite index from your existing file. The file stays exactly where it is
  and stays the reviewable artifact; the DB is a private index
  (`learning.db_path`, never committed). Rows are keyed by your
  `.omater.yaml`'s `project` name (run from the project dir, or pass
  `--sprint-project` explicitly).
- Writes go DB-first, then **write through to the file byte-exactly**: every
  line keeps its raw bytes and only the flipped status token changes. Your
  preamble, per-line comments, and structural change log survive by
  construction - there is no reformatting code path.
- Reading never validates statuses: legacy values (for example the banned
  `optional` on old retrospective lines) are carried through as audit trail
  and reported, never "fixed". The status vocabulary gates writes only.
- Your existing story files, epic files, and `_bmad-output`-style artifact
  repos are read and written where they live. Declare the artifact repo in
  `.omater.yaml` (`artifact_roots`, `commit_scope`, `completion: exempt:`)
  and both toolchains keep operating on the same files.

**The one discipline for dual writers:** run `omater sprint import` before the
first `omater sprint set` of any session. The write-through export writes ALL
tracked statuses, so a stale DB can silently revert file-side edits made by
the other toolchain (or a human) since the last import. Import-first makes
the write-through carry the file's current truth.

Two mirror-image safety rails when the file and DB disagree:

- A key the DB tracks but the file dropped is **reported**, and removed from
  the DB only by an explicit `import --prune` - a truncated file must not
  delete real tracking as a side effect.
- A key the file carries but the DB lacks is imported normally.
