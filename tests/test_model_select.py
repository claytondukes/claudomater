from __future__ import annotations

import json
from pathlib import Path

from story_automator.core import model_select as ms


def _write_usage_cache(path: Path, *, percent: float, severity: str = "ok", name: str = "Fable") -> str:
    payload = {
        "limits": [
            {"kind": "session", "percent": 5, "severity": "ok"},
            {
                "kind": "weekly_scoped",
                "percent": percent,
                "severity": severity,
                "scope": {"model": {"display_name": name}},
            },
        ]
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _write_agents(tmp_path: Path, stories: list[dict]) -> Path:
    agents = tmp_path / "agents.md"
    payload = {"version": "1.0.0", "stories": stories}
    agents.write_text("---\nfoo: bar\n---\n\n```json\n" + json.dumps(payload) + "\n```\n", encoding="utf-8")
    return agents


def _write_state(tmp_path: Path, agents_file: Path) -> Path:
    state = tmp_path / "state.md"
    state.write_text(f"---\nagentsFile: {agents_file}\n---\n\nbody\n", encoding="utf-8")
    return state


def _cfg(cache_path: str) -> dict:
    # Mirror the bundled policy defaults but point at a test cache.
    return {
        "usageCachePath": cache_path,
        "defaultComplexity": "medium",
        "reviewSteps": ["review", "retro"],
        "tiers": {
            "normal": {"low": ms.SONNET, "medium": ms.OPUS, "high": ms.FABLE, "review": ms.FABLE},
            "fableExhausted": {"low": ms.SONNET, "medium": ms.SONNET, "high": ms.OPUS, "review": ms.OPUS},
        },
        "fableFallback": {"scopedModelName": "Fable", "thresholdPercent": 95},
    }


# --- fable_available -------------------------------------------------------

def test_fable_available_thresholds(tmp_path):
    cfg_ok = _cfg(_write_usage_cache(tmp_path / "u1.json", percent=50))
    cfg_edge = _cfg(_write_usage_cache(tmp_path / "u2.json", percent=95))
    cfg_crit = _cfg(_write_usage_cache(tmp_path / "u3.json", percent=10, severity="critical"))
    assert ms.fable_available(cfg_ok)[0] is True
    assert ms.fable_available(cfg_edge)[0] is False
    assert ms.fable_available(cfg_crit)[0] is False


def test_fable_available_fail_open(tmp_path):
    # Missing cache -> available (Fable-by-default intent).
    assert ms.fable_available(_cfg(str(tmp_path / "nope.json")))[0] is True
    # A scoped limit for a different model must not affect Fable.
    other = _write_usage_cache(tmp_path / "u.json", percent=100, severity="critical", name="Opus")
    assert ms.fable_available(_cfg(other))[0] is True


# --- select_model: normal (Fable available) --------------------------------

def test_select_model_normal_by_complexity(tmp_path):
    cache = _write_usage_cache(tmp_path / "u.json", percent=20)
    agents = _write_agents(
        tmp_path,
        [
            {"storyId": "1.1", "complexity": "low"},
            {"storyId": "1.2", "complexity": "medium"},
            {"storyId": "1.3", "complexity": "high"},
        ],
    )
    state = _write_state(tmp_path, agents)
    cfg = _cfg(cache)
    assert ms.select_model("dev", "1.1", str(state), cfg) == ms.SONNET
    assert ms.select_model("dev", "1.2", str(state), cfg) == ms.OPUS
    assert ms.select_model("create", "1.3", str(state), cfg) == ms.FABLE
    assert ms.select_model("auto", "1.3", str(state), cfg) == ms.FABLE


def test_select_model_review_always_top(tmp_path):
    cache = _write_usage_cache(tmp_path / "u.json", percent=20)
    agents = _write_agents(tmp_path, [{"storyId": "1.1", "complexity": "low"}])
    state = _write_state(tmp_path, agents)
    cfg = _cfg(cache)
    # Even a low-complexity story reviews on the top tier.
    assert ms.select_model("review", "1.1", str(state), cfg) == ms.FABLE
    assert ms.select_model("retro", "1", str(state), cfg) == ms.FABLE


# --- select_model: Fable exhausted -----------------------------------------

def test_select_model_fable_exhausted(tmp_path):
    cache = _write_usage_cache(tmp_path / "u.json", percent=100, severity="critical")
    agents = _write_agents(
        tmp_path,
        [
            {"storyId": "1.1", "complexity": "low"},
            {"storyId": "1.2", "complexity": "medium"},
            {"storyId": "1.3", "complexity": "high"},
        ],
    )
    state = _write_state(tmp_path, agents)
    cfg = _cfg(cache)
    assert ms.select_model("dev", "1.1", str(state), cfg) == ms.SONNET
    assert ms.select_model("dev", "1.2", str(state), cfg) == ms.SONNET  # medium -> Sonnet in fallback
    assert ms.select_model("dev", "1.3", str(state), cfg) == ms.OPUS   # high -> Opus in fallback
    assert ms.select_model("review", "1.3", str(state), cfg) == ms.OPUS


# --- select_model: complexity resolution edge cases ------------------------

def test_select_model_unresolved_complexity_uses_default(tmp_path):
    cache = _write_usage_cache(tmp_path / "u.json", percent=20)
    agents = _write_agents(tmp_path, [{"storyId": "9.9", "complexity": "high"}])
    state = _write_state(tmp_path, agents)
    cfg = _cfg(cache)
    notes: list[str] = []
    # Story not in agents file -> default complexity 'medium' -> Opus, with a warn note.
    model = ms.select_model("dev", "1.1", str(state), cfg, warn=notes.append)
    assert model == ms.OPUS
    assert any("complexity unresolved" in n for n in notes)


def test_select_model_defaults_without_config(tmp_path):
    # No config block at all -> in-code defaults; missing cache -> Fable available.
    agents = _write_agents(tmp_path, [{"storyId": "1.3", "complexity": "high"}])
    state = _write_state(tmp_path, agents)
    # Point HOME-less default cache path away by relying on fail-open: the real
    # cache may or may not exist, so assert only the complexity mapping when the
    # explicit high story resolves. Use an isolated config that fails open.
    cfg = {"usageCachePath": str(tmp_path / "absent.json")}
    assert ms.select_model("dev", "1.3", str(state), cfg) == ms.FABLE
    assert ms.select_model("dev", "1.1", str(state), cfg) == ms.OPUS  # unknown story -> default medium
