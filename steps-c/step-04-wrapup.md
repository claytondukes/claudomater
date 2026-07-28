---
name: 'step-04-wrapup'
description: 'Finalize: summary, learnings, recommendations (terminal step)'
learningsFile: '{output_folder}/story-automator/learnings.md'
templates: '../data/wrapup-templates.md'
markerFile: '{project-root}/.claude/.story-automator-active'
stateFilePattern: '{output_folder}/story-automator/orchestration-*.md'
outputFile: '{output_folder}/story-automator/orchestration-{epic_id}-{timestamp}.md'
stateMetrics: '../scripts/story-automator'
reportRetentionPolicy: '../data/report-retention-policy.md'
---

# Step 4: Wrap-up

**Goal:** Generate summary, capture learnings, finalize state.
**Interaction mode:** Structured wrap-up with recommendation output.

---

## Do

### 1. Load Final State
From state document (located via `{stateFilePattern}`; resolved path stored as `{outputFile}` for this run), extract:
- Story progress table
- Action log
- Session references

Calculate:
- Stories completed vs total
- Code review cycles
- Escalations encountered

Use the existing state document path from execution, and derive `story_range_csv` from frontmatter `storyRange`.

Deterministic metrics:
```bash
metrics=$("{stateMetrics}" state-metrics --state "{state_document_path}")
```

Parallel optimization (metrics + retention policy extraction):
```bash
tmp_metrics=$(mktemp)
tmp_retention=$(mktemp)

("{stateMetrics}" state-metrics --state "{state_document_path}" > "$tmp_metrics") &
metrics_pid=$!

(awk '/^```bash/{flag=1;next}/^```/{flag=0}flag{print}' "{reportRetentionPolicy}" > "$tmp_retention") &
retention_pid=$!

wait "$metrics_pid"
wait "$retention_pid"

metrics=$(cat "$tmp_metrics")
retention_cmds=$(cat "$tmp_retention")
rm -f "$tmp_metrics" "$tmp_retention"
```

**Optimization (data ops):** If action log exceeds 200 lines, use compact summary by default.
```bash
log_block=$(awk '/^## Action Log/{flag=1;next}/^## /{if(flag){exit}}flag{print}' "{state_document_path}")
log_lines=$(printf "%s\n" "$log_block" | wc -l | tr -d ' ')
if [ "$log_lines" -gt 200 ]; then
  log_focus=$(printf "%s\n" "$log_block" | tail -n 50)
else
  log_focus="$log_block"
fi
```

### 2. Generate Summary
From `{templates}`, use **Summary Report Template**.

Fill in all stats and display to user.

### 3. Capture Learnings
Analyze run for patterns:
- Common code review issues
- Steps needing escalation
- Timing patterns
- What worked well

**IF `{learningsFile}` exists:** Load and merge
**ELSE:** Create new

Append entry using **Learnings Entry Template** from `{templates}`.

### 4. Recommendations
From `{templates}`, use **Recommendations Template**.

Present actionable suggestions based on patterns observed.

### 4b. Validation Report Housekeeping
Load `{reportRetentionPolicy}` and apply its retention guidance when needed.

If validation report history is large, run the suggested maintenance command from that policy file.

### 5a. Validate Completion (HARD GATE — added Story 24-7)

Before writing `status=COMPLETE`, prove every required step ran for every
story in the range. Reads off `../data/workflow-diagram.md` "State document
writes" table (canonical reference). DO NOT proceed to Section 5 on
`ok=false`.

```bash
verdict=$("{stateMetrics}" orchestrator-helper validate-completion \
  --state "{state_document_path}")
ok=$(echo "$verdict" | jq -r '.ok')
missing=$(echo "$verdict" | jq -r '.missing | join(", ")')

if [ "$ok" != "true" ]; then
  echo "ERROR: story-automator refuses to write status=COMPLETE." >&2
  echo "Missing required step entries: $missing" >&2
  echo "State document: {state_document_path}" >&2
  echo "Resume via /story-automator resume (step-01b-continue will" >&2
  echo "route to the first missing step)." >&2
  "{stateMetrics}" orchestrator-helper state-update "{state_document_path}" \
    --set lastUpdated="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --append-array stepsCompleted=step-04-wrapup-invariant-fail
  echo "- **[$(date -u +%Y-%m-%dT%H:%M:%SZ)]** step-04 invariant FAILED: missing=$missing" >> "{state_document_path}"
  exit 1
fi
```

If `ok=true`: proceed to Section 5 (Finalize State) which writes
`status=COMPLETE` and appends `step-04-wrapup` to `stepsCompleted`.

### 5. Finalize State
Update state document:
- `status = 'COMPLETE'`
- `completedAt = {timestamp}`
- Append final summary to action log

After writing `status=COMPLETE`, append the canonical `step-04-wrapup`
marker so the run's invariant ledger is complete (Story 24-7):

```bash
"{stateMetrics}" orchestrator-helper state-update "{state_document_path}" \
  --set status=COMPLETE \
  --set lastUpdated="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --append-array stepsCompleted=step-04-wrapup
```

Display: "**State document finalized.**"

### 6. Remove Marker File
Delete `{markerFile}` to disable the Stop hook safeguard.

This allows Claude to stop normally after workflow completion.

### 7. Workflow Complete

Display:
```
**🎉 Story Automator workflow complete!**

All stories have been processed through the build cycle.
Retrospectives were triggered automatically when each epic completed (during execution loop).

State document: {outputFile}
Learnings: {learningsFile}
```

Persist final state to `{outputFile}`.

---

## End

**Workflow terminates here.** Retrospectives are now handled within the execution loop (step-03b) when each epic completes, not as a separate terminal step.
