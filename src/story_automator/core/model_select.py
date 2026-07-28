"""Complexity- and usage-aware model tier selection.

The orchestrator picks the coding model per step:

* ``review`` / ``retro`` are the quality gate and always run on the top tier
  (Fable, or Opus when Fable is exhausted).
* ``create`` / ``dev`` / ``auto`` follow the story's computed complexity level:
  ``high`` -> Fable, ``medium`` -> Opus, ``low`` -> Sonnet.

When Fable's model-scoped weekly limit is exhausted the whole table shifts down
one tier (``high`` -> Opus, ``medium``/``low`` -> Sonnet, review -> Opus). Fable
exhaustion is read live from the OAuth usage cache that ``statusline.sh`` keeps
fresh at ``~/.cache/claude-statusline/usage.json`` (the same feed as ``/usage``);
the ``weekly_scoped`` limit entry carries the Fable ``percent`` and ``severity``.

Defaults live here as the single in-code source of truth. The bundled policy's
optional ``models`` block (``data/orchestration-policy.json``) overrides them and
is frozen into each run's policy snapshot; the *live* Fable check still happens
per-spawn so a run that burns through Fable mid-flight downgrades automatically.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Callable

from .common import file_exists, read_text
from .frontmatter import find_frontmatter_value

# --- Default model IDs (current versions) --------------------------------
# Update these when model versions change; the policy `models` block can also
# override any of them without touching code.
FABLE = "claude-fable-5"
OPUS = "claude-opus-4-8"
SONNET = "claude-sonnet-5"

DEFAULT_USAGE_CACHE = "~/.cache/claude-statusline/usage.json"
DEFAULT_FABLE_SCOPE_NAME = "Fable"
DEFAULT_FABLE_THRESHOLD = 95
DEFAULT_COMPLEXITY = "medium"
REVIEW_STEPS = ("review", "retro")
_LEVELS = ("low", "medium", "high")

# Tier tables keyed by complexity level plus a "review" slot for review/retro.
_NORMAL_TIERS: dict[str, str] = {"low": SONNET, "medium": OPUS, "high": FABLE, "review": FABLE}
_FABLE_OUT_TIERS: dict[str, str] = {"low": SONNET, "medium": SONNET, "high": OPUS, "review": OPUS}


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _cfg_str(config: Any, key: str, default: str) -> str:
    value = _as_dict(config).get(key)
    return value.strip() if isinstance(value, str) and value.strip() else default


def _fallback_cfg(config: Any) -> dict[str, Any]:
    return _as_dict(_as_dict(config).get("fableFallback"))


def _review_steps(config: Any) -> tuple[str, ...]:
    raw = _as_dict(config).get("reviewSteps")
    if isinstance(raw, list):
        steps = tuple(s.strip().lower() for s in raw if isinstance(s, str) and s.strip())
        if steps:
            return steps
    return REVIEW_STEPS


def _tier_tables(config: Any) -> tuple[dict[str, str], dict[str, str]]:
    normal = dict(_NORMAL_TIERS)
    exhausted = dict(_FABLE_OUT_TIERS)
    tiers = _as_dict(_as_dict(config).get("tiers"))
    for key, table in (("normal", normal), ("fableExhausted", exhausted)):
        override = _as_dict(tiers.get(key))
        table.update({k: v for k, v in override.items() if isinstance(v, str) and v.strip()})
    return normal, exhausted


def _default_complexity(config: Any) -> str:
    level = _cfg_str(config, "defaultComplexity", DEFAULT_COMPLEXITY).lower()
    return level if level in _LEVELS else DEFAULT_COMPLEXITY


def fable_available(config: Any = None, *, usage_cache_path: str | None = None) -> tuple[bool, str]:
    """Return ``(available, note)`` for the Fable tier.

    Fail-open: when the usage cache is missing/unreadable or carries no Fable
    ``weekly_scoped`` entry, Fable is treated as available (Fable-by-default is
    the intent). Downgrade only on positive evidence of exhaustion.
    """
    path = usage_cache_path or _cfg_str(config, "usageCachePath", DEFAULT_USAGE_CACHE)
    scope_name = _cfg_str(_fallback_cfg(config), "scopedModelName", DEFAULT_FABLE_SCOPE_NAME)
    threshold_raw = _fallback_cfg(config).get("thresholdPercent")
    threshold = threshold_raw if isinstance(threshold_raw, (int, float)) and not isinstance(threshold_raw, bool) else DEFAULT_FABLE_THRESHOLD
    expanded = os.path.expanduser(str(path))
    if not file_exists(expanded):
        return True, ""
    try:
        data = json.loads(read_text(expanded))
    except (OSError, ValueError):
        return True, ""
    limits = data.get("limits") if isinstance(data, dict) else None
    if not isinstance(limits, list):
        return True, ""
    for limit in limits:
        if not isinstance(limit, dict) or limit.get("kind") != "weekly_scoped":
            continue
        model_name = str(((_as_dict(_as_dict(limit.get("scope")).get("model"))).get("display_name")) or "").strip()
        if scope_name and model_name.lower() != scope_name.lower():
            continue
        severity = str(limit.get("severity") or "").strip().lower()
        try:
            percent = float(limit.get("percent"))
        except (TypeError, ValueError):
            percent = 0.0
        if severity == "critical" or percent >= threshold:
            return False, f"{model_name or scope_name} weekly usage {int(percent)}% (severity={severity or 'n/a'}, threshold={int(threshold)}%)"
        return True, ""
    return True, ""


def story_complexity(state_file: str, story_id: str) -> str:
    """Best-effort complexity level for ``story_id`` from the run's agents file.

    Returns ``""`` when it cannot be resolved (missing state/agents file, story
    not listed, malformed JSON) so callers can apply their own default.
    """
    if not state_file or not story_id:
        return ""
    try:
        agents_path = find_frontmatter_value(state_file, "agentsFile")
    except OSError:
        return ""
    if not agents_path or not file_exists(agents_path):
        return ""
    try:
        text = read_text(agents_path)
    except OSError:
        return ""
    match = re.search(r"(?s)```json\s*(\{.*?\})\s*```", text)
    block = match.group(1) if match else text.strip()
    try:
        payload = json.loads(block)
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""
    for story in payload.get("stories", []) or []:
        if isinstance(story, dict) and str(story.get("storyId")) == str(story_id):
            return str(story.get("complexity") or "").strip().lower()
    return ""


def select_model(
    step: str,
    story_id: str,
    state_file: str,
    config: Any = None,
    *,
    warn: Callable[[str], None] | None = None,
) -> str:
    """Resolve the model id for ``step`` on ``story_id``.

    ``config`` is the policy ``models`` block (or ``None`` to use defaults).
    ``warn`` receives human-readable notes (unresolved complexity, Fable
    downgrade) — the caller routes them to stderr, never stdout.
    """
    def emit(message: str) -> None:
        if warn:
            warn(message)

    step_norm = (step or "").strip().lower()
    normal, exhausted = _tier_tables(config)
    available, note = fable_available(config)
    table = normal if available else exhausted

    if step_norm in _review_steps(config):
        tier_key = "review"
    else:
        level = story_complexity(state_file, story_id)
        if level not in _LEVELS:
            level = _default_complexity(config)
            if story_id:
                emit(f"[model-select] complexity unresolved for story {story_id}; using default '{level}'")
        tier_key = level

    model = table.get(tier_key) or normal.get(tier_key) or OPUS
    if not available and note:
        emit(f"[model-select] Fable over limit ({note}); {step_norm or 'step'} tier '{tier_key}' -> {model}")
    return model
