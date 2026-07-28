# Non-Blocking Wait Ladder (stepped session/review polling)

## Why this exists

The orchestrator previously waited on tmux sessions with a **blocking**
`monitor-session` call (an internal poll loop, up to 60+ minutes) and on
Copilot reviews with a blocking `sleep 30` shell loop. Both run inside a
single Bash tool call, which the harness hard-caps (~10 min max, 2 min
default). The call gets killed mid-wait, recovery is undefined, and the
orchestration sits idle 10-15 minutes after the work already finished —
or never re-checks at all.

**The fix:** never block a tool call on external work. Do ONE cheap check,
persist the wait state, schedule a wakeup, END THE TURN. Repeat on wake.

## The ladder

Delays between checks, in seconds:

```
LADDER: 300, 180, 120, 120, 120, ...   (5 min, 3 min, 2 min, then every 2 min)
```

- `delaySeconds = 300` for the first wait after starting external work
  (sessions rarely finish in under 5 minutes),
- `180` for the second check,
- `120` for every check after that, until a terminal state.
- The ladder RESETS every time new external work starts (new session
  spawned, Copilot review re-requested).

## State contract (persisted in the state doc — REQUIRED)

Waits must survive turn boundaries and session restarts. Immediately after
starting external work, persist:

```bash
"$scripts" orchestrator-helper state-update "$state_file" \
  --set waitSession="$session" \
  --set waitKind=<create|dev|auto|review|retro|copilot-fix> \
  --set waitAgent="$current_agent" \
  --set waitStory="<story_id>" \
  --set waitPolls=0 \
  --set waitStartedAt="$(date +%s)" \
  --set waitEscalatedAt= \
  --set waitTimeoutMin=<per-kind timeout, e.g. 60>
```

`waitEscalatedAt` is the escalation clock-pause marker (see Timeouts below):
empty while the session is working, epoch-stamped while an escalation is
pending user input.

## The loop (orchestrator-level, one check per turn)

1. **Schedule the wakeup and end the turn.** Call `ScheduleWakeup` with:
   - `delaySeconds`: `LADDER[waitPolls]` (300 if `waitPolls==0`, 180 if `1`,
     else 120)
   - `prompt`: `"/story-automator resume"`
   - `reason`: specific, e.g. `"polling dev session for 14-1 (check #3)"`

   Then STOP — produce no further tool calls this turn.

   The project Stop hook recognizes this turn-end as a paced wait — a
   non-empty `waitSession` in the state doc plus a marker heartbeat fresher
   than 15 minutes — and lets the session stop instead of pumping it: the
   wakeup is the pump. This is why the state contract above is REQUIRED
   in this exact order: end the turn without persisting `waitSession`
   first and the hook blocks the stop, cancelling the wakeup you just
   scheduled and collapsing the ladder into a tight poll loop.

2. **On wake**, refresh the marker heartbeat FIRST — it is the liveness
   signal the Stop hook checks before allowing the next paced turn-end
   (stale heartbeat = pacing loop presumed dead = blocking resumes):

   ```bash
   "$scripts" orchestrator-helper marker heartbeat
   ```

   Then read the `wait*` fields from the state doc and run ONE
   non-blocking check:

   ```bash
   result=$("$scripts" check-session "$waitSession" --json --agent "$waitAgent" \
     --workflow "$waitKind" --story-key "$waitStory" --state-file "$state_file" \
     --started-at "$waitStartedAt" --timeout "$waitTimeoutMin")
   final_state=$(echo "$result" | jq -r '.final_state')
   ```

3. **Branch on `final_state`:**
   - `running` → increment `waitPolls` in the state doc, go to 1.
   - `completed` / `incomplete` / `crashed` / `stuck` / `not_found` /
     `timeout` → clear the `wait*` fields (`--set waitSession=`), kill the
     session (`"$scripts" tmux-wrapper kill "$waitSession"`), and continue
     the step's existing result handling. `check-session` emits the SAME
     JSON contract as `monitor-session` (`final_state`, `todos_done`,
     `todos_total`, `output_file`, `exit_reason`, `output_verified`), so
     all downstream parsing is unchanged.

## Re-entry rule (idempotence)

Steps that spawn sessions MUST guard on re-entry: if the state doc already
has a non-empty `waitSession` for the current story/phase, DO NOT respawn —
jump straight to the check (step 2 above). This is what makes the pattern
crash-safe: a killed turn resumes with a check, never a duplicate session.

## Timeouts

The per-kind timeout formerly enforced inside `monitor-session` is now
enforced across wakeups: `check-session --started-at --timeout` returns
`final_state=timeout` when the wall-clock budget is exhausted. Use the same
budgets as before (dev/create/review 60 min; retro per `$retro_timeout`;
copilot-fix 30 min). Codex sessions get 1.5x automatically.

**Escalation pauses the clock (Story 1.1 postmortem, 2026-07-14):** time a
session spends blocked on a pending escalation does NOT count against its
timeout budget. When an escalation is raised for the waiting session,
stamp `--set waitEscalatedAt="$(date +%s)"`. While `waitEscalatedAt` is
non-empty, ignore any `final_state=timeout` (the session is waiting on the
user, not stuck) and keep polling. When the answer is injected, shift the
clock forward by the pause and clear the marker:

```bash
pause=$(( $(date +%s) - waitEscalatedAt ))
"$scripts" orchestrator-helper state-update "$state_file" \
  --set waitStartedAt=$(( waitStartedAt + pause )) --set waitEscalatedAt=
```

`check-session --started-at` then measures only actual working time.
(First Acme run: dev 1.1 "exceeded" its 90m ceiling with ~60m of it
spent waiting on two escalation answers.)

## Completion detection tiers

`check-session` decides "completed" from three signals, strongest first:

1. **Stop-hook stamp (deterministic).** Child sessions run the project Stop
   hook when a turn ends; it stamps `turnCompletedAt` into the session state
   file (env: `STORY_AUTOMATOR_CHILD` + `STORY_AUTOMATOR_SESSION`, injected
   at spawn). No pane parsing, no model compliance needed.
2. **Completion timer line.** `<Verb> for Nm Ns` in the pane (Unicode-safe —
   verbs like "Sautéed" include accented letters).
3. **Frozen-pane fallback.** Idle-at-prompt + pane content (ticking footer
   stripped) unchanged across checks ≥60s apart.

Tiers 2-3 remain for codex agents (no Claude hooks) and sessions spawned
before the stamp existed. All three still require the prompt-visible gate
(no "esc to interrupt" on screen).

## Pane-watcher note

The Story 24-10 auto-answer pane-watcher runs inside every `session_status`
call, so each `check-session` check also answers whitelisted elicitation
prompts. Worst-case auto-answer latency is the current ladder gap (5 min on
the first check) — still strictly better than the prompt stalling a killed
blocking wait indefinitely.

## FORBIDDEN

- Blocking `monitor-session` calls from the orchestrator (legacy; kept only
  for non-interactive/CI contexts that have no ScheduleWakeup).
- `sleep` loops of any kind in orchestrator tool calls.
- Waiting without persisting `wait*` state first (a killed turn loses the wait).
- Fixed-interval polling faster than the ladder (burns prompt cache for nothing).
