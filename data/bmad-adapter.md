# BMAD Capability Adapter (v1.0.0)

**Why:** BMAD releases churn. The building-block skills the automator calls
(`create-story`, `dev-story`, `generate-e2e-tests`, `retrospective`,
`sprint-status`) have been stable across BMAD 6.6 → 6.10, but the **top-level
orchestrator** is exactly what BMAD keeps replacing (it became `bmad-loop`). Our
strategy: **own the orchestration, call the stable primitives through a thin
adapter** so a BMAD rename is a one-line edit, not a broken run.

---

## The seam: `skillCandidates`

Each step in `orchestration-policy.json` names its BMAD skill via an ordered
candidate list instead of a single hardcoded name:

```json
"dev": {
  "assets": {
    "skillName": "bmad-dev-story",
    "skillCandidates": ["bmad-dev-story"]
  }
}
```

`resolve_skill_name()` (in `core/runtime_policy.py`) picks the **first candidate
whose `SKILL.md` exists**; `skillName` is appended as the final fallback (so
legacy policies and the review-bridge override keep working). When BMAD renames
`bmad-dev-story` → `bmad-something-else`, add that name to the list:

```json
"skillCandidates": ["bmad-dev-story", "bmad-something-else"]
```

Nothing else in the orchestration changes. The whole coupling surface is ~5 skill
names in this one file.

## Declared path conventions: `bmadPaths`

The other coupling surface — where BMAD keeps its config and artifacts — is
declared once in the policy so it's overridable and verifiable:

```json
"bmadPaths": {
  "config": "_bmad/bmm/config.yaml",
  "implementationArtifacts": "_bmad-output/implementation-artifacts",
  "sprintStatus": "_bmad-output/implementation-artifacts/sprint-status.yaml",
  "planningArtifacts": "_bmad-output/planning-artifacts"
}
```

## `doctor` — fail loud, fail early

`story-automator doctor [PROJECT_ROOT]` resolves every capability + path against
the current project and prints a green/red report. The init step runs it
automatically and HALTs on failure, so a BMAD upgrade that renames a skill
surfaces as:

```
✗ dev-story   none of ['bmad-dev-story'] present
     ↳ similar installed skills: bmad-dev-auto
       (add the right skill to steps.dev.skillCandidates in orchestration-policy.json)
```

instead of a cryptic mid-run `PolicyError`. Rename hints are drawn only from
*unclaimed* installed skills (skills already mapped to another capability are
never suggested), ranked by token overlap; when a rename shares no token (e.g.
`bmad-loop`), doctor says so honestly rather than guessing.

Doctor degrades gracefully: if a project's review bridge is incomplete (so the
full policy loader would throw), it falls back to the bundled policy structure so
it can still enumerate every problem.

## What we bundle (immune to BMAD reinstalls)

The review bridge (`bmad-story-automator-review`) is genuinely ours — it's bundled
under `templates/project-scaffold/` and (re)installed by `story-automator setup`,
so a BMAD reinstall that wipes `.claude/skills/` can't permanently remove it;
re-run `setup` and it's back.

## Adding version-specific profiles (deferred)

If a future BMAD release ever needs a *different* mapping than an older one that
you must still support, promote `skillCandidates` to version-keyed profiles keyed
off the BMAD version string (`bmadPaths.config` → `# Version:`). Not built yet —
one candidate list covers 6.6–6.10, so it would be speculative today.
