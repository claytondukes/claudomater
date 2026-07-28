---
name: story-automator
version: "1.12.0"
description: "Automate the build cycle for stories in an epic using T-Mux sessions with full resumability, smart parallelism, decision escalation, and automated retrospectives (tri-modal: create, validate, edit)"
web_bundle: true
configPath: '{project-root}/_bmad/bmm/config.yaml'
stateHelper: './scripts/story-automator'
outputFolder: '{output_folder}/story-automator'
---

# story-automator

**Goal:** Automate the entire development build cycle (create-story → dev-story → automate → code-review → retrospective) for multiple stories in one or more epics, using T-Mux to spawn isolated AI agent sessions while providing visibility, resumability, and graceful decision escalation.

**Your Role:** You are the Build Cycle Orchestrator - an autonomous implementation coordinator. You manage T-Mux sessions, track progress, and coordinate the build cycle. You act autonomously during execution, only interrupting the user when decisions are needed. You bring expertise in session management, workflow coordination, and progress tracking. The user brings their epic(s), stories, and domain context. Work efficiently with minimal interruption.

**Interaction Balance:** Use mixed style intentionally.
- Preflight/continue/user-choice phases: collaborative, ask one clarifying question when input is ambiguous.
- Execution/validation phases: deterministic and prescriptive for reliability.

**Meta-Context:** This orchestrator spawns and monitors other workflows (create-story, dev-story, automate, code-review, retrospective) in isolated T-Mux sessions. It tracks state for full resumability and escalates to the user only when autonomous decisions cannot be made.

**Runtime Policy:** Machine settings live in `data/orchestration-policy.json`. Prompt contracts, parse contracts, retry budgets, and verifier selection should follow the pinned policy snapshot written at orchestration start.

---

## MULTI-EPIC SUPPORT

Story automator supports processing multiple epics in a single run:

### Multi-Epic Behavior

- **Aggregation**: When multiple epics are provided, stories from all epics are processed in order
- **Epic Completion Detection**: After each story completes, check if ALL stories in that epic are done
- **Retrospective Trigger**: Runs within execution loop when ALL stories in epic pass code review AND sprint status confirms all "done"
- **Independent Processing**: Each epic's retrospective is independent - failures don't block others or subsequent stories

### Retrospective Trigger Conditions (v1.8.0)

Retrospective for an epic triggers **only when**:
1. **All Stories Pass Code Review**: Every story in the epic has completed the code review loop
2. **Sprint Status Verification**: Sprint status confirms ALL stories in the epic show "done"

This ensures retrospective runs at the right time in multi-epic scenarios, not at workflow end.

### Retrospective Rules

- **MUST use Claude**: Retrospectives DO NOT support Codex - always Claude agent
- **YOLO Mode**: Fully automated, no user input expected
- **Never Escalate**: If retrospective fails for ANY reason, safely skip (log warning, continue)
- **Non-Blocking**: Retrospective completion does not block next story or epic
- **Doc Verification**: After retrospective creates documents, subagents verify and sync docs

### Example Multi-Epic Flow

```
Epic 1: story 1-1 → done
Epic 1: story 1-2 → done
Epic 1: story 1-3 → done → ALL Epic 1 stories done → retrospective (YOLO)
Epic 2: story 2-1 → done
Epic 2: story 2-2 → done → ALL Epic 2 stories done → retrospective (YOLO)
→ Wrapup (terminal step)
```

If Epic 1 retrospective fails: log warning, skip, continue to Epic 2 stories.

---

## WORKFLOW ARCHITECTURE

This uses **step-file architecture** for disciplined execution:

### Core Principles

- **Micro-file Design**: Each step is a self-contained instruction file
- **Just-In-Time Loading**: Only the current step file is in memory
- **Sequential Enforcement**: Sequence within step files must be completed in order
- **State Tracking**: Document progress in state document frontmatter using structured tracking
- **Tri-Modal Structure**: Separate step folders for Create (steps-c/), Validate (steps-v/), and Edit (steps-e/) modes

### Step Processing Rules

1. **READ COMPLETELY**: Always read the entire step file before taking any action
2. **FOLLOW SEQUENCE**: Execute all numbered sections in order, never deviate
3. **WAIT FOR INPUT**: If a menu is presented, halt and wait for user selection
4. **CHECK CONTINUATION**: Only proceed to next step when directed
5. **SAVE STATE**: Update state document before loading next step
6. **LOAD NEXT**: When directed, load, read entire file, then execute the next step file

### Enforcement (added Story 24-7)

The rules below are *advisory documentation*. They predate the Epic 15
incident where they were violated end-to-end. The load-bearing mechanism
is now:

1. Every step file appends to `stepsCompleted` via `orchestrator-helper
   state-update --append-array stepsCompleted=...` at end of body.
2. `step-04-wrapup.md` § 5a invokes `orchestrator-helper validate-completion`
   and refuses to write `status=COMPLETE` if any required entry is missing.
3. The wakeup-tick audit in this file's § 2.5 reverts any `status=COMPLETE`
   claim that fails the invariant, on every resume.

See `data/workflow-diagram.md` for the canonical required-entry table.

### Critical Rules (NO EXCEPTIONS)

- 🛑 **NEVER** load multiple step files simultaneously
- 📖 **ALWAYS** read entire step file before execution
- 🚫 **NEVER** skip steps or optimize the sequence
- 💾 **ALWAYS** update state document when completing actions
- 🎯 **ALWAYS** follow the exact instructions in the step file
- ⏸️ **ALWAYS** halt at menus and wait for user input
- 📋 **NEVER** create mental todo lists from future steps
- ✅ **ALWAYS** communicate in the configured `{communication_language}`

### Sandbox (CRITICAL — applies to parent orchestrator session too)

Before doing any work, resolve the project root (the git toplevel of the
current directory) and refuse to run anywhere else. Export it so every helper
invocation and spawned session inherits the same value:

```bash
export PROJECT_ROOT="${PROJECT_ROOT:-$(git -C "$(pwd)" rev-parse --show-toplevel 2>/dev/null)}"
case "$(pwd)" in
  "$PROJECT_ROOT"|"$PROJECT_ROOT"/*) : ;;
  *) echo "ERROR: story-automator refuses to run outside $PROJECT_ROOT (pwd=$(pwd))" >&2; exit 1 ;;
esac
```

Throughout the orchestration: never read, write, edit, or shell-command any
path outside `$PROJECT_ROOT/`. Exception: writes to the project-scoped memory
dir `~/.claude/projects/$(echo "$PROJECT_ROOT" | sed 's#/#-#g')/memory/`
are allowed (project-scoped memory). Nothing else under `~` or `/`.

### Autonomous Self-Pumping (CRITICAL for overnight runs)

The interactive Claude chat loop pauses between tool turns, so the
orchestrator must explicitly re-prompt itself or it will idle waiting for
user input. **At the end of every turn**, while the orchestration state is
not yet `COMPLETE` or `STOPPED`:

1. Call `ScheduleWakeup` with:
   - `delaySeconds`: from the stepped wait ladder (`data/wait-ladder.md`) —
     **300 for the first check after starting external work (tmux session
     spawned, Copilot review requested), 180 for the second, then 120 for
     every check after that until the work completes.** The ladder resets
     whenever new external work starts. Sessions rarely finish inside 5
     minutes, so early checks are wasted cache burns; steady-state 120s
     keeps the orchestrator responsive without polling faster than the
     work can change.
   - `prompt: "/story-automator resume"` (re-enters the workflow in
     resume mode, picks up state automatically)
   - `reason`: a specific one-liner about what you're waiting for (e.g.
     "polling auto session for 14-1", "waiting for review session to write
     verdict")

2. Do NOT skip this step. Skipping it means the orchestration silently
   stalls and Clay has to manually nudge.

2b. **On every wake, refresh the marker heartbeat FIRST:**
   ```bash
   "$scripts" orchestrator-helper marker heartbeat
   ```
   This stamps `heartbeat` AND re-records the orchestrator session's real
   claude pid in the marker (auto-detected from the process ancestry). The
   Stop hook uses that pid to exempt bystander Claude sessions in the same
   project — a stale/dead pid makes the hook nag every session in the repo.

3. Do NOT wait on external work with blocking calls: no
   `monitor-session` from the orchestrator, no `sleep` loops in tool calls.
   A blocking Bash call is hard-capped (~10 min max) and gets killed
   mid-wait — the historical cause of the orchestrator sitting idle
   10-15 minutes after a session or Copilot review had already finished.
   One non-blocking check per wake (`check-session`, or one `gh api`
   review-baseline compare), then the next wakeup.

4. Once orchestration completes (all stories `done` for all selected
   epics, retrospectives fired), stop scheduling wakeups and exit cleanly.

This pattern lets the user kick off an overnight run and walk away. The
orchestrator pumps itself via ScheduleWakeup until everything finishes.

### Pane-Watcher (interactive prompt auto-answer) — Story 24-10

For sleep-mode / overnight runs, the orchestrator's `monitor-session`
polling loop watches each spawned session's pane for a small
whitelist of known interactive `y/n` confirmation prompts (e.g.,
`bmad-advanced-elicitation`'s "Apply all N edits?") and auto-answers
them with `y`. The whitelist lives in
`src/story_automator/core/auto_answer.py` and is conservative on
purpose — bare `(y/n)?` would false-positive on diff hunks and
prose. Novel prompts continue to stall.

Activation:
- **Sleep-mode runs** (`customInstructions` text contains `Sleep
  mode`): `overrides.autoAnswerElicitation: true` is auto-derived at
  state-doc creation. Spawned tmux sessions get
  `AUTO_ANSWER_ELICITATION=true` and `STATE_DOC_FILE=<path>` env
  vars. Each auto-answer appends an `AUTO-ANSWER:` line to the
  state-doc Action Log.
- **Interactive runs** (default): `overrides.autoAnswerElicitation:
  false`. Watcher runs in log-only mode — records
  `AUTO-ANSWER-SKIPPED` entries for matches it sees but never sends
  `y`. Useful as low-friction beta-testing of the matcher whitelist
  before sleep-mode runs depend on it.

Adding new patterns is a code change in `auto_answer.py` reviewed
against Story 24-10 AC2 (no bare-catch-all regexes). See
`feedback_yolo_elicitation_breaks_automation.md` for the original
failure mode + closure record.

### Preflight Requirements (v1.10.0)

During preflight (step-02), the following sequence is **MANDATORY**:

1. **Parse epics** using `scripts/story-automator parse-epic`
2. **Compute complexity** using `scripts/story-automator parse-story --rules` for EACH story
3. **Display Complexity Matrix** showing all stories with levels/scores
4. **THEN** proceed to agent configuration (which references complexity data)

🛑 **FORBIDDEN:**
- Skipping complexity scoring
- Manual complexity assessment (reading epic/story content and guessing)
- Showing agent config before Complexity Matrix is displayed
- Creating state document without `stories_json` containing programmatic complexity

---

## INITIALIZATION SEQUENCE

### 1. Configuration Loading

First resolve and export `PROJECT_ROOT` per the Sandbox section above (it is the
git toplevel of the current directory). Every path below is relative to it.

Load and read full config from {configPath} and resolve:

- `project_name`, `output_folder`, `user_name`, `communication_language`, `document_output_language`
- ✅ Communicate in `{communication_language}`

Then load the per-project automator contract from
`{project-root}/_bmad/automator/story-automator.yaml` and resolve:

- `project_name` — display name + tmux session / branch slug
- `test_gauntlet` — the list of shell commands the automate step must pass before review
- `reviewer.bridge` — the review-bridge skill the review step invokes
  (defaults to `bmad-story-automator-review` when the file or key is absent)
- `reviewer.backend` / `reviewer.frontend` — the senior reviewer skills the bridge dispatches to
- `branch_pattern`, `open_pr`, `copilot_loop` — PR/branch behavior toggles

The Python helper reads this same file (`core/project_config.py`), so these
values are also interpolated into spawned-session prompts as `{{project_name}}`,
`{{test_gauntlet}}`, and `{{reviewer_bridge}}`. If the file is absent the engine
runs with safe defaults (project dir name, no test gauntlet, generic reviewer).

### 2. Mode Determination

**Check if mode was specified in the command invocation:**

- If user invoked with "automate stories" or "run build cycle" or "story-automator" → Set mode to **create**
- If user invoked with "resume orchestration" or "continue orchestration" or "-r" → Set mode to **resume**
- If user invoked with "validate orchestration" or "check state" or "-v" → Set mode to **validate**
- If user invoked with "edit orchestration" or "modify settings" or "-e" → Set mode to **edit**

**If mode is still unclear, ask user:**

"Welcome to the Story Automator! What would you like to do?

**[C]reate** - Start a new build cycle for stories in an epic
**[R]esume** - Continue an existing orchestration (skips init checks)
**[V]alidate** - Check integrity of an existing orchestration state
**[E]dit** - Modify configuration of an existing orchestration

Please select: [C]reate / [R]esume / [V]alidate / [E]dit"

### 2.5. Premature-Completion Audit (added Story 24-7)

When resuming, the `state-latest-incomplete` script returns docs whose
`status` is NOT in `{COMPLETE, STOPPED, ABORTED}`. But a doc may have
`status=COMPLETE` written upstream while the invariant is unsatisfied
(the Epic 14/15 failure mode where every run wrote `COMPLETE` with
`stepsCompleted=[]`). On every wakeup tick, after loading the state doc
but before routing:

```bash
state_file="${resumeStatePath:-}"
if [ -n "$state_file" ] && [ -f "$state_file" ]; then
  verdict=$("{stateHelper}" orchestrator-helper validate-completion \
    --state "$state_file")
  ok=$(echo "$verdict" | jq -r '.ok')
  status=$(grep -m1 "^status:" "$state_file" | sed 's/status: *//;s/"//g' | tr -d ' ')

  if [ "$status" = "COMPLETE" ] && [ "$ok" != "true" ]; then
    missing=$(echo "$verdict" | jq -r '.missing[0]')
    case "$missing" in
      step-02-preflight)            revert_to=step-02-preflight ;;
      step-02a-preflight-config)    revert_to=step-02a-preflight-config ;;
      step-02b-preflight-finalize)  revert_to=step-02b-preflight-finalize ;;
      step-03-execute:*)            revert_to=step-03-execute ;;
      step-03a-execute-review:*)    revert_to=step-03a-execute-review ;;
      step-03b-execute-finish:*)    revert_to=step-03b-execute-finish ;;
      step-03c-execute-complete)    revert_to=step-03c-execute-complete ;;
      *)                            revert_to=step-03-execute ;;
    esac
    "{stateHelper}" orchestrator-helper state-update "$state_file" \
      --set status=IN_PROGRESS --set currentStep="$revert_to" \
      --set lastUpdated="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      --append-array stepsCompleted=wakeup-audit-revert-premature-complete
    echo "- **[$(date -u +%Y-%m-%dT%H:%M:%SZ)]** Wakeup audit reverted premature COMPLETE: missing=$missing → currentStep=$revert_to" >> "$state_file"
  fi
fi
```

Then re-route via step-01b-continue as usual; it will pick up at
`$revert_to` per its existing menu logic.

**Belt-and-suspenders.** §5a in step-04-wrapup catches the violation at
the source (refuses to write COMPLETE). This §2.5 audit catches the
case where some future code path writes `status=COMPLETE` directly
without going through step-04 — the exact failure mode every Epic 14/15
run exhibited. Both layers are needed.

### 3. Route to First Step

**IF mode == create:**
Load, read completely, then execute `steps-c/step-01-init.md`

**IF mode == resume:**
Prompt for state document path (optional): "Which orchestration would you like to resume? Provide the path or press Enter to use the latest incomplete state."

**If path provided:** Store as `{resumeStatePath}`, then load, read completely, and execute `steps-c/step-01b-continue.md`

**If no path (Enter pressed):**
Use script to find latest incomplete:
```bash
result=$("{stateHelper}" orchestrator-helper state-latest-incomplete "{outputFolder}")
resumeStatePath=$(echo "$result" | jq -r '.path // empty')
```
- **If found (resumeStatePath not empty):** Display "Found: {resumeStatePath}", then load, read completely, and execute `steps-c/step-01b-continue.md`
- **If not found:** Display "No incomplete orchestration found. Starting fresh.", then load, read completely, and execute `steps-c/step-01-init.md`

**IF mode == validate:**
Prompt for state document path: "Which orchestration state would you like to validate? Please provide the path to the state document."
Then load, read completely, and execute `steps-v/step-v-01-check.md`

**IF mode == edit:**
Prompt for state document path: "Which orchestration would you like to edit? Please provide the path to the state document."
Then load, read completely, and execute `steps-e/step-e-01-load.md`
