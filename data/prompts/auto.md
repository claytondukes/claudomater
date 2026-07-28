Execute the {{label}} workflow for story {{story_id}}, then run the {{project_name}}
test gate before exiting.

{{skill_line}}{{workflow_line}}{{checklist_line}}Story file: _bmad-output/implementation-artifacts/{{story_prefix}}-*.md

## Step 0 (MANDATORY, BEFORE ANYTHING ELSE): Verify sandbox

```bash
PROJECT_ROOT="${PROJECT_ROOT:-$(git -C "$(pwd)" rev-parse --show-toplevel 2>/dev/null)}"
case "$(pwd)" in
  "$PROJECT_ROOT"|"$PROJECT_ROOT"/*) : ;;
  *) echo "ERROR: story-automator refuses to run outside $PROJECT_ROOT (pwd=$(pwd))" >&2; exit 1 ;;
esac
```

Then for the rest of this session: never read, write, edit, or shell-command
any path that is not under `$PROJECT_ROOT/`. Exception: writes to the
project-scoped memory dir `~/.claude/projects/$(echo "$PROJECT_ROOT" | sed 's#/#-#g')/memory/`
are allowed. Nothing else under `~` or `/` is allowed.

## Order of operations

1. Run the qa-generate-e2e-tests workflow as instructed by the skill above.
   Generate API/E2E tests for the story's feature surface. Use existing
   project test patterns; never hand-roll a competing framework.

2. **Test pass gate (mandatory before exit).** After test generation, run the
   project's full test gauntlet from the project root:

   ```bash
   set -e
   {{test_gauntlet}}
   ```

   If any command exits non-zero, fix the underlying problem in the story's
   implementation or in the newly-generated tests. Do NOT bypass, do NOT mark
   tests as `.skip` or `.todo` to make them pass, do NOT add timeouts to mask
   flakiness. Re-run until every gauntlet command exits zero.

3. Only exit the session once all gate commands pass. If you cannot
   make them pass after a genuine fix attempt, exit non-zero with a clear
   one-line reason on stderr so the automator escalates.

## Standing rules

- Auto-apply all discovered gaps in tests.
- Treat the test gauntlet as authoritative. The story is not test-ready until
  every gauntlet command exits zero.
- No workarounds, no fallbacks, no fake passes. {{extra_instruction}}
