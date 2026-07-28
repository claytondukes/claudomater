---
name: 'step-01-init'
description: 'Check for existing state and route appropriately'
nextStep: './step-02-preflight.md'
continueStep: './step-01b-continue.md'
outputFolder: '{output_folder}/story-automator'
outputFile: '{outputFolder}/init-log-{timestamp}.md'
rules: '../data/orchestrator-rules.md'
markerFile: '{project-root}/.claude/.story-automator-active'
scripts: '../scripts/story-automator'
ensureStopHook: '../scripts/story-automator'
stateHelper: '../scripts/story-automator'
settingsFile: '{project-root}/.claude/settings.json'
---

# Step 1: Initialize

**Goal:** Verify safeguards, check for existing state → resume or start fresh.

---

## Do

### 1. Verify Stop Hook Installation

**CRITICAL:** The Stop hook prevents premature stopping during orchestration.

Use script to ensure the Stop hook exists:
```bash
result=$("{ensureStopHook}" ensure-stop-hook --settings "{settingsFile}" \
  --command "{scripts} stop-hook" --timeout 10)
ok=$(echo "$result" | jq -r '.ok')
changed=$(echo "$result" | jq -r '.changed')
```

**IF ok == false:** Report error and STOP.

**IF changed == true:**
Display:
```
**Stop Hook Installed**

I've added the story-automator Stop hook to .claude/settings.json.
This prevents the orchestrator from randomly stopping mid-workflow.

⚠️ **Please restart this Claude session** for the hook to take effect.

After restarting, run the story-automator workflow again.
```
**HALT** - Do not proceed until user restarts

**IF changed == false:**
Display: "✓ Stop hook verified"
Continue to step 1b

### 1b. Verify Automator Assets (review bridge + per-project config)

A fresh BMAD install ships the standard `bmad-*` skills but NOT the automator's
review bridge, and never writes the per-project config. The `review` step
hard-requires `.claude/skills/bmad-story-automator-review/`.

Check for the bridge, and auto-remediate if missing (idempotent — creates only
what is absent, never overwrites):
```bash
bridge="{project-root}/.claude/skills/bmad-story-automator-review"
if [ ! -f "$bridge/SKILL.md" ]; then
  "{scripts}" setup "{project-root}"
fi
```

`setup` installs the review bridge, writes a prefilled
`_bmad/automator/story-automator.yaml` (project name from BMAD config, test
gauntlet auto-detected from the stack), and verifies the Stop hook + marker
gitignore. Review its summary; edit `story-automator.yaml` if the detected test
gauntlet or PR toggles are wrong. Then continue.

**IF the bridge was already present:** Display "✓ Review bridge verified" and continue.

### 1c. Health Check (BMAD capability doctor)

BMAD releases occasionally rename or replace skills. `doctor` resolves every
capability the automator calls (create-story, dev-story, generate-e2e-tests,
senior-review, retrospective) plus the declared path conventions against THIS
project — turning a would-be cryptic mid-run failure into an upfront diagnostic.

```bash
health=$("{scripts}" doctor "{project-root}" --json)
health_ok=$(echo "$health" | jq -r '.ok')
```

**IF health_ok == false:** Display the full report and HALT — do not start a run
against a project with unresolved capabilities:
```bash
"{scripts}" doctor "{project-root}"
```
The report names each missing capability and, when it can, suggests the renamed
skill to add to `steps.<step>.skillCandidates` in `data/orchestration-policy.json`
(or a per-project policy override). Fix, then re-run.

**IF health_ok == true:** Display "✓ BMAD capabilities verified" and continue.

### 2. Load Rules
Load `{rules}` once. These apply to all subsequent steps.

### 3. Check for Existing State
Search `{outputFolder}` for `orchestration-*.md` files.

Use deterministic state listing:
```bash
state_list=$("{stateHelper}" orchestrator-helper state-list "{outputFolder}")
latest_incomplete=$(echo "$state_list" | jq -r '.files | map(select(.status == "COMPLETE" | not)) | sort_by(.lastUpdated) | last | .path // empty')
```

**IF latest_incomplete is non-empty:**
- Display: "**Found existing orchestration in progress.**"
- Show: epic name, current story, current step, last updated
- → Load `{continueStep}`
- **STOP** (don't continue below)

**IF none found:**
- Continue to step 4

### 4. Welcome
Display:
```
**Welcome to Story Automator.**

I'll automate story implementation by spawning isolated sessions,
handling code review loops, and committing completed stories.

Everything is logged for full resumability.
```

### 5. Check Sprint Status (MANDATORY)
```bash
has_status=$("{stateHelper}" orchestrator-helper sprint-status exists)
sprint_ok=$(echo "$has_status" | jq -r '.exists')
```

**IF sprint_ok == false:** ABORT immediately.

Display:
```
**❌ Sprint status file not found.**

Expected: `_bmad-output/implementation-artifacts/sprint-status.yaml`

This file is required before running the story automator.
Please run the **sprint-planning** workflow first to generate it.
```
**HALT** - Do not proceed.

**IF sprint_ok == true:**
- Store for later reference during preflight
- Will be used to check if earlier stories need completion

### 6. Setup
Ensure `{outputFolder}` exists.

**6a. Detect orphaned prior inits (run-death trace — Story 1.1 postmortem, 2026-07-14):**
An init-log without a terminal line (`launched` / `aborted` / `superseded`)
means a previous run died between init/preflight and execute without leaving
any explanation. Mark each one superseded so the death is traceable:
```bash
for f in "{outputFolder}"/init-log-*.md; do
  [ -f "$f" ] || continue
  if ! grep -qE '\] (launched|aborted|superseded):' "$f"; then
    printf "[%s] superseded: no orchestration state was created by this run (died during init/preflight); superseded by {outputFile}\n" \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$f"
    echo "⚠️ Orphaned init from a previous run marked superseded: $f"
  fi
done
```

**6b. Write the run marker.** Append an initialization entry to `{outputFile}`
— this file is the run's trace and MUST exist before any interactive
preflight question:
```bash
printf \"[%s] init: stop-hook=%s existing_state=%s\\n\" \
  \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\" \"${changed}\" \"${latest_incomplete}\" >> \"{outputFile}\"
```

Store the path as `init_log` and carry it forward (step-02 → step-02a).
**Abandonment rule:** any path that ends this run before the orchestration
state document exists (user abort, fatal error) MUST append a terminal line
to `$init_log` first: `[ts] aborted: <reason>`. step-02a § 5 appends
`[ts] launched: state=<path>` right after state-doc creation, closing the
marker.

**Note:** Marker file (`{markerFile}`) is created in step-02b-preflight-finalize after epic/story context is established.

---

## Then
→ Load `{nextStep}`
