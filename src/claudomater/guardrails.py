"""Usage guardrails: turn a usage snapshot into an ok / pause / degrade decision.

Rules (from the design, §5):

- Unknown usage (no credentials, fetch failed, stale cache) = over-threshold
  = PAUSE. Fail closed, never run blind.
- Window thresholds are account-global: a pause pauses ALL runs on the
  account. Per-window behavior (`pause` | `degrade`) comes from user config.
- Scoped (top-tier) quota >= `degrade_scoped_at` follows the degrade path:
  ride the chain down, or step down once then pause for the user.
- An ESCALATED story (failure history) never silently runs on a degraded
  model: if its required tier is unavailable, that story pauses.
- An account switch re-baselines the guardrails immediately.
- In-flight phases finish; no NEW phase spawns after a threshold trips —
  callers gate phase *spawns* on the decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from claudomater.config import SKIP, UserConfig, family_rank
from claudomater.usage import UsageSnapshot, UsageUnavailable

PAUSE = "pause"
DEGRADE = "degrade"
OK = "ok"

WINDOW_LABELS = {"five_hour": "5h", "seven_day": "7d", "scoped": "scoped"}


@dataclass
class Decision:
    action: str  # ok | pause | degrade
    reasons: list[str] = field(default_factory=list)
    window: str | None = None  # five_hour | seven_day | scoped
    resets_at: str | None = None
    rebaselined: bool = False
    snapshot: UsageSnapshot | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "reasons": self.reasons,
            "window": self.window,
            "resets_at": self.resets_at,
            "rebaselined": self.rebaselined,
            "usage": self.snapshot.as_dict() if self.snapshot else None,
        }


def evaluate(
    snapshot: UsageSnapshot | UsageUnavailable | None,
    cfg: UserConfig,
    baseline_account: dict[str, str] | None = None,
) -> Decision:
    """Evaluate one guardrail read. Pass the UsageUnavailable exception (or
    None) as the snapshot to get the fail-closed pause."""
    if snapshot is None or isinstance(snapshot, UsageUnavailable):
        reason = str(snapshot) if snapshot else "no usage data"
        return Decision(
            action=PAUSE,
            reasons=[f"usage unknown, failing closed: {reason}"],
            window=None,
        )

    rebaselined = bool(
        baseline_account
        and snapshot.account
        and baseline_account != snapshot.account
    )

    decision = Decision(action=OK, rebaselined=rebaselined, snapshot=snapshot)
    if rebaselined:
        decision.reasons.append(
            f"account switch detected ({baseline_account} -> {snapshot.account}); "
            "guardrails re-baselined"
        )

    # Window thresholds. Pause dominates degrade if both windows trip.
    tripped: list[tuple[str, str, float, str | None]] = []
    for window, pct in (("five_hour", snapshot.five_hour), ("seven_day", snapshot.seven_day)):
        if pct is None:
            return Decision(
                action=PAUSE,
                reasons=[
                    f"usage unknown, failing closed: {window} window missing from snapshot"
                ],
                rebaselined=rebaselined,
                snapshot=snapshot,
            )
        threshold = cfg.usage.pause_at[window]
        if pct >= threshold:
            action = cfg.usage.on_threshold[window]
            resets = (
                snapshot.five_hour_resets_at
                if window == "five_hour"
                else snapshot.seven_day_resets_at
            )
            tripped.append((window, action, pct, resets))

    for window, action, pct, resets in tripped:
        decision.reasons.append(
            f"{WINDOW_LABELS[window]} window at {pct:.0f}% "
            f">= {cfg.usage.pause_at[window]}% -> {action}"
        )
        if action == PAUSE and decision.action != PAUSE:
            decision.action, decision.window, decision.resets_at = PAUSE, window, resets
        elif action == DEGRADE and decision.action == OK:
            decision.action, decision.window, decision.resets_at = DEGRADE, window, resets

    # Scoped (top-tier) quota: degrade path.
    if (
        decision.action == OK
        and snapshot.scoped is not None
        and snapshot.scoped >= cfg.usage.degrade_scoped_at
    ):
        decision.action = DEGRADE
        decision.window = "scoped"
        decision.resets_at = snapshot.scoped_resets_at
        decision.reasons.append(
            f"scoped ({snapshot.scoped_model or 'top-tier'}) quota at "
            f"{snapshot.scoped:.0f}% >= {cfg.usage.degrade_scoped_at}% -> degrade"
        )

    return decision


def next_model(current: str, degrade_path: list[str]) -> str:
    """Walk the degrade path from `current`. Returns the next model name,
    the literal 'pause' (end of the path), or `current` unchanged when the
    path holds no strictly lower tier for it — degrading must never step a
    model UP the chain (a sonnet phase does not become opus)."""
    if current in degrade_path:
        idx = degrade_path.index(current)
        if idx + 1 < len(degrade_path):
            return degrade_path[idx + 1]
        return PAUSE
    current_rank = family_rank(current)
    for entry in degrade_path:
        if entry == PAUSE:
            continue
        if family_rank(entry) < current_rank:
            return entry
    return current


def scope_applies(required_model: str, scoped_model: str | None, degrade_path: list[str]) -> bool:
    """Does a scoped-quota trip constrain `required_model`? The scoped limit
    names its model (e.g. 'Fable'); only phases requiring that family
    degrade — an opus phase keeps working when only Fable quota is burned.

    Family rank is the primary match (so a display-name change like
    'Fable' -> 'Fable 5' cannot silently disarm the scoped guardrail);
    substring is the fallback for unrecognized names, and with no scope name
    at all only tiers above the path's first fallback are treated as scoped."""
    if scoped_model:
        s_rank, r_rank = family_rank(scoped_model), family_rank(required_model)
        if s_rank and r_rank:
            return s_rank == r_rank
        return scoped_model.lower().replace(" ", "") in required_model.lower()
    real_steps = [m for m in degrade_path if m != PAUSE]
    if not real_steps:
        return True
    return family_rank(required_model) > family_rank(real_steps[0])


def model_for_phase(
    required_model: str,
    decision: Decision,
    cfg: UserConfig,
    escalated: bool = False,
) -> tuple[str | None, str | None]:
    """Resolve the model a new phase may spawn with under `decision`.

    Returns (model, reason): model None = do not spawn (pause), with reason.
    Escalated stories never run degraded — if their required tier is
    unavailable they pause even when the degrade path would continue.
    """
    if decision.action == OK:
        return required_model, None
    if decision.action == PAUSE:
        return None, "; ".join(decision.reasons) or "paused by guardrails"
    # DEGRADE — first decide whether this phase's tier is affected AT ALL;
    # an unaffected tier keeps working, escalated or not (the escalation
    # rule pauses a story whose required tier is UNAVAILABLE, and an
    # untouched tier is fully available).
    if required_model == SKIP:
        return required_model, None
    if decision.window == "scoped":
        scoped_name = decision.snapshot.scoped_model if decision.snapshot else None
        if not scope_applies(required_model, scoped_name, cfg.usage.degrade_path):
            return required_model, None
    step = next_model(required_model, cfg.usage.degrade_path)
    if step == required_model:
        return required_model, None  # nothing lower to degrade to; keep working
    if escalated:
        return None, (
            "escalated story requires its full-tier model "
            f"({required_model}) and never runs degraded; pausing"
        )
    if step == PAUSE:
        return None, (
            f"degrade path exhausted for {required_model}; pausing for the user"
        )
    return step, (
        f"degraded {required_model} -> {step} ({'; '.join(decision.reasons)})"
    )
