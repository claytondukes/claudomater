Execute the BMAD create-story workflow for story {{story_id}}.

## SANDBOX (MANDATORY, fail loudly)

Verify the working directory before any file or shell operation:

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
are allowed (project-scoped memory). Nothing else under `~` or `/` is allowed.

## Story creation

{{skill_line}}{{workflow_line}}{{instructions_line}}{{template_line}}{{checklist_line}}Create story file at: _bmad-output/implementation-artifacts/{{story_prefix}}-*.md

Story ID: {{story_id}}

#YOLO - Do NOT wait for user input.

## YOLO conventions (MANDATORY — spawned-session quality gate)

You are running inside `story-automator` as a spawned tmux session.
The orchestrator cannot answer interactive prompts. Follow these rules:

1. **Advanced elicitation is REQUIRED** per `.claude/CLAUDE.md` ("Always run
   advanced elicitation after initial creation"). Run it in the SAME session,
   immediately after the initial story file is written — invoke the
   `bmad-advanced-elicitation` skill directly via the Skill tool. Do NOT
   type the instruction into your own chat input box and wait — that
   queues text and stalls the session.

2. **Auto-approve elicitation edits.** When the elicitation flow asks
   "Apply all N edits (y/n)?" or any similar y/n confirmation about
   applying its proposed improvements, answer **y** and proceed. Sleep-mode
   pre-authorizes elicitation edits. Document each batch you applied in
   the story's Dev Notes / Change Log section so a human reviewer can see
   what changed.

3. **Never leave queued input unsent.** If you find yourself "about to type
   X to ask Y", do X via a direct tool call instead. The orchestrator polls
   the pane and will kill the session if it sits idle with queued input.

4. **No interactive checkpoints.** If a sub-skill or workflow you invoke
   would normally ask the user a question, decide it yourself using the
   story's context and the LZ enterprise standards in `CLAUDE.md`. Pick the
   path most likely to produce a senior-dev-reviewable story.

5. **Exit cleanly.** When the story file is written + elicitation applied +
   final validation passes, end your session (no trailing prompts, no
   "anything else?" pauses). The orchestrator detects session exit as the
   completion signal.

6. **Do NOT write PR / merge / Copilot steps as DEV tasks.** In the automator,
   the orchestrator owns push → open PR → Copilot convergence → merge, and it
   runs them only AFTER a separate senior code-review. The dev session
   implements code + tests + checkboxes and commits — it must not push, open
   PRs, run the Copilot loop, or merge. So when an acceptance criterion can only
   be proven via a real PR or post-merge (required-check enforcement, a
   squash-commit trailer, a deliberately-red proof PR, etc.), phrase it as an
   explicit **orchestrator / post-merge proof** (in Completion Notes or a
   clearly non-dev task) — never as a dev task that opens PRs, drives Copilot,
   or merges. Writing "the dev opens a PR and admin-merges" is the exact defect
   this rule prevents.
