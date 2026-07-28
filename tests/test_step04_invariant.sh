#!/usr/bin/env bash
# Story 24-7 AC4 — synthetic test for step-04-wrapup §5a invariant gate.
#
# Setup a state doc with status=READY and stepsCompleted missing exactly
# one required entry. Run the §5a bash block. Verify:
#   - exit code is 1
#   - step-04-wrapup-invariant-fail is written to stepsCompleted
#   - action log line is appended
#   - status was NOT advanced to COMPLETE
#
# Invokes the wrapper script (./scripts/story-automator) not raw python.

set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$SKILL_DIR/scripts/story-automator"

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

state_doc="$tmp/state.md"

# Required entries for storyRange=[test-1] minus step-03b-execute-finish:test-1
cat > "$state_doc" <<'EOF'
---
storyRange: ["test-1"]
status: READY
currentStep: step-04-wrapup
stepsCompleted: [step-02-preflight, step-02a-preflight-config, step-02b-preflight-finalize, step-03-execute:test-1, step-03a-execute-review:test-1, step-03c-execute-complete]
lastUpdated: ""
---

## Action Log

EOF

# §5a bash block (inline copy — keeps the test independent of the markdown
# render). Mirrors steps-c/step-04-wrapup.md §5a exactly.
state_document_path="$state_doc"
stateMetrics="$SCRIPT"

set +e
verdict=$("$stateMetrics" orchestrator-helper validate-completion \
  --state "$state_document_path")
verdict_exit=$?
set -e

ok=$(echo "$verdict" | jq -r '.ok')
missing=$(echo "$verdict" | jq -r '.missing | join(",")')

if [ "$ok" != "true" ]; then
  "$stateMetrics" orchestrator-helper state-update "$state_document_path" \
    --set lastUpdated="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --append-array stepsCompleted=step-04-wrapup-invariant-fail >/dev/null
  echo "- **[$(date -u +%Y-%m-%dT%H:%M:%SZ)]** step-04 invariant FAILED: missing=$missing" >> "$state_document_path"
  invariant_fired=1
else
  invariant_fired=0
fi

# Assertions
fail() { echo "FAIL: $1" >&2; exit 1; }

[ "$verdict_exit" -eq 1 ] || fail "validate-completion expected exit 1, got $verdict_exit"
[ "$invariant_fired" -eq 1 ] || fail "invariant did not fire on missing entry"
grep -q "step-04-wrapup-invariant-fail" "$state_doc" || fail "invariant-fail marker not written to state doc"
grep -q "step-04 invariant FAILED" "$state_doc" || fail "invariant-fail action log line not appended"
grep -q "status: READY" "$state_doc" || fail "status was advanced past READY despite invariant fail"
grep -q "status: COMPLETE" "$state_doc" && fail "status=COMPLETE was written despite invariant fail"

echo "test_step04_invariant.sh: PASS"
