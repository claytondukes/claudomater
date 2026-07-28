# Copilot PR Review Loop (LZ custom, v2.0)

**Purpose:** After opening a PR, drive `copilot-pull-request-reviewer[bot]`
to a clean review through repeated trigger / sr-dev-assessment / fix /
resolve / re-trigger cycles. **Gate on convergence, not on cycle count.**

This is **distinct from** `code-review-loop.md`, which covers the BMAD
internal code-review session that runs *before* the PR is opened. This
file picks up *after* `step-03b-execute-finish.md` section E.5 (push +
open PR) and gates entry to F (verify sprint-status / story complete).

---

## Why this exists (Epic 15 postmortem, 2026-05-14)

During the first sleep-mode batch on Epic 15 the orchestrator opened
PRs #130 / #131 / #132 and advanced to the next story without ever
re-triggering Copilot. Cycle 1 findings sat unaddressed. When Clay
manually drove PR #132 through 10 cycles during the postmortem, the
following real bugs surfaced beyond cycle 1:

- Migration silently degraded restricted prompts to visible-to-all
- TypeError on `set()` of dict-shaped groups in RBAC filter
- Pydantic default `extra='ignore'` let legacy `rbac_requirement`
  posts silently create public prompts
- Malformed `allowed_groups` JSON failed open to public
- Valid-but-wrong-type JSON (`null`, `{}`) failed open to public
- Form didn't block save while groups list was loading/errored
- Migration regression test for delete-restricted-rows was missing

A 5-cycle hard cap would have stopped at cycle 5 and missed the
silent-public-access bugs in cycles 7–9. The right gate is
**convergence**, with a cycle cap acting only as a safety brake
against unconvergeable false-positive loops.

Root cause of the original sleep-mode failure: GitHub Copilot's
auto-review fires **once per PR creation**. Subsequent commits do
**not** trigger a re-review on their own.

---

## The mechanism

### Detecting whether Copilot review is enabled (deterministic)

**Never decide "Copilot not enabled" from an ad-hoc `gh` sniff.** On the
first Acme run (Story 1.1 postmortem, 2026-07-14) the orchestrator
queried `reviewRequests` + `/pulls/N/comments` filtered by the
reviews-endpoint login, waited 60 seconds, saw nothing, and skipped the
loop "non-blocking" — while Copilot was enabled and had 3 real findings.

**Endpoint login gotcha:** Copilot's login is
`copilot-pull-request-reviewer[bot]` on `/pulls/N/reviews` but `Copilot`
on `/pulls/N/comments`. Filtering one endpoint by the other endpoint's
login silently returns nothing. Never cross-filter.

Copilot is "enabled" iff EITHER signal confirms it:

1. **Ruleset check** — a repo ruleset carries a `copilot_code_review` rule:
   ```bash
   for rid in $(gh api "repos/$O/$R/rulesets" --jq '.[].id'); do
     gh api "repos/$O/$R/rulesets/$rid" \
       --jq '[.rules[].type] | any(. == "copilot_code_review")'
   done
   ```
2. **Review poll** — a review by login `copilot-pull-request-reviewer[bot]`
   appears on `/pulls/N/reviews` within the loop's full no-show window
   (20 min per attempt, up to 3 `@copilot` re-requests). A single short
   wait proves nothing.

Only after BOTH come back negative may "not enabled" even be considered —
and **when the project config sets `copilot_loop: true`, "Copilot not
detected" is a BLOCKING escalation**: present the ruleset output and the
review-poll history, wait for the user's answer, and skip the loop only if
the user says so. A silent/non-blocking skip is forbidden.

### Trigger an initial review

Automatic on `gh pr create`. No action needed; Copilot posts the first
review within ~3–5 minutes if it is enabled at the repo level.

### Trigger every subsequent review

```bash
gh pr edit <PR> --add-reviewer "@copilot"
```

`@copilot` is `gh`'s shortcut for the `copilot-pull-request-reviewer`
GitHub App (verified on github.com; not supported on GitHub Enterprise
Server). Pushing new commits **does not** trigger Copilot on its own.

### Resolve a thread to silence false-positive re-raises

```bash
# 1. Reply to the inline comment with our dismissal rationale
PARENT_COMMENT_ID=$(gh api repos/$O/$R/pulls/$PR/comments \
  --jq ".[] | select(.path == \"$PATH\" and .line == $LINE) | .id")
gh api -X POST repos/$O/$R/pulls/$PR/comments/$PARENT_COMMENT_ID/replies \
  -f body="Dismissed — addressed in commit $SHA. <one-line rationale>."

# 2. Resolve the thread via GraphQL (stops Copilot re-raising)
THREAD_ID=$(gh api graphql -f query='{ repository(owner:"…",name:"…"){
  pullRequest(number:'"$PR"'){ reviewThreads(first:30){
    nodes{ id comments(first:1){ nodes{ path line } } }
  } } } }' --jq ".data.repository.pullRequest.reviewThreads.nodes[]
    | select(.comments.nodes[0].path == \"$PATH\"
         and .comments.nodes[0].line == $LINE) | .id")
gh api graphql -f query='
  mutation($t: ID!) {
    resolveReviewThread(input: {threadId: $t}) {
      thread { id isResolved }
    }
  }' -f t="$THREAD_ID"
```

**Always do both.** The reply is the durable paper trail (Copilot's
re-trigger may un-anchor the thread; the reply text survives). The
resolve is what makes Copilot stop flagging the same issue.

Resolution is sticky across re-triggers and was confirmed to suppress
re-raises in the Epic 15 postmortem (7 resolved threads stayed
resolved through cycles 5–10).

---

## The loop

```
copilotCycle    = 1
safetyCap       = 10   # safety brake only; not the terminator
```

**Terminator (the primary gate):** all three conditions true on the
same cycle —

1. Latest review body matches: `Copilot reviewed N out of N changed files in this pull request and generated no new comments.`
2. `reviewThreads` query returns **zero** threads where `isResolved == false`.
3. CI status rollup shows no FAILURE on the relevant gating checks.

If any one is false, the loop continues.

**Safety brake:** `copilotCycle > safetyCap`. If hit, escalate with the
full unresolved-threads list. A real Copilot session should never need
more than ~15 cycles even on a large PR; if the cap is hit, something
unusual is going on (often: a false-positive Copilot keeps re-raising
that wasn't resolved correctly).

**Per-cycle steps:**

1. **Wait for the current review** (cycle 1 = the auto-review fired by
   `gh pr create`; cycle ≥ 2 = the one triggered by the previous
   iteration's `gh pr edit`). NEVER wait with a `sleep` loop — do ONE
   `submitted_at` check per turn against the persisted
   `copilotPrevReviewAt` baseline, then `ScheduleWakeup` on the stepped
   ladder (300s → 180s → 120s repeating; `data/wait-ladder.md`) and end
   the turn. Persist the baseline BEFORE waiting: a review that lands
   while a turn is dead must still register as new on resume. After
   20 min with no review, re-request `@copilot` (max 3 no-shows, then
   defer the PR).

2. **Convergence check (all three terminators):**
   ```bash
   review_body=$(gh api repos/$O/$R/pulls/$PR/reviews \
     --jq 'map(select(.user.login == "copilot-pull-request-reviewer[bot]")) | last.body')
   unresolved=$(gh api graphql -f query='
     { repository(owner:"…",name:"…") {
         pullRequest(number:'"$PR"') {
           reviewThreads(first:30) {
             nodes { isResolved }
           }
         }
       } }' --jq '[.data.repository.pullRequest.reviewThreads.nodes[]
                  | select(.isResolved == false)] | length')
   ci_failure=$(gh pr view $PR --json statusCheckRollup --jq \
     '[.statusCheckRollup[] | select(.conclusion == "FAILURE")] | length')
   if echo "$review_body" | grep -qi "generated no new comments" \
      && [ "$unresolved" -eq 0 ] \
      && [ "$ci_failure" -eq 0 ]; then
     break   # genuine convergence
   fi
   ```

3. **Sr-dev assessment (MANDATORY — Copilot is not always right):**
   For each unresolved thread, answer the 4-question gate (see "Sr-dev
   assessment" below) **before** writing any code.

4. **Apply real fixes; resolve+reply for false positives.** Push real
   fixes as new commits with clear messages. For dismissed findings,
   resolve the thread *and* reply to the comment with the rationale —
   both are required.

5. **Re-trigger:**
   ```bash
   gh pr edit $PR --add-reviewer "@copilot"
   ```

6. `copilotCycle++`, loop.

---

## Copilot infrastructure errors

The Copilot review service occasionally returns a review whose body is
literally `Copilot encountered an error and was unable to review this pull request. You can try again by re-requesting a review.`
This is **not** a real finding — it's an upstream service failure
(observed during the Epic 15 postmortem: PR #132 hit it 3× in a row
after a successful prior review).

**Handling:**

1. If the review body matches that error string, do **not** treat it as
   either convergence or a finding. Retry the `gh pr edit --add-reviewer
   "@copilot"` after a short delay.
2. After **3 consecutive** infrastructure errors with no new commits in
   between, the underlying service is degraded. Stop retrying, log the
   state in the orchestration log as **convergence-pending**, and **do
   NOT merge** this PR — it must hold for a clean Copilot review before
   merging. The orchestrator may **move on to the next story** in the
   queue; the deferred PR stays open and is revisited (the next loop
   iteration or a manual re-trigger).
3. Re-trigger on the next session (or when CI signals a relevant push)
   to confirm.

This sits in the middle ground between "converged" and "needs more
work" — the code is fine but we lack the Copilot signal to confirm it,
so we hold the merge until the signal is real.

---

## Auto-merge on convergence

Once the three-signal terminator is satisfied (body matches
`generated no new comments` + zero unresolved threads + zero CI
failures), the orchestrator **automatically merges the PR to main**
before advancing to the next story.

**Why:** Holding clean PRs in `open` state lets sibling stories accumulate
overlapping changes on the same files. Epic 15 demonstrated this — three
PRs all ended up touching `settings-dynamic.test.ts` because the first
one didn't merge before the others were started. Auto-merging the moment
a PR is genuinely clean prevents that drift and avoids manual rebase
work later.

```bash
# Pre-conditions checked already at this point: body OK, threads 0,
# CI green. Issue the merge.
gh pr merge "${pr_number}" --squash --delete-branch --auto
```

Merge mode is `--squash` to keep main's history linear and per-story.
`--delete-branch` cleans up the remote branch after merge.
`--auto` lets GitHub wait for any remaining required checks even
though our terminator already confirmed they're green — defense in
depth against a race where the orchestrator's view of CI lags
GitHub's required-status policy.

**Do NOT auto-merge when:**
- The story is **convergence-pending** because of Copilot infra
  errors (see the section above).
- A required check is failing (the three-signal terminator already
  blocks this case).
- The PR has labels signaling a hold (`do-not-merge`, `wip`, etc.) —
  respect them; log and defer.
- A maintainer has dismissed the auto-merge by clicking "Disable
  auto-merge" — the gh CLI will return non-zero; log and defer.

If `gh pr merge` fails, log the error verbatim, set the story to
convergence-pending, and continue to the next story. Do not retry
the merge in the same loop iteration.

---

## GitHub-enforced gate (post-24-11)

The orchestrator's in-skill terminator above is now mirrored by a
repo-level CI check, `copilot-review-converged`, implemented by
[`.github/workflows/copilot-review-gate.yml`](../../../../.github/workflows/copilot-review-gate.yml)
(Story 24-11). The check becomes *required* — i.e. blocking on merge —
only after a repo admin adds `copilot-review-converged` to the
`required_status_checks` rule on Ruleset 11750742 ("Require PR" on
`main`); see the story's Dev Notes (AC6) for the exact `gh api PUT`
recipe. Until that admin step lands, the gate runs on every PR and
reports pass/fail visibly but does not block the merge button.

Same three signals — minus CI rollup — gate the check: latest live
(non-dismissed) Copilot review body matches the case-insensitive regex
`generated no (new )?comments` (the `(new )?` group keeps the gate
liberal in what it accepts, per Story 24-11 AC3), zero unresolved review
threads (across Copilot AND human reviewers), no Copilot infra-error
string. CI rollup is intentionally NOT layered in: the existing
required-checks list already enforces it, and depending on rollup from
inside a job that is itself on the rollup risks deadlock.

Defense in depth: this skill's terminator remains the orchestrator's
primary gate; the workflow catches PRs the orchestrator did not open
(dependabot, hotfix branches, human contributors).

When the gate fails: resolve threads (§"Resolve a thread to silence
false-positive re-raises"), re-trigger Copilot (§"Trigger every
subsequent review"), push the fix. The workflow re-runs on `pull_request`
(opened / synchronize / reopened / ready_for_review) and
`pull_request_review` (submitted / dismissed) events — any of these will
refresh the gate's verdict.

---

## Convergence is a signal, not a guarantee

In the Epic 15 postmortem, **cycle 4 returned "no new comments"**, but
cycles 5–9 each found new real bugs after we re-triggered with the
threads resolved. Copilot's review is non-deterministic — different
sampling each invocation may surface different issues.

That means:

- The "no new comments" body is **necessary but not sufficient** for
  convergence. The skill terminator therefore requires **all three**
  signals (review body + zero unresolved threads + CI green).
- Even after the three-signal convergence, expect that a future
  re-trigger could surface something new. That's fine — once the
  story is merged the conversation is closed. The loop only needs to
  terminate when there are no live, real findings.

---

## Sr-dev assessment (the most important rule)

**Copilot is a high-recall, medium-precision reviewer.** It catches
real bugs, but it also:

- **Re-anchors stale comments** after the original line is moved or
  fixed. Same wording, different line, looks fresh. Always verify the
  cited line still contains the cited pattern before treating as live.
- **Repeats false positives** when the thread isn't resolved.
  Resolving stops the re-raise; pushing commits alone does not.
- **Misreads PR descriptions vs. code.** If the description says one
  thing and the code does another, Copilot frequently flags the *code*
  even when the code is correct and the description is wrong. The
  correct fix is often to update the description.
- **Underestimates intentional FAIL LOUDLY design.** Watch for reflex
  try/except suggestions that violate codebase conventions
  ("no defaults / no fallbacks / fail loudly / no skip workarounds").

For each finding, answer these four questions **before applying any
fix**:

1. **Does the cited line still contain what Copilot describes?** Read
   the file. If the anchor is stale, dismiss with a one-line note and
   resolve the thread.
2. **Is the cited contract real?** Verify against the actual upstream
   shape, schema, or hook return type. Don't trust the PR description
   as ground truth — read the code or the upstream serializer.
3. **Is the fix consistent with LZ enterprise rules?** No silent
   fallbacks, no TODOs, no skip workarounds, no backward-compat shims.
   If Copilot's suggested fix violates these, take a different
   approach that addresses the same concern.
4. **Is this in scope for the current story?** Real findings that are
   genuinely out of scope get a follow-up story and an explicit
   "Out of scope" PR-description line, not a silent skip.

When you dismiss a finding, **always do both**: post a reply with the
rationale **and** resolve the thread. Pushing a commit message
mentioning "false positive" without resolving doesn't stop the
re-raise.

**Change Log audit trail (HARD requirement — Story 1.1 postmortem,
2026-07-14):** EVERY finding assessed in a cycle — REAL (fixed),
DISMISSED, or STALE — must be recorded in the story file's Change Log
entry for that round with a substantive rationale (a four-word note is
not a rationale). The round's entry must state the full finding count,
matching the orchestration log's cycle line. On the first Acme run,
round 2's dismissal of finding A existed only in the orchestration log;
the story's Change Log said "2 findings" and recorded only B and C —
the dismissal decision was unauditable from the story doc.

---

## Trust calibration for auto-session "gate green" reports

Related rule (Epic 15 surfaced this twice). The bmad-story-automator-review
auto-test step (BMAD `qa-generate-e2e-tests` workflow) sometimes
reports "all gates green" while real CI fails. Specifically: a session
may declare pre-existing failures "out of scope" and exit clean, only
for the same failures to block the PR's CI run.

**Rule:** the auto-session's "green" claim is advisory, not
authoritative. After PR open, the **first thing** to verify is the
real CI status:

```bash
gh pr view $PR --json statusCheckRollup --jq \
  '.statusCheckRollup[] | "\(.name): \(.conclusion // .status)"'
```

If any check is FAILURE, treat it as an unfinished story even if the
auto-session's exit message said otherwise. The CI run is ground
truth; the auto-session report is not.

---

## PR description ↔ code consistency check

Before opening the PR (and on every push to it), re-read the body and
verify each claim against the code in the latest commit. If anything
drifts — endpoint name, field name, line reference, test count, fix
strategy — update the body before re-triggering Copilot. A PR body
that lies about its own code wastes a Copilot cycle and erodes review
trust.

This is the `feedback_pr_description_must_match_code` memory rule
elevated to a workflow gate. Cycle 1 of PR #131 spent its entire
budget on Copilot flagging a description-vs-code mismatch that was
the description's fault, not the code's.

---

## State tracking

In the orchestration state document, log each cycle:

```
- **[ts]** Story N PR #M Copilot cycle 1: 3 findings (2 real + 1 false-positive dismissed via resolve+reply). Fix push: SHA.
- **[ts]** Story N PR #M Copilot cycle 2: 1 finding (real). Fix push: SHA.
- **[ts]** Story N PR #M Copilot cycle 3: converged — body matches "no new comments", 0 unresolved threads, CI green.
```

A story is **not** finalized until the three-signal terminator hits,
or the safety cap fires and the residual findings have been triaged.

---

## What "story complete" means

Updated definition (post-Epic-15):

- Local commit landed
- Sprint-status verified "done" (in PR mode the review bridge holds
  sprint-status at "review"; step-03b § E.7 flips it to "done" only after
  PR CI is green and this loop has converged — Story 1.1 postmortem,
  2026-07-14: the story sat "done" while the PR's CI was failing)
- PR opened
- **Copilot loop converged on all three signals** (review body says
  "generated no new comments" AND zero unresolved threads AND CI
  rollup has no FAILURE), OR safety cap fired with explicit residual
  triage
- Lz-automator-review-flagged auto-session gate-green claim
  **verified against real CI**

Until all five are true, the story is "in review", not done.

---

## Postmortem reference

Epic 15 PR #132 needed **10 cycles** to converge cleanly, finding 13
distinct real bugs over the iteration. Notably:

- Cycle 4 said "no new comments" but cycles 5–9 each found new real
  fail-open access-control bugs. The "no new comments" body alone is
  not sufficient — verify unresolved threads too.
- Resolved threads stayed silent across cycles 5–10. The resolve
  mutation is sticky.
- The pattern across cycles 5–9 was a single theme ("fail open vs
  fail closed for access-control data") surfaced from different
  angles. Watch for thematic clusters — they often indicate a
  systemic design tightening rather than independent bugs.
