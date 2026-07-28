---
name: 'step-03a-execute-review'
description: 'Autonomous execution loop - automate and code review'
nextStep: './step-03b-execute-finish.md'
scriptsDir: '../scripts/story-automator'
outputFile: '{output_folder}/story-automator/orchestration-{epic_id}-{timestamp}.md'
retryStrategy: '../data/retry-fallback-strategy.md'
reviewLoop: '../data/code-review-loop.md'
---

# Step 3a: Execute Review Phase

**Goal:** Run automate (guardrails) and code review loop for the current story.
**Interaction mode:** Deterministic autonomous execution.

---

## Prerequisites

- Step 3 completed (create-story and dev-story done)
- State document updated with current story progress

Set: `scripts="{scriptsDir}"`

---

## Story Loop (Continue from Step 3)

### C. Automate (Guardrails)
*Skip if `overrides.skipAutomate`*

**Apply retry/fallback pattern from `{retryStrategy}`:** Non-blocking, but still retry on failure.

```bash
# Story 24-10 — re-derive pane-watcher flags from state doc
auto_answer=$(grep -E '^[[:space:]]+autoAnswerElicitation:' "$state_file" | head -1 | sed -E 's/.*autoAnswerElicitation:[[:space:]]*//')
auto_answer_flags=()
if [ "$auto_answer" = "true" ]; then
  auto_answer_flags=(--auto-answer-elicitation --state-doc-file "$state_file")
fi

# --command required (see Spawn Pattern in step-03)
session=$("$scripts" tmux-wrapper spawn auto {epic} {story_id} \
  --agent "$current_agent" \
  --command "$("$scripts" tmux-wrapper build-cmd auto {story_id} --agent "$current_agent" --state-file "$state_file")" \
  "${auto_answer_flags[@]}")
"$scripts" orchestrator-helper state-update "$state_file" \
  --set waitSession="$session" --set waitKind=auto --set waitAgent="$current_agent" \
  --set waitStory="{story_id}" --set waitPolls=0 \
  --set waitStartedAt="$(date +%s)" --set waitTimeoutMin=60
```

**WAIT — non-blocking stepped ladder (full pattern: `data/wait-ladder.md`):** call
`ScheduleWakeup` (`delaySeconds`: 300 for check #1, then 180, then 120 repeating;
`prompt`: `/story-automator resume`; `reason`: e.g. "polling auto session for {story_id} (check #N)"),
then END the turn. Re-entry guard: if the state doc already has a non-empty `waitSession`
for this story/phase, skip the spawn above and start here. On each wake run exactly ONE
non-blocking check (NEVER the blocking `monitor-session`):

```bash
result=$("$scripts" check-session "$waitSession" --json --agent "$waitAgent" \
  --started-at "$waitStartedAt" --timeout "$waitTimeoutMin")
```

If `final_state == "running"` → increment `waitPolls` in the state doc, schedule the next
wakeup, END the turn. On any terminal state (`completed` / `incomplete` / `crashed` /
`stuck` / `not_found` / `timeout` — same JSON contract as `monitor-session`):

```bash
"$scripts" orchestrator-helper state-update "$state_file" --set waitSession=
"$scripts" tmux-wrapper kill "$waitSession"
```

- SUCCESS:
  ```bash
  # Update Story Progress: mark automate done
  tmp_state=$(mktemp)
  sed "s/^| ${story_id} |.*$/| ${story_id} | done | done | done | - | - | in-progress |/" "{outputFile}" > "$tmp_state" && mv "$tmp_state" "{outputFile}"
  ```
  Display: `[story {N}/{total}] automate -> done`
  → proceed to D
- FAILURE → retry up to 3 attempts (non-blocking, so fewer retries), then log warning:
  ```bash
  # Update Story Progress: mark automate skipped
  tmp_state=$(mktemp)
  sed "s/^| ${story_id} |.*$/| ${story_id} | done | done | skip | - | - | in-progress |/" "{outputFile}" > "$tmp_state" && mv "$tmp_state" "{outputFile}"
  ```
  Display: `[story {N}/{total}] automate -> skip (non-blocking)`
  → proceed to D

### D. Code Review Loop

**See `{reviewLoop}` for complete script-based review cycle with v2.3 per-task agent configuration.**

**MANDATORY log-summary contract (every review cycle):**
- Run a single grep/regex pass over review output first.
- Return only compact fields to parent flow: `next_action`, `confidence`, `error_class`, `issues_count`, `top_issues`.
- Do not carry full log payloads forward unless escalation requires raw evidence.

```bash
review_log=$(echo "$result" | jq -r '.output_file')
review_focus=$(grep -nE "SUCCESS|FAIL|ERROR|CRITICAL|WARN|RETRY|ESCALATE|ISSUE" "$review_log" | head -n 120)
if [ -z "$review_focus" ]; then
  review_focus=$(tail -n 120 "$review_log")
fi

# Compact subprocess-style summary contract for parent flow
review_summary=$("$scripts" orchestrator-helper parse-output "$review_log" review --state-file "$state_file" | jq -c '
  {
    next_action: (.next_action // "retry"),
    confidence: (.confidence // 0),
    error_class: (.error_class // "unknown"),
    issues_count: ((.issues // []) | length),
    top_issues: ((.issues // [])[:3])
  }
')
```

Key points:
- Up to 5 cycles using `story-automator tmux-wrapper spawn review` + the non-blocking `check-session` wait ladder (`data/wait-ladder.md`)
- **Agent:** Uses per-task config from state document (`resolve_agent_for_task "review"`)
- **Verification:** Uses `--workflow review --story-key` for sprint-status verification
- **States:** `completed` (verified):
  ```bash
  # Update Story Progress: mark code-review done
  tmp_state=$(mktemp)
  sed "s/^| ${story_id} |.*$/| ${story_id} | done | done | done | done | - | in-progress |/" "{outputFile}" > "$tmp_state" && mv "$tmp_state" "{outputFile}"
  ```
  Display: `[story {N}/{total}] review -> done`
  → E | `incomplete` → count as failed attempt, retry until maxCycles, then CRITICAL escalate (Trigger #8)
- Exit loop when the review verifier confirms completion — sprint-status "done", or (PR mode, `open_pr: true`) story-file Status "done" while the bridge holds sprint-status at "review"; step-03b § E.7 owns the review → done flip after PR CI green + Copilot convergence
- If `review_summary.next_action` is ambiguous, ask one clarifying question before escalating.

---

## Auto-Proceed to Finalization

Display: "**Code review complete. Proceeding to finalize commits and status checks...**"

```bash
"$scripts" orchestrator-helper state-update "{outputFile}" \
  --set currentStep=step-03b-execute-finish \
  --set lastUpdated="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --append-array stepsCompleted=step-03a-execute-review:${story_id}
echo "- **[$(date -u +%Y-%m-%dT%H:%M:%SZ)]** Code review complete, proceeding to finalization" >> "{outputFile}"
```

---

## Then
→ Immediately load and execute `{nextStep}`
