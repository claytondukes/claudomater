# The story pipeline

The artifact-facing half of claudomater: sprint tracking, the gates a story
must pass before and at its done-flip, the QA-board finish flow, and the
run-metrics store. All of it operates on convention-shaped markdown/yaml
files you author (no BMAD tooling required - see
[bmad-interop.md](bmad-interop.md) for the conventions and the fresh-repo
seed).

## Sprint tracking (`omater sprint`)

The DB is the writer; `sprint-status.yaml` is a **byte-exact write-through
export**. The file is a curated audit record (rules preamble, per-line
justifications, a structural change log) that happens to carry a status
map, so a regenerating exporter would destroy it. Instead the export is a
span model: every line keeps its raw bytes, an entry line also records the
character offsets of its status token, and a flip is
`raw[:start] + new + raw[end:]`. Indentation, separator spacing, and inline
comments survive by construction.

- Reading never validates a status: legacy values (for example the banned
  `optional` on old retrospective lines) are audit trail, reported but
  never "fixed". The vocabulary gates writes only.
- Epic membership is read positionally (a story key cannot be parsed:
  sub-epic `epic-4-5` makes `4-5-1-...` ambiguous).
- A key the DB tracks but the file lacks is a loud report, removed only by
  an explicit `import --prune`.
- `add-epic` requires `--epic-file` pointing at an epic markdown that
  carries a `## Definition of Done` section, checked BEFORE any write. The
  retro line is always appended as `epic-N-retrospective:
  fable-review-required` - the banned `optional` is unrepresentable.
- `check-retros` fails if any `*-retrospective:` line carries `optional`; a
  missing file fails loudly.
- **Discipline:** `omater sprint import` before the first `set` of any
  session - the write-through writes ALL tracked statuses, and a stale DB
  silently reverts file-side edits made since the last import.

Story FILE flips: `sprint.set_story_file_status(path, status)` rewrites the
single `Status:` line byte-exactly (`Status:`, `**Status:**`, or
`**Status**:` - the colon is mandatory; a prose line opening with "Status "
is not a marker). None or multiple markers refuse.

## Completion-integrity gate (`omater gate completion`)

A story cannot flip `done` while its own paperwork disagrees with reality.
Two fail-closed blades, run against the MERGE COMMIT:

1. Any unchecked `- [ ]` box inside `## Tasks / Subtasks` blocks.
2. `### File List` is compared as a SET against `git show --name-only` on
   the merge commit - a merged file the list omits and a listed file the
   merge lacks are both named. A missing File List section blocks by
   default (`require_file_list=False` is the explicit opt-out). Entries are
   `- \`path\`` with at most one flat parenthetical note (nested parens are
   a malformed entry and refuse - an unreadable entry must not silently
   thin the list).

Exemptions (File List prefixes that legitimately ride outside the merge -
driver-owned artifacts in a separate repo) come from `.omater.yaml`
`completion: exempt:` and NOWHERE else: the public API has no exempt
parameter, so no driver call site can quietly widen the gate. Every
invocation logs a `gate/completion-gate` run event with inputs, the exempt
list used, and the verdict.

```bash
omater gate completion --story-file PATH --merge-sha SHA [ROOT]   # requires a live run
```

## Surface classification (`claudomater.surface`)

The QA-board gate's first question - "did this story touch user-facing
surface?" - as an exclusions-FIRST pattern engine over the merged file set,
with the project's lists in committed config (`surface_rules:`). Exclusions
beat surface globs; `**` matches on path-segment boundaries; `neutral` is a
real third outcome; a non-repo-relative path and an empty changed-file set
are refused (a broken lookup must not read as "no surface").

## QA-board finish flow (`claudomater.qaboard.finish_story`)

After a story's PR merges and BEFORE its done-flip:

- **Surface story**: author the walkthrough step (insert-only append to the
  epic's authoring spec AND an idempotent POST to the live board, section
  resolved by epic), then regenerate the coverage matrix and run the epic
  gate in one shot - judged by EXIT CODE, never by parsing output.
- **No-surface story**: write the waiver EVALUATION (verdict buckets and
  all) to the run log - "no step needed" is a recorded decision, not a
  silence.

Board unreachable, malformed spec, missing step content for a surface
story, gate nonzero: each is a loud stop. Wired per project via
`adapters.qa_board` (`null` = no board).

`finish_story` also persists the run-metrics row when the driver passes
`metrics_facts` + `metrics_path` (both or neither - half-wired refuses).

## Epic-close gate (`omater gate close-epic`)

```bash
omater gate close-epic EPIC --sprint PATH [ROOT]
```

Ordered and counted:

1. Precheck: the artifact repo is clean AND pushed (`git rev-list
   @{u}..HEAD` empty) - story artifacts land before the close.
2. Write-ahead `close-gate` run event (no outcome claim), then the board
   gate runs and the matrix regenerates.
3. The regenerated matrix's `Story files audited: N` (plain or
   markdown-bold form) is validated against the sprint file's
   non-superseded story count for the epic - a mismatch or an unreadable
   count FAILS loudly, never warns. The happy path logs N/N.

## Conventions sweep (`omater sweep`)

```bash
omater sweep --range origin/main..HEAD [--repo R]
```

Checks a diff's ADDED lines for em-dashes outside backtick code spans
(quoting a historical em-dash heading verbatim stays legal) and
attribution/co-author footers. Exit 0 clean, 2 on findings or error. Runs
in every pre-push gate; the parser is hunk-aware (an added content line
starting with `++` is judged, not mistaken for a file header) and pins
deterministic diff flags (`--no-ext-diff --no-textconv --no-color`).

The companion config is `conventions:` in `.omater.yaml` - the standing
rules themselves, injected verbatim into every phase prompt
([phases.md](phases.md#prompt-injection-seams)).

## Run-metrics store (`omater report`)

One JSONL row per finished story, written by `finish_story` in the same
change set as the story's other artifacts: PR, merge sha, converge rounds,
findings fixed/dismissed (threads and suppressed separately), wall minutes,
cost, parks, gate-or-waiver outcome, merge-bypass flag.

- Appends are idempotent by `story_id` (an identical retry converges; a
  DIFFERENT row for a recorded story refuses loudly) and serialized under
  an inter-process lock.
- The full row schema - required fields, types, finite cost, per-kind
  outcome shape - is enforced at write AND load, so a damaged store fails
  as a typed error, not a renderer crash.

```bash
omater report --metrics run-metrics/stories.jsonl --epic 47   # per-epic table
omater report --metrics run-metrics/stories.jsonl             # cross-epic trends
```
