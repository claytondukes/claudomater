"""Config loaders: per-project `.omater.yaml` and per-user `~/.omater/config.yaml`.

Project config is committed to the consumer repo so pipeline behavior is
reviewable and versioned like code. User config carries account facts
(thresholds, degrade paths, webhooks) that never belong in a shared repo.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PROJECT_CONFIG_NAME = ".omater.yaml"
USER_CONFIG_PATH = Path("~/.omater/config.yaml")

DEPLOYMENT_TYPES = ("sandbox", "internal", "production", "mission-critical")
FORGES = ("github", "bitbucket")
MODEL_ROLES = ("orchestrator", "create", "dev", "sr_review", "merge", "escalation")

MODEL_FABLE = "claude-fable-5"
MODEL_OPUS = "claude-opus-5"
MODEL_SONNET = "claude-sonnet-5"
SKIP = "skip"  # sentinel model value: the phase does not run at this deployment type

# Model families ranked by tier; degrading must move strictly DOWN this order.
_FAMILY_RANK = (("fable", 4), ("opus", 3), ("sonnet", 2), ("haiku", 1))


def family_rank(model: str) -> int:
    lowered = model.lower()
    for family, rank in _FAMILY_RANK:
        if family in lowered:
            return rank
    return 0

# What `deployment_type` controls (defaults, all overridable per role/knob).
# The escalation role is deployment-type-independent: a story with failure
# history always gets the strongest available model.
DEPLOYMENT_POLICY: dict[str, dict[str, Any]] = {
    "sandbox": {
        "models": {
            "orchestrator": MODEL_SONNET,
            "create": MODEL_SONNET,
            "dev": MODEL_SONNET,
            "sr_review": SKIP,
            "merge": MODEL_SONNET,
            "escalation": MODEL_FABLE,
        },
        "review_floor": "CRITICAL",
        "red_green": "no",
        "ci_on_push": "lint+type",
        "qa_board": "no",
        "close_pass": "no",
    },
    "internal": {
        "models": {
            "orchestrator": MODEL_OPUS,
            "create": MODEL_OPUS,
            "dev": MODEL_OPUS,
            "sr_review": MODEL_OPUS,
            "merge": MODEL_OPUS,
            "escalation": MODEL_FABLE,
        },
        "review_floor": "MUST-FIX",
        "red_green": "behavioral",
        "ci_on_push": "fast",
        "qa_board": "no",
        "close_pass": "lessons",
    },
    "production": {
        "models": {
            "orchestrator": MODEL_FABLE,
            "create": MODEL_FABLE,
            "dev": MODEL_OPUS,
            "sr_review": MODEL_FABLE,
            "merge": MODEL_FABLE,
            "escalation": MODEL_FABLE,
        },
        "review_floor": "SHOULD-FIX",
        "red_green": "yes",
        "ci_on_push": "fast",
        "qa_board": "optional",
        "close_pass": "lessons",
    },
    "mission-critical": {
        "models": {
            "orchestrator": MODEL_FABLE,
            "create": MODEL_FABLE,
            "dev": MODEL_OPUS,
            "sr_review": MODEL_FABLE,
            "merge": MODEL_FABLE,
            "escalation": MODEL_FABLE,
        },
        "review_floor": "NOTE",
        "red_green": "yes+mutation",
        "ci_on_push": "fast+smoke",
        "qa_board": "required",
        "close_pass": "lessons+close-gate-audit+blind-post-merge-review",
    },
}


class ConfigError(Exception):
    """A config file is missing, unparsable, or fails validation."""


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid yaml in {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return data


_ENV_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def expand_env(value: Any) -> Any:
    """Expand `${VAR}` string values from the environment; unset -> None."""
    if isinstance(value, str):
        m = _ENV_REF.match(value.strip())
        if m:
            return os.environ.get(m.group(1)) or None
        return value
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    return value


@dataclass
class MergeConfig:
    converge: str = "required"  # required | off
    reviewer: str = "copilot"  # copilot | agent (copilot is GitHub-only)


@dataclass
class ProjectConfig:
    project: str
    deployment_type: str = "sandbox"
    forge: str = "github"
    models: dict[str, str] = field(default_factory=dict)  # explicit overrides only
    merge: MergeConfig = field(default_factory=MergeConfig)
    secrets_deny: list[str] = field(default_factory=list)
    adapters: dict[str, str | None] = field(
        default_factory=lambda: {"issue_tracker": None, "qa_board": None}
    )
    learning_scopes: list[str] = field(default_factory=lambda: ["global"])
    ci_tier_on_push: str | None = None  # None -> deployment_type default
    ci_tier_on_merge: str = "full"
    gates: dict[str, Any] = field(default_factory=dict)
    root: Path | None = None

    def model_for(self, role: str) -> str:
        """Resolved model for a role: explicit override, else the deployment default."""
        if role not in MODEL_ROLES:
            raise ConfigError(f"unknown model role: {role}")
        if role in self.models:
            return self.models[role]
        return DEPLOYMENT_POLICY[self.deployment_type]["models"][role]

    def policy(self) -> dict[str, Any]:
        """The fully resolved policy — logged at run start so a changed
        deployment_type is *visible* (model chain, review floor, CI tier)."""
        base = DEPLOYMENT_POLICY[self.deployment_type]
        return {
            "project": self.project,
            "deployment_type": self.deployment_type,
            "forge": self.forge,
            "models": {role: self.model_for(role) for role in MODEL_ROLES},
            "review_floor": base["review_floor"],
            "red_green": base["red_green"],
            "ci_on_push": self.ci_tier_on_push or base["ci_on_push"],
            "ci_on_merge": self.ci_tier_on_merge,
            "qa_board": base["qa_board"],
            "close_pass": base["close_pass"],
            "merge": {"converge": self.merge.converge, "reviewer": self.merge.reviewer},
        }


def load_project_config(root: Path | str) -> ProjectConfig:
    root = Path(root)
    data = _load_yaml(root / PROJECT_CONFIG_NAME)

    project = data.get("project")
    if not project or not isinstance(project, str):
        raise ConfigError(f"{PROJECT_CONFIG_NAME}: 'project' (string) is required")

    deployment_type = data.get("deployment_type", "sandbox")
    if deployment_type not in DEPLOYMENT_TYPES:
        raise ConfigError(
            f"{PROJECT_CONFIG_NAME}: deployment_type must be one of {DEPLOYMENT_TYPES}, "
            f"got {deployment_type!r}"
        )

    forge = data.get("forge", "github")
    if forge not in FORGES:
        raise ConfigError(
            f"{PROJECT_CONFIG_NAME}: forge must be one of {FORGES}, got {forge!r}"
        )

    models = _require_mapping("models", data.get("models"))
    for role, value in models.items():
        if role not in MODEL_ROLES:
            raise ConfigError(
                f"{PROJECT_CONFIG_NAME}: unknown model role {role!r} "
                f"(known: {MODEL_ROLES})"
            )
        if not isinstance(value, str) or not value:
            raise ConfigError(
                f"{PROJECT_CONFIG_NAME}: models.{role} must be a model name string"
            )

    merge_raw = _require_mapping("merge", data.get("merge"))
    merge = MergeConfig(
        converge=merge_raw.get("converge", "required"),
        reviewer=merge_raw.get("reviewer", "copilot"),
    )
    if merge.converge not in ("required", "off"):
        raise ConfigError(
            f"{PROJECT_CONFIG_NAME}: merge.converge must be 'required' or 'off'"
        )
    if merge.reviewer not in ("copilot", "agent"):
        raise ConfigError(
            f"{PROJECT_CONFIG_NAME}: merge.reviewer must be 'copilot' or 'agent'"
        )
    if merge.reviewer == "copilot" and forge != "github":
        raise ConfigError(
            f"{PROJECT_CONFIG_NAME}: merge.reviewer 'copilot' is GitHub-only; "
            f"forge is {forge!r} — use reviewer: agent or converge: off"
        )

    ci_raw = _require_mapping("ci", data.get("ci"))
    learning_raw = _require_mapping("learning", data.get("learning"))
    adapters_raw = _require_mapping("adapters", data.get("adapters"))

    secrets_deny = data.get("secrets_deny") or []
    if not isinstance(secrets_deny, list) or not all(
        isinstance(s, str) for s in secrets_deny
    ):
        raise ConfigError(f"{PROJECT_CONFIG_NAME}: secrets_deny must be a list of names")

    return ProjectConfig(
        project=project,
        deployment_type=deployment_type,
        forge=forge,
        models=dict(models),
        merge=merge,
        secrets_deny=list(secrets_deny),
        adapters={
            "issue_tracker": adapters_raw.get("issue_tracker"),
            "qa_board": adapters_raw.get("qa_board"),
        },
        learning_scopes=list(learning_raw.get("scopes") or ["global"]),
        ci_tier_on_push=ci_raw.get("tier_on_push"),
        ci_tier_on_merge=ci_raw.get("tier_on_merge", "full"),
        gates=_require_mapping("gates", data.get("gates")),
        root=root,
    )


@dataclass
class UsageConfig:
    pause_at: dict[str, int] = field(
        default_factory=lambda: {"five_hour": 95, "seven_day": 95}
    )
    on_threshold: dict[str, str] = field(
        default_factory=lambda: {"five_hour": "pause", "seven_day": "pause"}
    )
    degrade_scoped_at: int = 80
    degrade_path: list[str] = field(default_factory=lambda: [MODEL_OPUS, "pause"])
    max_stale_seconds: int = 300  # fail closed beyond this


def _validate_degrade_path(path: list[str]) -> None:
    """A degrade path that can't work must fail at config load, not by
    degrading a live run into a nonexistent model at 2am."""
    if not path:
        raise ConfigError("usage.degrade_path must not be empty")
    for i, entry in enumerate(path):
        if entry == "pause":
            if i != len(path) - 1:
                raise ConfigError(
                    "usage.degrade_path: 'pause' must be the last entry"
                )
            continue
        if not isinstance(entry, str) or family_rank(entry) == 0:
            raise ConfigError(
                f"usage.degrade_path: unrecognized model {entry!r} "
                "(recognizable families: fable, opus, sonnet, haiku)"
            )
    ranks = [family_rank(e) for e in path if e != "pause"]
    if ranks != sorted(ranks, reverse=True) or len(set(ranks)) != len(ranks):
        raise ConfigError(
            "usage.degrade_path must step strictly DOWN the tiers "
            "(e.g. [claude-opus-5, claude-sonnet-5])"
        )


@dataclass
class UserConfig:
    usage: UsageConfig = field(default_factory=UsageConfig)
    slack_webhook: str | None = None
    learning_db_path: Path = Path("~/.omater/learning.db")
    learning_export_path: Path = Path("~/.dotfiles/omater/lessons")

    @property
    def notify_enabled(self) -> bool:
        return bool(self.slack_webhook)


def _require_mapping(name: str, value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a mapping, got {type(value).__name__}")
    return value


def _require_int(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name} must be an integer, got {value!r}")
    return value


def load_user_config(path: Path | str | None = None) -> UserConfig:
    """Load `~/.omater/config.yaml`; a missing file yields spec defaults."""
    cfg_path = Path(path) if path else USER_CONFIG_PATH.expanduser()
    if not cfg_path.exists():
        return UserConfig()
    data = expand_env(_load_yaml(cfg_path))

    usage_raw = _require_mapping("usage", data.get("usage"))
    usage = UsageConfig()
    if "pause_at" in usage_raw:
        usage.pause_at.update(_require_mapping("usage.pause_at", usage_raw["pause_at"]))
    if "on_threshold" in usage_raw:
        usage.on_threshold.update(
            _require_mapping("usage.on_threshold", usage_raw["on_threshold"])
        )
    usage.degrade_scoped_at = _require_int(
        "usage.degrade_scoped_at", usage_raw.get("degrade_scoped_at", 80)
    )
    if "degrade_path" in usage_raw:
        raw_path = usage_raw["degrade_path"]
        if not isinstance(raw_path, list):
            raise ConfigError("usage.degrade_path must be a list")
        usage.degrade_path = list(raw_path)
    usage.max_stale_seconds = _require_int(
        "usage.max_stale_seconds", usage_raw.get("max_stale_seconds", 300)
    )

    for window, pct in usage.pause_at.items():
        if window not in ("five_hour", "seven_day"):
            raise ConfigError(f"usage.pause_at: unknown window {window!r}")
        if isinstance(pct, bool) or not isinstance(pct, int) or not 0 <= pct <= 100:
            raise ConfigError(f"usage.pause_at.{window}: must be an int 0-100")
    for window, action in usage.on_threshold.items():
        if window not in ("five_hour", "seven_day"):
            raise ConfigError(f"usage.on_threshold: unknown window {window!r}")
        if action not in ("pause", "degrade"):
            raise ConfigError(
                f"usage.on_threshold.{window}: must be 'pause' or 'degrade'"
            )
    _validate_degrade_path(usage.degrade_path)

    notify_raw = _require_mapping("notify", data.get("notify"))
    learning_raw = _require_mapping("learning", data.get("learning"))

    cfg = UserConfig(usage=usage, slack_webhook=notify_raw.get("slack_webhook"))
    if learning_raw.get("db_path"):
        cfg.learning_db_path = Path(learning_raw["db_path"])
    if learning_raw.get("export_path"):
        cfg.learning_export_path = Path(learning_raw["export_path"])
    return cfg
