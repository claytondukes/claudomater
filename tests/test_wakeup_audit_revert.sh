#!/usr/bin/env bash
# Story 24-7 AC5 — synthetic test for workflow.md §2.5 Premature-Completion Audit.
#
# Setup state doc with status=COMPLETE but stepsCompleted missing one
# required entry. Run the §2.5 audit bash block. Verify:
#   - status reverts from COMPLETE → IN_PROGRESS
#   - currentStep reverts to the step matching the first missing entry
#   - wakeup-audit-revert-premature-complete marker is written
#   - action log line is appended

set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$SKILL_DIR/scripts/story-automator"

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

state_doc="$tmp/state.md"

# stepsCompleted has every required entry EXCEPT step-03b-execute-finish:test-1
cat > "$state_doc" <<'EOF'
---
storyRange: ["test-1"]
status: COMPLETE
currentStep: step-04-wrapup
stepsCompleted: [step-02-preflight, step-02a-preflight-config, step-02b-preflight-finalize, step-03-execute:test-1, step-03a-execute-review:test-1, step-03c-execute-complete]
lastUpdated: ""
---

## Action Log

EOF

# §2.5 bash block (inline copy — mirrors workflow.md §2.5)
state_file="$state_doc"
stateHelper="$SCRIPT"

set +e
verdict=$("$stateHelper" orchestrator-helper validate-completion --state "$state_file")
set -e
ok=$(echo "$verdict" | jq -r '.ok')
status=$(grep -m1 "^status:" "$state_file" | sed 's/status: *//;s/"//g' | tr -d ' ')

if [ "$status" = "COMPLETE" ] && [ "$ok" != "true" ]; then
  missing=$(echo "$verdict" | jq -r '.missing[0]')
  case "$missing" in
    step-02-preflight)            revert_to=step-02-preflight ;;
    step-02a-preflight-config)    revert_to=step-02a-preflight-config ;;
    step-02b-preflight-finalize)  revert_to=step-02b-preflight-finalize ;;
    step-03-execute:*)            revert_to=step-03-execute ;;
    step-03a-execute-review:*)    revert_to=step-03a-execute-review ;;
    step-03b-execute-finish:*)    revert_to=step-03b-execute-finish ;;
    step-03c-execute-complete)    revert_to=step-03c-execute-complete ;;
    *)                            revert_to=step-03-execute ;;
  esac
  "$stateHelper" orchestrator-helper state-update "$state_file" \
    --set status=IN_PROGRESS --set currentStep="$revert_to" \
    --set lastUpdated="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --append-array stepsCompleted=wakeup-audit-revert-premature-complete >/dev/null
  echo "- **[$(date -u +%Y-%m-%dT%H:%M:%SZ)]** Wakeup audit reverted premature COMPLETE: missing=$missing → currentStep=$revert_to" >> "$state_file"
fi

fail() { echo "FAIL: $1" >&2; exit 1; }

grep -q "status: IN_PROGRESS" "$state_doc" || fail "status was not reverted to IN_PROGRESS"
grep -q "currentStep: step-03b-execute-finish" "$state_doc" || fail "currentStep was not reverted to first-missing step"
grep -q "wakeup-audit-revert-premature-complete" "$state_doc" || fail "audit marker not written to stepsCompleted"
grep -q "Wakeup audit reverted premature COMPLETE" "$state_doc" || fail "audit action log line not appended"

echo "test_wakeup_audit_revert.sh: PASS"
