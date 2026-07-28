# Escalation Triggers

**Purpose:** Conditions that require human decision and cannot be resolved autonomously.

## Escalation Categories

### CRITICAL Escalations
**Definition:** Automation CANNOT proceed - requires human decision.

**Behavior:**
1. Delete marker file: `rm "{marker_file}"`
2. Update state: set status to PAUSED in state document
3. Present options (stop hook won't interfere)
4. Wait for user input
5. On resume: recreate marker, set IN_PROGRESS, continue

**Triggers in this category:**
- Code Review Loop Exceeded (#1)
- Session Spawn Failure (#3)
- Git Commit Failure (#4)
- Unexpected Error (#5)
- Dev-Story Implementation Failure (#7) when blocking + retries exhausted
- Session Incomplete (#8) - session finished but workflow not verified complete (v2.2)

### PREFERENCE Escalations
**Definition:** Automation COULD proceed either way - user chooses direction.

**Behavior:**
1. Keep marker file (automation still "active")
2. Present options
3. Act on selection immediately

**Triggers in this category:**
- Cannot Parse Session Output (#2)
- Dependency Conflict (#6)
- Dev-Story Implementation Failure (#7) when NOT blocking

---

## Escalation Protocol

When an escalation trigger is hit:
1. Categorize: CRITICAL or PREFERENCE
2. If CRITICAL: delete marker, set status to PAUSED
3. Notify: sound/notification
3b. If a session wait is active (`waitSession` set), pause its timeout
    clock: `--set waitEscalatedAt="$(date +%s)"` (see "Escalation pauses
    the timeout clock" below)
4. Present: situation + numbered options
5. Wait: halt until user responds
6. Log: record decision in action log — the entry MUST carry an explicit
   `resolved-by:` field naming who answered: `resolved-by: user`,
   `resolved-by: auto-answer-policy (<policy + trigger class>)`, or
   `resolved-by: orchestrator-default (<authorizing rule>)`
7. Resume: if CRITICAL, recreate marker, set IN_PROGRESS, continue; if the
   clock was paused in 3b, shift `waitStartedAt` forward by the pause and
   clear `waitEscalatedAt`

**Attribution rule (Story 1.1 postmortem, 2026-07-14):** The orchestrator
MUST NOT answer its own escalation. Presenting options and then submitting
the recommended option without a user reply is FORBIDDEN unless the active
policy explicitly authorizes auto-answer for that trigger class (e.g. the
Story 24-10 pane-watcher whitelist under `overrides.autoAnswerElicitation`)
— and then the log line must say so via `resolved-by: auto-answer-policy`.
A resolution line without `resolved-by:` is a defect: on the first Acme
run, Q2's log line read as the orchestrator submitting its own
recommendation and nobody could tell who chose.

**Escalation pauses the timeout clock (Story 1.1 postmortem, 2026-07-14):**
time a session spends blocked on a pending escalation does NOT count
against its timeout ceiling. On raise, record
`--set waitEscalatedAt="$(date +%s)"`; on resolution, shift the clock and
clear the marker:

```bash
pause=$(( $(date +%s) - waitEscalatedAt ))
"$scripts" orchestrator-helper state-update "$state_file" \
  --set waitStartedAt=$(( waitStartedAt + pause )) --set waitEscalatedAt=
```

While `waitEscalatedAt` is non-empty, a `final_state=timeout` from
`check-session` is invalid — the session is blocked on the user, not stuck;
keep polling. (First Acme run: dev 1.1 "exceeded" its 90m ceiling with
~60m of it spent waiting on two escalation answers.)

---

## Trigger Index

Each trigger includes its escalation message template in:
- `data/escalation-messages-core.md` (Triggers 1-4)
- `data/escalation-messages-extended.md` (Triggers 5-7)

### 1. Code Review Loop Exceeded (CRITICAL)
**Trigger:** Code review has run 5 cycles without clean status.
**See:** `escalation-messages-core.md#1-code-review-loop-exceeded`

### 2. Cannot Parse Session Output (PREFERENCE)
**Trigger:** Output doesn't match success/failure patterns.
**See:** `escalation-messages-core.md#2-cannot-parse-session-output`

### 3. Session Spawn Failure (CRITICAL)
**Trigger:** T-Mux session failed to spawn after retries.
**See:** `escalation-messages-core.md#3-session-spawn-failure`

### 4. Git Commit Failure (CRITICAL)
**Trigger:** Git commit failed (conflict, hook error, etc.).
**See:** `escalation-messages-core.md#4-git-commit-failure`

### 5. Unexpected Error (CRITICAL)
**Trigger:** Unhandled exception or unexpected condition.
**See:** `escalation-messages-extended.md#5-unexpected-error`

### 6. Dependency Conflict (PREFERENCE)
**Trigger:** Parallelism detects potential conflict.
**See:** `escalation-messages-extended.md#6-dependency-conflict`

### 7. Dev-Story Implementation Failure (CRITICAL or PREFERENCE)
**Trigger:** dev-story completes with errors after retries.
**See:** `escalation-messages-extended.md#7-dev-story-implementation-failure`

### 8. Session Incomplete (CRITICAL) [v2.2]
**Trigger:** `story-automator monitor-session` returns `final_state: "incomplete"` **after maxCycles exhausted**
**Condition:** Session finished (idle/exited) but workflow verification failed across all retry attempts.
**Typical cause:** Codex code-review session ended without updating sprint-status.

**Why CRITICAL (not PREFERENCE):**
- Automated retries already exhausted
- Human must decide: manual fix, use Claude, or skip story

**Options:**
1. **[1] Manual Fix** - Update sprint-status.yaml yourself
2. **[2] Run with Claude** - Re-run code-review with Claude agent
3. **[3] Skip Story** - Mark story as skipped and continue
4. **[X] Pause** - Stop orchestration for investigation

**Verification command:**
```bash
"$scripts" orchestrator-helper verify-code-review {story_id}
```

---

## Non-Escalation Conditions

Handled automatically (no escalation):
- Optional step (automate) skipped by override → log and continue
- Session completes with clear success → continue
- Session completes with clear failure → retry once, then escalate if still fails
