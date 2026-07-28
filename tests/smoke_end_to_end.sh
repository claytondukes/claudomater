#!/usr/bin/env bash
# Story 24-7 AC10 — end-to-end smoke test for Layer-1 wiring.
#
# Part A: happy path — every required append fires, §5a invariant
#         passes, status=COMPLETE is written, step-04-wrapup lands.
# Part B: missing-entry path — one step's append is omitted, §5a
#         invariant halts before status=COMPLETE.

set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$SKILL_DIR/scripts/story-automator"

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

fail() { echo "FAIL: $1" >&2; exit 1; }

# ----------------------------------------------------------------------
# Part A — happy path
# ----------------------------------------------------------------------

state_a="$tmp/state-a.md"
cat > "$state_a" <<'EOF'
---
storyRange: ["smoke-1"]
status: READY
currentStep: step-02-preflight
stepsCompleted: []
lastUpdated: ""
---

## Action Log

EOF

# Simulate every step's append in order.
"$SCRIPT" orchestrator-helper state-update "$state_a" \
  --append-array stepsCompleted=step-02-preflight >/dev/null
"$SCRIPT" orchestrator-helper state-update "$state_a" \
  --append-array stepsCompleted=step-02a-preflight-config >/dev/null
"$SCRIPT" orchestrator-helper state-update "$state_a" \
  --append-array stepsCompleted=step-02b-preflight-finalize >/dev/null
"$SCRIPT" orchestrator-helper state-update "$state_a" \
  --append-array stepsCompleted=step-03-execute:smoke-1 >/dev/null
"$SCRIPT" orchestrator-helper state-update "$state_a" \
  --append-array stepsCompleted=step-03a-execute-review:smoke-1 >/dev/null
"$SCRIPT" orchestrator-helper state-update "$state_a" \
  --append-array stepsCompleted=step-03b-execute-finish:smoke-1 >/dev/null
"$SCRIPT" orchestrator-helper state-update "$state_a" \
  --append-array stepsCompleted=step-03c-execute-complete >/dev/null

# §5a invariant check.
verdict=$("$SCRIPT" orchestrator-helper validate-completion --state "$state_a")
ok=$(echo "$verdict" | jq -r '.ok')
[ "$ok" = "true" ] || fail "Part A: validate-completion returned ok=$ok (verdict=$verdict)"

# §5 writes status=COMPLETE and appends step-04-wrapup.
"$SCRIPT" orchestrator-helper state-update "$state_a" \
  --set status=COMPLETE \
  --append-array stepsCompleted=step-04-wrapup >/dev/null

grep -q "status: COMPLETE" "$state_a" || fail "Part A: status not set to COMPLETE"
grep -q "step-04-wrapup" "$state_a" || fail "Part A: step-04-wrapup not in stepsCompleted"

echo "smoke Part A (happy path): PASS"

# ----------------------------------------------------------------------
# Part B — missing-entry path
# ----------------------------------------------------------------------

state_b="$tmp/state-b.md"
cat > "$state_b" <<'EOF'
---
storyRange: ["smoke-2"]
status: READY
currentStep: step-04-wrapup
stepsCompleted: []
lastUpdated: ""
---

## Action Log

EOF

# Append every required entry EXCEPT step-03a-execute-review:smoke-2.
for entry in \
  step-02-preflight \
  step-02a-preflight-config \
  step-02b-preflight-finalize \
  step-03-execute:smoke-2 \
  step-03b-execute-finish:smoke-2 \
  step-03c-execute-complete; do
  "$SCRIPT" orchestrator-helper state-update "$state_b" \
    --append-array stepsCompleted="$entry" >/dev/null
done

set +e
verdict=$("$SCRIPT" orchestrator-helper validate-completion --state "$state_b")
exit_code=$?
set -e

[ "$exit_code" -eq 1 ] || fail "Part B: validate-completion expected exit 1, got $exit_code"
ok=$(echo "$verdict" | jq -r '.ok')
[ "$ok" = "false" ] || fail "Part B: ok=$ok (expected false)"
missing=$(echo "$verdict" | jq -r '.missing | join(",")')
case "$missing" in
  *step-03a-execute-review:smoke-2*) ;;
  *) fail "Part B: missing list does not contain step-03a-execute-review:smoke-2 — got $missing" ;;
esac

# Section 5 must NOT have written COMPLETE on the missing-entry path.
grep -q "status: COMPLETE" "$state_b" && fail "Part B: status=COMPLETE was written despite invariant fail"

echo "smoke Part B (missing-entry path): PASS"
echo "smoke_end_to_end.sh: PASS"
