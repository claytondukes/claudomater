Execute the {{reviewer_bridge}} bridge for story {{story_id}}.

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

## Bridge workflow

{{skill_line}}{{workflow_line}}{{checklist_line}}Story file: _bmad-output/implementation-artifacts/{{story_prefix}}-*.md

You are running inside an autonomous story-automator session — no menus, no
confirmations. The bridge skill will detect whether the diff is frontend or
backend and invoke the project's configured senior reviewer on your behalf.
Follow the workflow exactly as written. {{extra_instruction}}
