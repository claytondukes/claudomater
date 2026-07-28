# Model Selection (v1.0.0)

The orchestrator picks the coding **model** per step automatically inside
`tmux-wrapper build-cmd` — no orchestrator action or doc load is required at
runtime. Selection combines two axes: **story complexity** and **live Fable
usage**.

Implementation: `src/story_automator/core/model_select.py`
Config: the `models` block in `data/orchestration-policy.json` (frozen into each
run's policy snapshot). Defaults also live in code, so the block is optional.

---

## Tiers

| Complexity | Model (normal) | Model (Fable exhausted) |
|---|---|---|
| **high** (≥8) | Fable (`claude-fable-5`) | Opus (`claude-opus-4-8`) |
| **medium** (4–7) | Opus (`claude-opus-4-8`) | Sonnet (`claude-sonnet-5`) |
| **low** (≤3) | Sonnet (`claude-sonnet-5`) | Sonnet (`claude-sonnet-5`) |
| **review / retro** (always) | Fable (`claude-fable-5`) | Opus (`claude-opus-4-8`) |

- **`create` / `dev` / `auto`** follow the story's complexity level (from
  `parse-story --rules`, stored per story in the agents file).
- **`review` / `retro`** are the quality gate and always run top-tier,
  regardless of story complexity.
- The **parser** sub-agent (session output parsing) is unaffected and stays on
  `claude-haiku-4-5-20251001` (`runtime.parser.model`).

If a story's complexity cannot be resolved (agents file missing, story not
listed), selection falls back to `defaultComplexity` (`medium`) and emits a note
to **stderr** (never stdout, which carries the spawn command).

---

## Fable usage detection & fallback

Claude Code's OAuth usage feed (the same source as `/usage`) is cached by the
status line at `~/.cache/claude-statusline/usage.json`, refreshed every ~60s.
It carries a model-scoped weekly limit:

```json
{"kind":"weekly_scoped","percent":100,"severity":"critical",
 "scope":{"model":{"display_name":"Fable"}}}
```

`build-cmd` reads this **live on every spawn**. Fable is treated as **exhausted**
when `percent >= 95` **or** `severity == "critical"`, at which point the whole
tier table shifts down (see the second column above) and a note is logged to
stderr. A run that burns through Fable mid-flight downgrades automatically on the
next spawn.

**Fail-open:** if the cache is missing, unreadable, or has no Fable-scoped entry,
Fable is treated as **available** (Fable-by-default is the intent). Downgrades
happen only on positive evidence of exhaustion.

> The status line's `weekly_scoped` entry is the sole feed for Fable. Opus and
> Sonnet have no dedicated scoped cap today — they draw from the shared
> `weekly_all` (7d) pool — so they are not gated here.

---

## Configuration (`models` block)

```json
"models": {
  "usageCachePath": "~/.cache/claude-statusline/usage.json",
  "defaultComplexity": "medium",
  "reviewSteps": ["review", "retro"],
  "tiers": {
    "normal":        {"low": "claude-sonnet-5", "medium": "claude-opus-4-8", "high": "claude-fable-5", "review": "claude-fable-5"},
    "fableExhausted":{"low": "claude-sonnet-5", "medium": "claude-sonnet-5", "high": "claude-opus-4-8", "review": "claude-opus-4-8"}
  },
  "fableFallback": {"scopedModelName": "Fable", "thresholdPercent": 95}
}
```

**To change model versions:** edit `models.tiers` here (and `runtime.parser.model`
for the parser). To adjust the Fable cutoff, edit `fableFallback.thresholdPercent`.
Any subset may be omitted; missing values fall back to the in-code defaults in
`model_select.py`.

**Note on manual overrides:** setting `AI_COMMAND` (legacy manual CLI override)
bypasses model selection entirely — the command runs verbatim. Codex spawns are
also unaffected (Codex uses its own CLI and reasoning-effort settings).
