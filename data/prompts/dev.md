Execute the BMAD dev-story workflow for story {{story_id}}, but first verify
sandbox + the story branch per the project's rules.

{{skill_line}}{{workflow_line}}{{instructions_line}}{{checklist_line}}Story file: _bmad-output/implementation-artifacts/{{story_prefix}}-*.md

## Step -1 (MANDATORY, BEFORE ANYTHING ELSE): Verify sandbox

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

## Step 0 (MANDATORY, BEFORE ANY CODE WORK): Verify or create story branch

Branching rule: every story is developed on its own branch (named by the
project's `branch_pattern`, default `epic<N>/<story-slug>`) branched from
`main`. Run this before doing anything else — if anything fails, exit
non-zero with a clear reason.

```bash
set -e
story_id="{{story_id}}"
story_prefix="${story_id//./-}"

# Resolve the actual story file to extract the slug (e.g.
# "14-1-triggers-tolocalestring-null-guard")
story_file=$(ls _bmad-output/implementation-artifacts/${story_prefix}-*.md 2>/dev/null | head -1)
if [ -z "$story_file" ]; then
  echo "ERROR: no story file matching ${story_prefix}-*.md — cannot determine branch name" >&2
  exit 1
fi
story_slug=$(basename "$story_file" .md)
epic_num="${story_slug%%-*}"
# Branch name comes from the project's branch_pattern (story-automator.yaml);
# defaults to epic${epic_num}/${story_slug}.
branch_name="{{branch_pattern}}"

current_branch=$(git rev-parse --abbrev-ref HEAD)

if [ "$current_branch" = "$branch_name" ]; then
  echo "Already on story branch $branch_name — continuing."
else
  # Always branch from main per the project's branching rule. `git switch -c` carries
  # uncommitted changes (e.g. orchestrator marker, .gitignore tweaks) onto
  # the new branch, which is intentional. If the branch already exists from
  # a prior run, switch to it instead of creating.
  if git show-ref --verify --quiet "refs/heads/${branch_name}"; then
    git switch "$branch_name"
    echo "Switched to existing story branch $branch_name."
  else
    git switch -c "$branch_name" main
    echo "Created story branch $branch_name from main."
  fi
fi
```

Only proceed to the dev workflow below once the branch step prints success.

## Dev workflow

Implement all tasks marked [ ]. Run tests. Update checkboxes.

## Boundary — where the dev session STOPS (orchestrator owns the rest)

You implement code + tests, update the story's task checkboxes, and COMMIT your
work to the story branch. STOP THERE. Do **not** — even if the story's tasks
describe them:

- push the branch, or open / close / comment on PRs (including scratch PRs);
- drive or wait on the GitHub Copilot review loop;
- merge (squash / `--admin` / any method) to `main`.

Those steps are the ORCHESTRATOR's finish-phase and run only AFTER a separate
senior code-review. Story tasks that read like "open a PR", "drive the Copilot
loop", or "admin-merge" are written for a solo-human flow — in the automator the
orchestrator performs them, not you. If an acceptance criterion can ONLY be
proven via a real PR or post-merge (e.g. required-check enforcement blocking a
red PR, or the squash commit's trailer), implement the code, record the proof as
an explicit **orchestrator / post-merge** step in Completion Notes, and leave it
for the orchestrator. Do not open the PR or merge yourself.
