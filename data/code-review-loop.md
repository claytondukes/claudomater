# Code Review Loop Pattern (v2.3)

**Purpose:** Code review loop execution using script-based automation with per-task agent configuration.

---

## Configuration

```
reviewCycle = 1
maxCycles = 5
```

---

## Agent Selection (v3.0)

Code-review uses **deterministic agent selection** from the agents file, same as all other workflow steps.

```bash
# Resolve agent for review task (uses agents file)
resolve_agent_for_task "review" "$state_file" "{story_id}"
review_agent="$primary_agent"
review_fallback="$fallback_agent"

echo "Code review using: primary=$review_agent, fallback=$review_fallback"
```

**Per-task override example in state document:**
```yaml
agentConfig:
  defaultPrimary: "codex"
  defaultFallback: "claude"
  perTask:
    review:
      primary: "claude"      # Override: use Claude for reviews
      fallback: false        # Disable fallback for reviews
```

**Note on Codex:** If Codex is configured for reviews and fails to update sprint-status, the `--workflow review` completion verification (check-session/monitor-session) catches this and returns `final_state: "incomplete"`, triggering the escalation path below.

---

## Loop Execution

**WHILE reviewCycle ≤ maxCycles:**

### 1. Spawn Review Session

```bash
scripts="$PROJECT_ROOT/.claude/skills/story-automator/scripts/story-automator"
[ -x "$scripts" ] || scripts="$HOME/.claude/skills/story-automator/scripts/story-automator"
[ -x "$scripts" ] || { echo "story-automator helper not found" >&2; exit 1; }

# ⚠️ CRITICAL: --command is REQUIRED - without it, no command runs → never_active failure!
# Spawn with story-automator tmux-wrapper (handles naming, state cleanup, env vars)
session_name=$("$scripts" tmux-wrapper spawn review {epic} {story_id} \
  --agent "$review_agent" \
  --cycle $reviewCycle \
  --command "$("$scripts" tmux-wrapper build-cmd review {story_id} --agent "$review_agent" --state-file "$state_file")")
```

### 2. Wait for the Session (non-blocking ladder, v2.4)

```bash
# Persist the wait so it survives turn boundaries (see data/wait-ladder.md)
"$scripts" orchestrator-helper state-update "$state_file" \
  --set waitSession="$session_name" --set waitKind=review --set waitAgent="$review_agent" \
  --set waitStory="{story_id}" --set waitPolls=0 \
  --set waitStartedAt="$(date +%s)" --set waitTimeoutMin=60
```

**WAIT — non-blocking stepped ladder:** call `ScheduleWakeup` (`delaySeconds`: 300 for
check #1, then 180, then 120 repeating; `prompt`: `/story-automator resume`; `reason`:
"polling review session for {story_id} (check #N)"), then END the turn. On each wake run
exactly ONE non-blocking check — `--workflow review --story-key` preserved for
sprint-status verification:

```bash
result=$("$scripts" check-session "$waitSession" --json \
  --agent "$waitAgent" \
  --workflow review --story-key "$waitStory" --state-file "$state_file" \
  --started-at "$waitStartedAt" --timeout "$waitTimeoutMin")
final_state=$(echo "$result" | jq -r '.final_state')
output_file=$(echo "$result" | jq -r '.output_file')
```

If `final_state == "running"` → increment `waitPolls`, schedule the next wakeup, END the
turn. On any terminal state, clear the wait and continue:

```bash
"$scripts" orchestrator-helper state-update "$state_file" --set waitSession=
```

**Note:** The `--workflow review --story-key` parameters enable sprint-status verification before marking complete.

### 3. Parse Output

```bash
# Sub-agent parsing (haiku, 99% cheaper than main context)
parsed=$("$scripts" orchestrator-helper parse-output "$output_file" review --state-file "$state_file")
```

### 4. Verify Sprint Status

```bash
status=$("$scripts" orchestrator-helper sprint-status get {story_key})
is_done=$(echo "$status" | jq -r '.done')
```

---

## Decision Logic

### Handle final_state (v2.2)

**IF final_state == "completed":**
- Session verified complete (sprint-status shows "done")
- Log "Code review passed, story marked done"
- Cleanup: `"$scripts" tmux-wrapper kill "$session_name"`
- **EXIT LOOP** → proceed to Git Commit

**IF final_state == "incomplete":** (v2.2 - Codex-specific)
- Session idle but sprint-status NOT updated
- Cleanup: `"$scripts" tmux-wrapper kill "$session_name"`
- Increment `reviewCycle`
- If `reviewCycle <= maxCycles`: count this as a failed attempt and **CONTINUE** with a retry
- If `reviewCycle > maxCycles`: Escalate with CRITICAL priority (Trigger #8), then present options:
  1. **[1] Manual Fix** - Update sprint-status.yaml yourself
  2. **[2] Run with Claude** - Re-run code-review with Claude agent
  3. **[3] Skip Story** - Mark story as skipped and continue
- **HALT** — wait for user choice only after maxCycles is exhausted

**IF final_state == "crashed" or "stuck":**
- Log "Review session failed: $final_state"
- Cleanup: `"$scripts" tmux-wrapper kill "$session_name"`
- Increment reviewCycle
- **CONTINUE** (retry with new session)

### Handle is_done check

**IF is_done == true:**
- Log "Sprint-status verified done"
- **EXIT LOOP** → proceed to Git Commit

**IF is_done == false AND final_state == "completed":**
- This shouldn't happen with v2.2 verification
- Fallback: check story file status
- If story file shows "done", treat as complete

**IF reviewCycle > maxCycles:**
- Check escalation: `"$scripts" orchestrator-helper escalate review-loop "cycles=$reviewCycle"`
- **HALT** — wait for user choice

---

## Sprint-Status Verification (v3.0)

Status is determined by **CRITICAL issues remaining** after auto-fix:
- "done" → 0 CRITICAL issues, proceed to commit
- "in-progress" → 1+ CRITICAL issues, new review cycle

HIGH/MEDIUM/LOW issues are tracked as action items but don't block automation.

---

## Output Verification Fallback (v1.4.0)

If `output_verified == false` or output truncated, use story file fallback:

```bash
file_status=$("$scripts" orchestrator-helper story-file-status {story_id})
# If status == "done", skip parsing - story is complete
```

---

## Verification Command (v2.2)

Check if code-review actually completed:

```bash
"$scripts" orchestrator-helper verify-code-review {story_id} --state-file "$state_file"
# Returns: {"verified":true/false, "sprint_status":"...", ...}
```
