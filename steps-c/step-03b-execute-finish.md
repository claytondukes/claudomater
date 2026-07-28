---
name: 'step-03b-execute-finish'
description: 'Finalize each story (commit/status), trigger retrospective when epic complete, and finish execution loop'
nextStep: './step-03c-execute-complete.md'
scriptsDir: '../scripts/story-automator'
outputFile: '{output_folder}/story-automator/orchestration-{epic_id}-{timestamp}.md'
---

# Step 3b: Finalize Story + Wrap Execution

**Goal:** After code review completes for a story, commit changes, verify sprint status, update progress, and finish the loop.
**Interaction mode:** Deterministic autonomous execution.

---

## Story Loop (Continue from Step 3)

### E. Git Commit

**Required:** Commit after every story (do not skip).

```bash
commit=$("{scriptsDir}" commit-story --repo "{project-root}" --story {story_id} --title "{title}")
ok=$(echo "$commit" | jq -r '.ok')
```

- If `ok == true`:
  ```bash
  # Update Story Progress: mark git-commit done
  tmp_state=$(mktemp)
  sed "s/^| ${story_id} |.*$/| ${story_id} | done | done | done | done | done | in-progress |/" "{outputFile}" > "$tmp_state" && mv "$tmp_state" "{outputFile}"
  ```
  → proceed to E.5
- If `ok == false` → log warning and escalate

### E.5 Push + Open PR (Phase 2 — non-blocking)

After the story commit, push the branch and open a PR on GitHub. Failures
here do NOT block the orchestrator — local commit + sprint-status are the
gate; PR creation is best-effort. If push or `gh pr create` fails, log
loudly and continue. Clay can manually open the PR in the morning.

**Pre-flight: sandbox check** (paranoid for autonomous runs)

```bash
PROJECT_ROOT="${PROJECT_ROOT:-$(git -C "$(pwd)" rev-parse --show-toplevel 2>/dev/null)}"
case "$(pwd)" in
  "$PROJECT_ROOT"|"$PROJECT_ROOT"/*) : ;;
  *) echo "ERROR: refusing to push/PR outside $PROJECT_ROOT (pwd=$(pwd))" >&2; exit 1 ;;
esac
```

**Resolve branch + story metadata:**

```bash
story_file=$(ls _bmad-output/implementation-artifacts/{story_id}-*.md 2>/dev/null | head -1)
if [ -z "$story_file" ]; then
  echo "ERROR: no story file for {story_id} — cannot build PR body" >&2
  echo "- **[$(date -u +%Y-%m-%dT%H:%M:%SZ)]** Story {story_id} PR open: SKIPPED (no story file)" >> "{outputFile}"
else
  story_slug=$(basename "$story_file" .md)
  epic_num="${story_slug%%-*}"
  branch_name="epic${epic_num}/${story_slug}"

  # Story title — prefer first H1, fall back to slug
  story_title=$(grep -m1 '^# ' "$story_file" | sed 's/^# *//' 2>/dev/null)
  [ -z "$story_title" ] && story_title="$story_slug"
fi
```

**Push branch:**

```bash
push_err=""
if ! git push -u origin "$branch_name" 2>&1 | tee /tmp/sa-push-{story_id}.log; then
  push_err=$(cat /tmp/sa-push-{story_id}.log)
  echo "ERROR: git push failed for $branch_name" >&2
  echo "- **[$(date -u +%Y-%m-%dT%H:%M:%SZ)]** Story {story_id} push: FAILED ($push_err)" >> "{outputFile}"
fi
rm -f /tmp/sa-push-{story_id}.log
```

If push failed, skip PR creation but still proceed to F.

**Open PR (or reuse existing):**

```bash
pr_number=""
pr_url=""

if [ -z "$push_err" ] && [ -n "$story_file" ]; then
  # Check for existing PR on this branch (resume scenarios, retries)
  existing_pr=$(gh pr list --head "$branch_name" --json number,url --jq '.[0]' 2>/dev/null)
  if [ -n "$existing_pr" ] && [ "$existing_pr" != "null" ]; then
    pr_number=$(echo "$existing_pr" | jq -r '.number')
    pr_url=$(echo "$existing_pr" | jq -r '.url')
    echo "- **[$(date -u +%Y-%m-%dT%H:%M:%SZ)]** Story {story_id} PR exists: #${pr_number} ${pr_url}" >> "{outputFile}"
  else
    # Extract Story and Acceptance Criteria sections for PR body
    story_section=$(awk '/^## Story$/{flag=1;next} /^## /{flag=0} flag' "$story_file" | head -80)
    ac_section=$(awk '/^## Acceptance Criteria$/{flag=1;next} /^## /{flag=0} flag' "$story_file" | head -80)
    [ -z "$story_section" ] && story_section="(See story file)"
    [ -z "$ac_section" ] && ac_section="(See story file)"

    pr_body_file=$(mktemp)
    cat > "$pr_body_file" <<PRBODY
## Story

${story_section}

## Acceptance Criteria

${ac_section}

---

**Story file:** \`_bmad-output/implementation-artifacts/${story_slug}.md\`
**Branch:** \`${branch_name}\`
PRBODY

    pr_create_out=$(gh pr create \
      --title "feat({story_id}): ${story_title}" \
      --body-file "$pr_body_file" \
      --head "$branch_name" \
      --base main 2>&1)
    pr_create_rc=$?
    rm -f "$pr_body_file"

    if [ $pr_create_rc -eq 0 ]; then
      pr_url="$pr_create_out"
      pr_number=$(echo "$pr_url" | grep -oE '/pull/[0-9]+$' | grep -oE '[0-9]+$')
      echo "- **[$(date -u +%Y-%m-%dT%H:%M:%SZ)]** Story {story_id} PR opened: #${pr_number} ${pr_url}" >> "{outputFile}"
    else
      echo "ERROR: gh pr create failed for {story_id}: $pr_create_out" >&2
      echo "- **[$(date -u +%Y-%m-%dT%H:%M:%SZ)]** Story {story_id} PR open: FAILED ($pr_create_out)" >> "{outputFile}"
    fi
  fi
fi
```

Whether PR succeeds or fails, proceed to E.6. Local commit + sprint-status are
authoritative for orchestration progress; PR is a non-blocking artifact.

### E.6 Copilot PR Review Loop (LZ custom, MANDATORY when PR opened)

**See `data/copilot-review-loop.md` for the full pattern, including
the three-signal convergence gate, sr-dev assessment rules, and the
thread-resolve mechanism for false positives.**

GitHub Copilot's auto-review fires **once** on PR creation. Subsequent
commits do **not** trigger re-review on their own. Without this loop,
real findings sit unaddressed and the story silently ships incomplete.
This was the primary failure mode caught in the Epic 15 postmortem
(2026-05-14): PR #132 needed **10 cycles** to converge cleanly,
finding 13 distinct real bugs (silent migration access weakening,
TypeError crashes in RBAC filter, Pydantic-extra fail-open on legacy
clients, malformed/wrong-type JSON fail-open, form save during groups
loading, …). A 5-cycle hard cap would have shipped them all.

**Skip this section only if** PR creation failed and `pr_number` is
empty. Otherwise it is required — **always, on every PR.**

> 🚨 **MANDATORY + DETECTION HARDENING (Story 1.1 postmortem, 2026-07-14).**
> The Copilot loop is NOT optional and NOT conditional on an ad-hoc
> "is Copilot enabled?" check. On the first real run the orchestrator
> queried `reviewRequests` + `/pulls/N/comments` filtered by the
> reviews-endpoint login, saw nothing, and wrongly concluded "Copilot
> not enabled" — skipping the loop entirely. Copilot WAS enabled and had
> left 3 real findings (incl. a genuine AC gap).
>
> **Endpoint gotcha:** Copilot's login is
> `copilot-pull-request-reviewer[bot]` on `/pulls/N/reviews` but
> `Copilot` on `/pulls/N/comments`. Filtering the comments endpoint by
> the reviews login silently returns nothing.
>
> **Rule:** Drive convergence via the `copilot-converge` skill (or its
> helper `.claude/skills/copilot-converge/scripts/copilot-review-status.sh`)
> — it bakes in the correct endpoints. The simplest correct path is to
> invoke the skill directly:
> `Skill(copilot-converge, args="PR #<n> (<owner>/<repo>) — <url>")`.
> Never decide "no Copilot" from a raw `gh` call; only the helper's
> `status`/`findings` output is authoritative. `retrigger` first (auto-
> review does not always fire on open), then treat an empty result with
> suspicion until the helper confirms `CONVERGED=1`.
>
> **Deterministic enablement check (never a 60-second sniff):** Copilot
> review is "enabled" iff EITHER signal confirms it:
> 1. A repo ruleset carries a `copilot_code_review` rule:
>    ```bash
>    for rid in $(gh api "repos/$OWNER/$REPO/rulesets" --jq '.[].id'); do
>      gh api "repos/$OWNER/$REPO/rulesets/$rid" \
>        --jq '[.rules[].type] | any(. == "copilot_code_review")'
>    done
>    ```
> 2. A review by login `copilot-pull-request-reviewer[bot]` appears on
>    `/pulls/N/reviews` within the loop's full no-show window (20 min +
>    up to 3 `@copilot` re-requests — Phase 1 below). A single short
>    wait proves nothing.
>
> NEVER filter one endpoint by the other endpoint's login, and never
> conclude "not enabled" until BOTH signals have come back negative.
>
> **When `copilot_loop: true` in `_bmad/automator/story-automator.yaml`,
> "Copilot not detected" is a BLOCKING escalation** — present the
> evidence (ruleset check output + review-poll history), WAIT for the
> user's answer, and skip the loop only if the user says so. A
> silent/non-blocking skip is forbidden: on the first real run it hid
> 3 real findings (including a genuine AC gap) for 10 hours.

**Loop terminator (three-signal convergence):** all three true on the
same cycle —

1. Latest Copilot review body matches:
   `Copilot reviewed N out of N changed files in this pull request and generated no new comments.`
2. `reviewThreads` query returns **zero** threads where
   `isResolved == false`.
3. CI `statusCheckRollup` has **no** check with `conclusion == "FAILURE"`.

If any one is false, continue. `safetyCap` (default 15) exists only
as a brake against unconvergeable false-positive loops — not as the
primary terminator.

The loop below spans MULTIPLE turns. It never blocks a tool call waiting for
Copilot: every wait is one cheap check + a stepped `ScheduleWakeup` (300s,
then 180s, then 120s repeating — `data/wait-ladder.md`). All loop state is
persisted in the state doc (`copilot*` keys) so a killed or resumed turn
never loses the review baseline — the historical failure mode where a review
arrived during a dead turn and the orchestrator re-baselined past it, then
sat blind for 10+ minutes.

**Phase 0 — init (run ONCE per PR; skip if the state doc already has
`copilotPr` set for this PR):**

```bash
# Derive owner/repo from the current repository (de-hardcoded for the global skill)
repo_full=$(gh repo view --json nameWithOwner --jq '.nameWithOwner' 2>/dev/null)
OWNER="${repo_full%%/*}"
REPO="${repo_full##*/}"

# Baseline BEFORE any waiting; auto-review fires on gh pr create, so at init
# this is usually empty and ANY review counts as new.
prev_review_at=$(gh api repos/$OWNER/$REPO/pulls/${pr_number}/reviews \
  --jq 'map(select(.user.login == "copilot-pull-request-reviewer[bot]")) | last.submitted_at // ""')

"{scriptsDir}" orchestrator-helper state-update "{outputFile}" \
  --set copilotPr="${pr_number}" --set copilotCycle=1 --set copilotInfraErrors=0 \
  --set copilotNoShows=0 --set copilotPrevReviewAt="${prev_review_at}" \
  --set copilotWaitPolls=0 --set copilotWaitStartedAt="$(date +%s)"
```

Then schedule the first wakeup (`delaySeconds: 300`, `prompt: "/story-automator resume"`,
`reason: "waiting for Copilot review on PR #{pr_number} (cycle 1, check #1)"`) and END
the turn. `safetyCap` is 15 cycles; `infraErrorCap` is 3.

**Phase 1 — check for a new review (ONE check per wake; never a sleep loop):**

```bash
cur_at=$(gh api repos/$OWNER/$REPO/pulls/${copilotPr}/reviews \
  --jq 'map(select(.user.login == "copilot-pull-request-reviewer[bot]")) | last.submitted_at // ""' 2>/dev/null)
```

- **New review** (`cur_at` non-empty AND `cur_at != copilotPrevReviewAt`) →
  IMMEDIATELY persist the new baseline, then go to Phase 2:

  ```bash
  "{scriptsDir}" orchestrator-helper state-update "{outputFile}" \
    --set copilotPrevReviewAt="${cur_at}" --set copilotNoShows=0
  ```

- **No new review** and `now - copilotWaitStartedAt < 1200` (20 min) →
  increment `copilotWaitPolls`, schedule the next wakeup on the ladder
  (180s if this was check #1, else 120s), END the turn.

- **No-show** (20+ min without a review) → Copilot lost the request;
  re-request and restart the ladder:

  ```bash
  gh pr edit "${copilotPr}" --add-reviewer "@copilot" >/dev/null 2>&1
  "{scriptsDir}" orchestrator-helper state-update "{outputFile}" \
    --set copilotNoShows=$((copilotNoShows + 1)) \
    --set copilotWaitPolls=0 --set copilotWaitStartedAt="$(date +%s)"
  echo "- **[$(date -u +%Y-%m-%dT%H:%M:%SZ)]** Story {story_id} PR #${copilotPr} Copilot cycle ${copilotCycle}: NO-SHOW ${copilotNoShows}/3 — re-requested review" >> "{outputFile}"
  ```

  After 3 consecutive no-shows → defer (Phase 3, deferred path). Otherwise
  schedule a 300s wakeup and END the turn.

**Phase 2 — process the new review:**

```bash
review_body=$(gh api repos/$OWNER/$REPO/pulls/${copilotPr}/reviews \
  --jq 'map(select(.user.login == "copilot-pull-request-reviewer[bot]")) | last.body' 2>/dev/null)
```

- **Infrastructure error** (`review_body` matches
  `"encountered an error and was unable to review"`): increment
  `copilotInfraErrors` in the state doc and log
  `SERVICE ERROR N/3`. If it reaches `infraErrorCap` (3) → defer (Phase 3).
  Otherwise re-request (`gh pr edit "${copilotPr}" --add-reviewer "@copilot"`),
  increment `copilotCycle`, reset `copilotWaitPolls=0` /
  `copilotWaitStartedAt=$(date +%s)`, schedule a 300s wakeup, END the turn.

- **Real review** → reset `copilotInfraErrors=0`, then run the three-signal
  convergence check (unchanged):

```bash
unresolved=$(gh api graphql -f query='
  { repository(owner:"'"$OWNER"'",name:"'"$REPO"'") {
      pullRequest(number:'"${copilotPr}"') {
        reviewThreads(first: 50) {
          nodes { isResolved comments(first:1) { nodes { path line body } } }
        }
      } } }' --jq '.data.repository.pullRequest.reviewThreads.nodes
                  | map(select(.isResolved == false)) | length' 2>/dev/null || echo 0)
ci_failure=$(gh pr view ${copilotPr} --json statusCheckRollup \
  --jq '[.statusCheckRollup[] | select(.conclusion == "FAILURE")] | length' 2>/dev/null || echo 0)

if echo "$review_body" | grep -qi "generated no new comments" \
   && [ "$unresolved" -eq 0 ] \
   && [ "$ci_failure" -eq 0 ]; then
  echo "- **[$(date -u +%Y-%m-%dT%H:%M:%SZ)]** Story {story_id} PR #${copilotPr} Copilot cycle ${copilotCycle}: CONVERGED (no new comments + 0 unresolved threads + 0 CI failures)" >> "{outputFile}"
  # converged → Phase 3
fi
```

- **Converged** → Phase 3 (merge path).
- **Not converged** → spawn a fix sub-agent. Pass the unresolved-threads JSON
  and the explicit instruction to (a) sr-dev-assess each finding per the
  4-question gate in `data/copilot-review-loop.md`, (b) apply only real
  fixes, (c) for each dismissed false positive run BOTH the reply comment
  AND the GraphQL `resolveReviewThread` mutation — pushing a commit alone
  does not silence Copilot, (d) record EVERY finding assessed this cycle —
  REAL (fixed), DISMISSED, or STALE — in the story file's Change Log entry
  for this round with a substantive rationale per finding; the entry must
  state the round's full finding count, matching the orchestration-log
  cycle line (Story 1.1 postmortem: a round-2 dismissal existed only in
  the orchestration log; the story Change Log said "2 findings" for a
  3-finding round).

```bash
threads_json=$(gh api graphql -f query='
  { repository(owner:"'"$OWNER"'",name:"'"$REPO"'") {
      pullRequest(number:'"${copilotPr}"') {
        reviewThreads(first: 50) {
          nodes { id isResolved comments(first:1) { nodes { id path line body } } }
        }
      } } }' --jq '.data.repository.pullRequest.reviewThreads.nodes
                  | map(select(.isResolved == false))' 2>/dev/null)

echo "- **[$(date -u +%Y-%m-%dT%H:%M:%SZ)]** Story {story_id} PR #${copilotPr} Copilot cycle ${copilotCycle}: ${unresolved} unresolved threads, ci_failure=${ci_failure}" >> "{outputFile}"

fix_cmd="$("{scriptsDir}" tmux-wrapper build-cmd copilot-fix {story_id} \
  --agent claude --state-file "{outputFile}" --pr "${copilotPr}" \
  --threads-json "$(echo "$threads_json" | jq -c .)")"
# Story 24-10 — re-derive pane-watcher flags from state doc
auto_answer=$(grep -E '^[[:space:]]+autoAnswerElicitation:' "{outputFile}" | head -1 | sed -E 's/.*autoAnswerElicitation:[[:space:]]*//')
auto_answer_flags=()
if [ "$auto_answer" = "true" ]; then
  auto_answer_flags=(--auto-answer-elicitation --state-doc-file "{outputFile}")
fi
fix_session=$("{scriptsDir}" tmux-wrapper spawn copilot-fix {epic} {story_id} \
  --agent claude --cycle "$copilotCycle" --command "$fix_cmd" \
  "${auto_answer_flags[@]}")
"{scriptsDir}" orchestrator-helper state-update "{outputFile}" \
  --set waitSession="$fix_session" --set waitKind=copilot-fix --set waitAgent=claude \
  --set waitStory="{story_id}" --set waitPolls=0 \
  --set waitStartedAt="$(date +%s)" --set waitTimeoutMin=30
```

  Wait for the fix session with the standard ladder (`data/wait-ladder.md` —
  300s/180s/120s wakeups, ONE `check-session` per wake). On any terminal
  state:

```bash
"{scriptsDir}" orchestrator-helper state-update "{outputFile}" --set waitSession=
"{scriptsDir}" tmux-wrapper kill "$waitSession"

# Re-trigger Copilot (the new commit alone does NOT re-trigger), advance the
# cycle, and restart the review-wait ladder.
gh pr edit "${copilotPr}" --add-reviewer "@copilot" >/dev/null 2>&1
"{scriptsDir}" orchestrator-helper state-update "{outputFile}" \
  --set copilotCycle=$((copilotCycle + 1)) \
  --set copilotWaitPolls=0 --set copilotWaitStartedAt="$(date +%s)"
```

  Then schedule a 300s wakeup (`reason: "waiting for Copilot re-review on PR
  #{pr_number} (cycle N)"`) and END the turn — back to Phase 1.

**Phase 3 — terminal:**

If `copilotCycle` exceeds the safety cap (15):

```bash
echo "- **[$(date -u +%Y-%m-%dT%H:%M:%SZ)]** Story {story_id} PR #${copilotPr} Copilot loop: HIT SAFETY CAP (15) — manual triage required" >> "{outputFile}"
# Do NOT silently mark the story complete. Story stays "in review".
# Most common cause when the cap is hit: a false-positive Copilot
# keeps re-raising that the sub-agent fixed in code but never
# resolved via the GraphQL mutation. Audit unresolved threads.
```

Auto-merge on clean convergence so sibling stories don't accumulate
overlapping diffs (Epic 15 hit this: three PRs all touched
settings-dynamic.test.ts because the first one didn't merge first).
Deferred PRs (service errors or 3 no-shows) stay open until a later session
re-triggers and the signal is real:

```bash
if [ "$converged" = "true" ]; then
  hold_labels=$(gh pr view "${copilotPr}" --json labels \
    --jq '[.labels[].name] | map(select(. == "do-not-merge" or . == "wip" or . == "hold")) | join(",")')
  if [ -n "$hold_labels" ]; then
    echo "- **[$(date -u +%Y-%m-%dT%H:%M:%SZ)]** Story {story_id} PR #${copilotPr}: hold label present (${hold_labels}); NOT auto-merging." >> "{outputFile}"
  elif gh pr merge "${copilotPr}" --squash --delete-branch 2>&1 \
       | tee -a "{outputFile}"; then
    echo "- **[$(date -u +%Y-%m-%dT%H:%M:%SZ)]** Story {story_id} PR #${copilotPr}: MERGED to main (--squash --delete-branch)." >> "{outputFile}"
  else
    # Ruleset / branch-protection block (Story 1.1 postmortem, 2026-07-14):
    # `main` may require an approving review the author cannot self-provide.
    # If the ONLY blocker is a policy the run is authorized to override,
    # retry with --admin (repo admin required). This is durably authorized
    # for this project (see memory feedback-story-automator-copilot-and-merge);
    # for other projects, confirm --admin once with the user.
    merge_state=$(gh pr view "${copilotPr}" --json mergeStateStatus,reviewDecision \
      --jq '.mergeStateStatus + "/" + (.reviewDecision // "")' 2>/dev/null)
    if gh pr merge "${copilotPr}" --squash --delete-branch --admin 2>&1 \
         | tee -a "{outputFile}"; then
      echo "- **[$(date -u +%Y-%m-%dT%H:%M:%SZ)]** Story {story_id} PR #${copilotPr}: MERGED via --admin (ruleset override; was ${merge_state})." >> "{outputFile}"
    else
      echo "- **[$(date -u +%Y-%m-%dT%H:%M:%SZ)]** Story {story_id} PR #${copilotPr}: merge FAILED even with --admin (${merge_state}) — see error above. Story is convergence-pending; escalate to user." >> "{outputFile}"
    fi
  fi
elif [ "$deferred" = "true" ]; then
  echo "- **[$(date -u +%Y-%m-%dT%H:%M:%SZ)]** Story {story_id} PR #${copilotPr}: NOT merging (deferred per Copilot service-error policy)." >> "{outputFile}"
fi
# Clear the copilot loop state for the next story
"{scriptsDir}" orchestrator-helper state-update "{outputFile}" --set copilotPr=
```

**Hard rules (from `data/copilot-review-loop.md`):**

1. **Convergence, not cycle count, is the gate.** Three signals must
   all be true: "no new comments" body + zero unresolved threads +
   no CI failures.
2. **Never apply Copilot suggestions blindly.** Sr-dev 4-question
   assessment first (anchor staleness / contract / LZ rules / scope).
3. **Resolve+reply is the way to silence false positives.** Pushing a
   commit message that mentions "false positive" does NOT stop the
   re-raise; the GraphQL `resolveReviewThread` mutation does.
4. **Stale anchor re-raises are a known false-positive pattern.**
   Verify the cited line before treating as live.
5. **PR description ↔ code consistency** — if Copilot's complaint is
   driven by a description mismatch, update the body, not the code.
6. **Auto-session "all green" claims must be verified against real CI**
   before accepting.
7. **Cycle 4 "no new comments" was a false convergence** in Epic 15
   PR #132 (cycles 5–9 found real bugs after). Three-signal check is
   why we don't trust the body alone.

### E.7 PR-Mode Sprint-Status Flip (done-gating on CI green)

**Skip this section if no PR was opened** (`pr_number` empty / `open_pr`
false) — in no-PR mode the review bridge already flipped sprint-status to
done and nothing here applies.

When a PR exists, the review bridge holds sprint-status at `review` (the
story file's Status carries the review verdict) and the review → done flip
is owned HERE (Story 1.1 postmortem, 2026-07-14: sprint-status said "done"
at 04:16Z while the PR's CI failed at 04:21Z). Flip only when BOTH hold:

1. **PR CI is green** — no failing and no still-running check:
   ```bash
   ci_not_green=$(gh pr view "${pr_number}" --json statusCheckRollup \
     --jq '[.statusCheckRollup[] | select((.conclusion // "") as $c
            | $c != "SUCCESS" and $c != "NEUTRAL" and $c != "SKIPPED")] | length' 2>/dev/null || echo 1)
   ```
   If checks are still running (`ci_not_green > 0` with pending checks),
   wait on the standard ladder (ONE check per wake — `data/wait-ladder.md`);
   never flip early. If a check FAILED, the story stays `review` — spawn a
   fix session (as done for the geiger CI failure on the first run) and
   re-check after it pushes.
2. **Copilot loop converged** (when `copilot_loop: true`) — E.6 hit the
   three-signal terminator. Deferred or safety-capped PRs do NOT flip: the
   story stays `review` (convergence-pending) per `data/copilot-review-loop.md`.

When both hold, flip the story's sprint-status entry from `review` to `done`
— the SINGLE sprint-status write the orchestrator is authorized to make (see
the exception in `data/orchestrator-rules.md`):

```bash
sprint_file="_bmad-output/implementation-artifacts/sprint-status.yaml"
story_key=$("{scriptsDir}" orchestrator-helper normalize-key {story_id} | jq -r '.key')
tmp_sprint=$(mktemp)
sed "s/^\([[:space:]]*${story_key}:[[:space:]]*\)review[[:space:]]*$/\1done/" "$sprint_file" > "$tmp_sprint" \
  && mv "$tmp_sprint" "$sprint_file"
echo "- **[$(date -u +%Y-%m-%dT%H:%M:%SZ)]** Story {story_id}: sprint-status review → done (PR #${pr_number} CI green + Copilot loop settled)" >> "{outputFile}"
```

### F. Verify Sprint Status

```bash
# Check sprint-status with story file fallback (v1.4.0)
normalized=$("{scriptsDir}" orchestrator-helper normalize-key {story_id})
story_key=$(echo "$normalized" | jq -r '.key')
status=$("{scriptsDir}" orchestrator-helper sprint-status get "$story_key")
is_done=$(echo "$status" | jq -r '.done')

# Fallback: trust story file if sprint-status disagrees (no-PR mode ONLY —
# in PR mode the story file says "done" while E.7 deliberately holds
# sprint-status at "review" until CI green + Copilot convergence, so the
# fallback would defeat the done-gate)
if [ "$is_done" != "true" ] && [ -z "$pr_number" ]; then
    file_done=$("{scriptsDir}" orchestrator-helper story-file-status {story_id} | jq -r '.status')
    [ "$file_done" = "done" ] && is_done="true"
fi
```

- If `is_done == false` in PR mode because E.7 withheld the flip (Copilot
  deferred / safety cap) → do NOT loop back to review; log convergence-pending
  and continue per `data/copilot-review-loop.md` (the story stays `review`)
- If `is_done == false` otherwise → return to Code Review Loop (Step 3, section D)
- If `is_done == true` → proceed to G

### G. Story Complete
Display: "**✅ Story {N} complete.**"
```bash
"{scriptsDir}" orchestrator-helper state-update "{outputFile}" \
  --set lastUpdated="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --append-array stepsCompleted=step-03b-execute-finish:${story_id}
echo "- **[$(date -u +%Y-%m-%dT%H:%M:%SZ)]** Story {story_id}: ✅ complete (commit + sprint-status verified)" >> "{outputFile}"

# Update Story Progress: mark story done
tmp_state=$(mktemp)
sed "s/^| ${story_id} |.*$/| ${story_id} | done | done | done | done | done | done |/" "{outputFile}" > "$tmp_state" && mv "$tmp_state" "{outputFile}"
```
Display: `[story {N}/{total}] finalize -> done`

### H. Check Epic Completion & Trigger Retrospective (Multi-Epic Support)

After each story completes, check if ALL stories in this epic are now done. Retrospective only triggers when every story in the epic has passed code review and sprint status confirms all are "done".

#### H.1 Check All Stories Done

```bash
# Run epic-level check in parallel with per-story checks
tmp_epic_status=$(mktemp)
("{scriptsDir}" orchestrator-helper sprint-status check-epic {epic_number} > "$tmp_epic_status") &
epic_status_pid=$!

# Get all stories for this epic and verify each is done
epic_stories=$("{scriptsDir}" orchestrator-helper get-epic-stories {epic_number} --state-file "{outputFile}")
stories_ok=$(echo "$epic_stories" | jq -r '.ok')
story_count=$(echo "$epic_stories" | jq -r '.count')
all_done=true

if [ "$stories_ok" != "true" ] || [ "$story_count" -eq 0 ]; then
    all_done=false
else
    tmp_story_checks=$(mktemp)
    echo "$epic_stories" | jq -r '.stories[]' \
      | xargs -I{} -P 4 sh -c '
          status=$("'"{scriptsDir}"'" orchestrator-helper sprint-status get "{}")
          done=$(echo "$status" | jq -r ".done")
          [ "$done" = "true" ] && echo "{}|done" || echo "{}|not_done"
        ' > "$tmp_story_checks"

    if rg -q '\|not_done$' "$tmp_story_checks"; then
      all_done=false
    fi
    rm -f "$tmp_story_checks"
fi
```

#### H.2 Secondary Verification via Sprint Status

```bash
# Double-check: use result from parallel epic-level check
wait "$epic_status_pid"
epic_status=$(cat "$tmp_epic_status")
rm -f "$tmp_epic_status"

epic_complete=$(echo "$epic_status" | jq -r '.allStoriesDone')
epic_ok=$(echo "$epic_status" | jq -r '.ok')

# Both checks must pass
if [ "$all_done" = "true" ] && [ "$epic_ok" = "true" ] && [ "$epic_complete" = "true" ]; then
    trigger_retro=true
else
    trigger_retro=false
fi
```

#### H.3 Trigger Retrospective (Only When Epic Fully Complete)

**IF trigger_retro == true:**

1. Display: "**✅ Epic {epic_number} complete! All stories passed code review. Triggering retrospective (YOLO mode)...**"
2. Log: `- **[{timestamp}]** Epic {epic_number}: ALL STORIES DONE - triggering retrospective`

```bash
# CRITICAL: Use build-cmd to get full YOLO prompt with doc verification
cmd=$("{scriptsDir}" tmux-wrapper build-cmd retro {epic_number} --agent "claude")
# Story 24-10 — re-derive pane-watcher flags from state doc
auto_answer=$(grep -E '^[[:space:]]+autoAnswerElicitation:' "{outputFile}" | head -1 | sed -E 's/.*autoAnswerElicitation:[[:space:]]*//')
auto_answer_flags=()
if [ "$auto_answer" = "true" ]; then
  auto_answer_flags=(--auto-answer-elicitation --state-doc-file "{outputFile}")
fi
session=$("{scriptsDir}" tmux-wrapper spawn retro "" {epic_number} --agent "claude" --command "$cmd" "${auto_answer_flags[@]}")

# Monitor with safe failure (never escalate on retro failure)
retro_timeout=60
[ "$story_count" -gt 10 ] && retro_timeout=90
"{scriptsDir}" orchestrator-helper state-update "{outputFile}" \
  --set waitSession="$session" --set waitKind=retro --set waitAgent=claude \
  --set waitStory="{epic_number}" --set waitPolls=0 \
  --set waitStartedAt="$(date +%s)" --set waitTimeoutMin="$retro_timeout"
```

**WAIT — non-blocking stepped ladder (full pattern: `data/wait-ladder.md`):** call
`ScheduleWakeup` (`delaySeconds`: 300 for check #1, then 180, then 120 repeating;
`prompt`: `/story-automator resume`; `reason`: "polling retro session for epic {epic_number} (check #N)"),
then END the turn. On each wake run exactly ONE non-blocking check:

```bash
result=$("{scriptsDir}" check-session "$waitSession" --json --agent claude \
  --started-at "$waitStartedAt" --timeout "$waitTimeoutMin")
```

If `final_state == "running"` → increment `waitPolls`, schedule the next wakeup, END the
turn. On any terminal state:

```bash
"{scriptsDir}" orchestrator-helper state-update "{outputFile}" --set waitSession=
"{scriptsDir}" tmux-wrapper kill "$waitSession"

retro_status=$(echo "$result" | jq -r '.final_state')

if [ "$retro_status" = "completed" ] || [ "$retro_status" = "success" ]; then
    echo "- **[{timestamp}]** Epic {epic_number} retrospective: completed successfully" >> "{outputFile}"
else
    echo "- **[{timestamp}]** Epic {epic_number} retrospective: skipped (reason: $retro_status)" >> "{outputFile}"
fi
```

3. Update state document with retrospective status:
```yaml
retrospectives:
  epic-{epic_number}:
    status: "completed" | "skipped"
    reason: "{reason_if_skipped}"
    timestamp: "{timestamp}"
```

4. **Continue to next story regardless of retrospective result** (retrospectives never block)

**IF trigger_retro == false:**
- Continue to next story (epic not yet complete)

**IMPORTANT RULES:**
- **ALL stories must be done**: Retrospective only triggers when every story in the epic shows "done" in sprint status
- **Use `build-cmd retro` with Claude**: Retrospectives do not support Codex
- **Never escalate; non-blocking**: If retrospective fails for any reason, log warning and continue

**END FOR EACH**

## Then
→ After all stories complete, load and execute `{nextStep}`
