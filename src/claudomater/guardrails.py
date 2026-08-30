"""Usage guardrails: turn a usage snapshot into an ok / pause / degrade decision.

Rules (from the design, §5):

- Unknown usage (no credentials, fetch failed, unreadable cache) =
  over-threshold = PAUSE. Fail closed, never run blind.
- STALE usage with a known last reading is not unknown (2026-08-30 order,
  after the Epic 9 incident): pause requires staleness AND a near-limit
  reading, where near-limit means the last reading projected forward at
  `STALE_DRIFT_PP_PER_MIN` reaches a pause threshold. The projection makes
  the rule self-capping — any reading pauses once it has been stale long
  enough. Degrades never act on stale data.
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

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from claudomater.config import SKIP, UserConfig, family_rank
from claudomater.runlog import RunError, RunLog
from claudomater.usage import (
    UsageSnapshot,
    UsageUnavailable,
    positive_identity,
    read_usage,
)

PAUSE = "pause"
DEGRADE = "degrade"
OK = "ok"

WINDOW_LABELS = {"five_hour": "5h", "seven_day": "7d", "scoped": "scoped"}

# How fast a window can plausibly climb while the reading is stale, in
# percentage points per minute. Phase 0.5 measured 83% -> 94% in a 24-minute
# full-tier story run (~0.46 pp/min); 0.5 rounds up. This is what makes the
# staleness rule self-capping: even a 0% last reading projects past a 95%
# threshold after ~190 stale minutes, so "proceed on stale" can never hold
# forever — no separate hard cap needed.
STALE_DRIFT_PP_PER_MIN = 0.5


def _reset_epoch_or_none(resets_at: str | None) -> float | None:
    """Epoch seconds of a window's recorded reset, or None when absent,
    unparseable, or timezone-naive. Same choke-point discipline as
    usage._num_or_none: garbage maps to None, and None keeps the
    CONSERVATIVE branch (drift the old reading forward, which can only
    pause earlier) — never a crash mid-guardrail, never a local-time
    guess at a naive timestamp."""
    if not isinstance(resets_at, str):
        return None
    try:
        dt = datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.timestamp()


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


def _stale_decision(
    exc: UsageUnavailable,
    cfg: UserConfig,
    baseline_account: dict[str, str] | None = None,
) -> Decision:
    """The staleness-AND-near-limit rule (2026-08-30 order). Staleness alone
    is not evidence of exhaustion — the Epic 9 run was paused at 17% real
    usage because the usage endpoint 429'd — so a stale-but-readable last
    reading pauses only when it PROJECTS to a pause threshold at
    STALE_DRIFT_PP_PER_MIN. A stale snapshot missing a pause window is
    unknown, not stale: fail closed. Degrades never act on stale data —
    degrading is a positive step that needs fresh numbers, so a stale scoped
    reading is ignored here and the worst case is running the top tier
    slightly past the soft degrade threshold until a refresh succeeds."""
    snap, age = exc.snapshot, exc.age_s
    if snap is None or age is None:
        return Decision(
            action=PAUSE,
            reasons=[f"usage unknown, failing closed: {exc}"],
        )
    # The stale path re-baselines exactly like the fresh path — the reading's
    # provenance IS its account, and a switch detected on a stale reading is
    # no less a switch. Skipping this let a run baselined to account A ride
    # account B's stale cache with rebaselined=False and no recorded reason.
    # positive_identity on the reading's side (F5): a placeholder identifies
    # NOBODY and cannot prove a switch happened.
    rebaselined = bool(
        baseline_account
        and positive_identity(snap.account)
        and baseline_account != snap.account
    )
    prefix: list[str] = []
    if rebaselined:
        prefix.append(
            f"account switch detected ({baseline_account} -> {snap.account}); "
            "guardrails re-baselined"
        )
    drift = STALE_DRIFT_PP_PER_MIN * (age / 60.0)
    # age was measured against fetched_at when the exception was raised, so
    # this IS the evaluation clock — no second wall-clock read to disagree.
    now = snap.fetched_at + age
    below: list[str] = []
    degrade_crossings: list[tuple[str, str | None]] = []
    for window, pct, resets in (
        ("five_hour", snap.five_hour, snap.five_hour_resets_at),
        ("seven_day", snap.seven_day, snap.seven_day_resets_at),
    ):
        if pct is None:
            return Decision(
                action=PAUSE,
                reasons=prefix
                + [
                    "usage unknown, failing closed: "
                    f"{window} window missing from the stale reading ({exc})"
                ],
                rebaselined=rebaselined,
                snapshot=snap,
            )
        threshold = cfg.usage.pause_at[window]
        reset_epoch = _reset_epoch_or_none(resets)
        if reset_epoch is not None and snap.fetched_at < reset_epoch <= now:
            # The reading predates its window's reset: that percentage
            # belongs to the EXPIRED window, and drifting it forward would
            # pause a window that has already restarted — a false deny, the
            # exact class this rule exists to avoid. The reset is a known
            # zero point strictly better than the pre-reset reading, so the
            # projection rebases from 0% there. (A stale interval spanning
            # several reset cycles rebases from the FIRST — over-projecting,
            # i.e. erring toward pause.) The reset must fall STRICTLY inside
            # the stale interval: a resets_at at or before fetched_at means
            # the reading was taken after that reset and is already the
            # current window's — rebasing on it would discard a valid high
            # reading for a lower from-zero projection and fail OPEN.
            projected = STALE_DRIFT_PP_PER_MIN * ((now - reset_epoch) / 60.0)
            reading = (
                f"{WINDOW_LABELS[window]} {pct:.0f}% pre-reset (window reset "
                f"at {resets}; projection rebased from 0% at the reset)"
            )
        else:
            projected = pct + drift
            reading = f"{WINDOW_LABELS[window]} {pct:.0f}%"
        if projected >= threshold:
            if cfg.usage.on_threshold[window] == PAUSE:
                return Decision(
                    action=PAUSE,
                    reasons=prefix
                    + [
                        f"stale usage ({int(age)}s old) with a near-limit last "
                        f"reading: {reading} projects to "
                        f"{projected:.0f}% (+{STALE_DRIFT_PP_PER_MIN} pp/min) "
                        f">= {threshold}% -> pause"
                    ],
                    window=window,
                    resets_at=resets,
                    rebaselined=rebaselined,
                    snapshot=snap,
                )
            # A degrade-configured window's crossing is OBSERVED, never acted
            # on: degrades need fresh numbers (the rule above), and pausing
            # here would be harder than the user configured for a soft
            # threshold — contrary to the fresh-path contract. Worst case is
            # running the configured tier slightly past the soft threshold
            # until a refresh succeeds. (Bounded by the hard stop below when
            # no pause window exists to self-cap.)
            degrade_crossings.append((window, resets))
            below.append(
                f"{reading} projects to "
                f"{projected:.0f}% >= {threshold}% but the window is "
                "degrade-configured and degrades never act on stale data"
            )
            continue
        below.append(
            f"{reading} projects to {projected:.0f}% < {threshold}%"
        )
    if degrade_crossings and not any(
        cfg.usage.on_threshold[w] == PAUSE for w in ("five_hour", "seven_day")
    ):
        # The stale HARD STOP: with every window degrade-configured (valid
        # config), no pause window exists to self-cap — the loop above would
        # return OK forever on stale data, since degrades never act on it.
        # A crossing that can neither degrade (stale) nor ever pause (no
        # pause window) must stop the run here.
        window, resets = degrade_crossings[0]
        return Decision(
            action=PAUSE,
            reasons=prefix
            + [
                f"stale usage ({int(age)}s old) crossed the degrade-configured "
                f"{WINDOW_LABELS[window]} threshold with NO pause-configured "
                "window to self-cap; degrades never act on stale data, so this "
                "is the stale hard stop: " + "; ".join(below)
            ],
            window=window,
            resets_at=resets,
            rebaselined=rebaselined,
            snapshot=snap,
        )
    return Decision(
        action=OK,
        reasons=prefix
        + [
            f"usage stale ({int(age)}s old; refresh failing) but the last "
            "reading is not near any pause threshold: "
            + "; ".join(below)
            + " — proceeding at degraded confidence"
        ],
        rebaselined=rebaselined,
        snapshot=snap,
    )


def evaluate(
    snapshot: UsageSnapshot | UsageUnavailable | None,
    cfg: UserConfig,
    baseline_account: dict[str, str] | None = None,
) -> Decision:
    """Evaluate one guardrail read. Pass the UsageUnavailable exception (or
    None) as the snapshot to get the fail-closed pause — unless the exception
    carries a stale-but-readable last reading, which gets the
    staleness-AND-near-limit rule instead."""
    if snapshot is None or isinstance(snapshot, UsageUnavailable):
        if isinstance(snapshot, UsageUnavailable) and snapshot.snapshot is not None:
            return _stale_decision(snapshot, cfg, baseline_account=baseline_account)
        reason = str(snapshot) if snapshot else "no usage data"
        return Decision(
            action=PAUSE,
            reasons=[f"usage unknown, failing closed: {reason}"],
            window=None,
        )

    # positive_identity, not truthiness (F5): {'unknown': 'true'} identifies
    # nobody — comparing it against the baseline would report an account
    # switch no one made.
    rebaselined = bool(
        baseline_account
        and positive_identity(snapshot.account)
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


def baseline_account_from_log(events: list[dict[str, Any]]) -> dict[str, str] | None:
    """Seed for the account baseline (parity finding F5): the account of the
    last positively-identified guardrail reading THIS RUN recorded. An
    in-memory-only baseline starts empty in every fresh process, so an
    account switch across a park/resume or crash-adoption boundary was
    handled safely but recorded as rebaselined=False — invisible in the
    post-mortem. The run log already carries the answer: walk back to the
    newest `guardrail-check` event whose snapshot account is a positive
    identity (a placeholder identifies nobody and cannot anchor a switch)."""
    for ev in reversed(events):
        if ev.get("event") != "guardrail-check":
            continue
        detail = ev.get("detail")
        if not isinstance(detail, dict):
            continue
        usage = detail.get("usage")
        if not isinstance(usage, dict):
            continue
        account = usage.get("account")
        if positive_identity(account):
            return account
    return None


def make_guardrail_check(
    cfg: UserConfig,
    runlog: RunLog | None = None,
    read: Callable[[], UsageSnapshot] | None = None,
) -> Callable[[], Decision]:
    """Build the spawn-gate callable PhaseRunner takes as `guardrail_check`.

    Owns the two pieces every driver was hand-rolling:
    - the read→evaluate plumbing: a UsageUnavailable raise is handed to
      `evaluate` as-is, so the staleness-AND-near-limit rule applies;
    - the account BASELINE: seeded from the run log's last recorded
      guardrail reading (F5) and advanced on every positively-identified
      reading, so an account switch is reported (rebaselined=True) exactly
      once — including across a park/resume boundary, where a process-local
      baseline forgot everything.

    `read` defaults to `read_usage` at the user config's staleness TTL;
    inject a fake for tests."""
    if read is None:

        def read() -> UsageSnapshot:
            return read_usage(max_stale=cfg.usage.max_stale_seconds)

    baseline: dict[str, str] | None = (
        baseline_account_from_log(runlog.events()) if runlog is not None else None
    )

    def check() -> Decision:
        nonlocal baseline
        try:
            snapshot: UsageSnapshot | UsageUnavailable = read()
        except UsageUnavailable as exc:
            snapshot = exc
        decision = evaluate(snapshot, cfg, baseline_account=baseline)
        snap = decision.snapshot
        if snap is not None and positive_identity(snap.account):
            baseline = snap.account
        return decision

    return check


@dataclass
class ParkWake:
    """What ended a `wait_for_unpark`: `outcome` is one of `capacity` (the
    spawn gate stopped pausing — window reset, account switch, or plain
    usage recovery), `resume` / `abort` (operator control), or `timeout`
    (max_wait exhausted; the run is STILL parked)."""

    outcome: str
    decision: Decision | None = None
    control: dict[str, Any] | None = None
    polls: int = 0
    waited_s: float = 0.0


def wait_for_unpark(
    runlog: RunLog,
    check: Callable[[], Decision],
    poll_interval_s: float = 300.0,
    max_wait_s: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> ParkWake:
    """The park-recovery loop (parity finding F6): before this, a parked run
    woke on exactly two signals — the clock or a human — and an account
    switch sat unnoticed until the operator pinged. Core stays passive (no
    daemon); this is the first-class loop the ORCHESTRATOR calls when a
    phase outcome is `paused`, instead of hand-rolling it:

    - polls `check` (build it with `make_guardrail_check`, so an account
      switch or a window reset reads as capacity) every `poll_interval_s`;
    - watches the control channel for an operator `resume` or `abort`
      written at-or-after the park (`abort` dominates when both landed);
      the caller acts on the returned control — this function only reports;
    - gives up after `max_wait_s` with outcome `timeout`, run still parked.

    The first gate poll happens immediately: the live Phase 1 park was
    resumable the moment the wait would have started (the operator had
    already switched accounts). Every exit writes a run-log event
    (`park-wake`, or `park-wait-timeout`), and entry is write-ahead
    (`park-wait`). `sleep`/`clock` are injectable for tests.

    Calling this on a run with NO `run-parked` event raises: an empty park
    timestamp would make every stale control eligible, waking (or aborting)
    on a command that answered an earlier state. A legitimate caller always
    has one — PhaseRunner parks before returning a paused outcome."""
    park_ts = ""
    for ev in reversed(runlog.events()):
        if ev.get("event") == "run-parked":
            park_ts = ev.get("ts", "")
            break
    if not park_ts:
        raise RunError(
            f"run {runlog.run_id} has no run-parked event (or a malformed "
            "one); wait_for_unpark is the resume loop for a PARKED run"
        )
    runlog.event(
        "run",
        "park-wait",
        {"poll_interval_s": poll_interval_s, "max_wait_s": max_wait_s},
    )
    start = clock()
    polls = 0

    def _pending_control() -> dict[str, Any] | None:
        # ts strings are _utc_now() ISO — lexicographically ordered. A
        # control from BEFORE the park was answered (or meant for) an
        # earlier state; only commands at-or-after the park are pending.
        pending = [
            c
            for c in runlog.read_controls()
            if c.get("action") in ("resume", "abort") and c.get("ts", "") >= park_ts
        ]
        for c in pending:
            if c["action"] == "abort":
                return c  # abort dominates: explicit operator stop
        return pending[-1] if pending else None

    while True:
        control = _pending_control()
        if control is not None:
            wake = ParkWake(
                outcome=control["action"],
                control=control,
                polls=polls,
                waited_s=clock() - start,
            )
            runlog.event(
                "run",
                "park-wake",
                {
                    "source": f"control-{control['action']}",
                    "polls": polls,
                    "waited_s": round(wake.waited_s, 1),
                },
            )
            return wake
        decision = check()
        polls += 1
        if decision.action != PAUSE:
            wake = ParkWake(
                outcome="capacity",
                decision=decision,
                polls=polls,
                waited_s=clock() - start,
            )
            runlog.event(
                "run",
                "park-wake",
                {
                    "source": "capacity",
                    "action": decision.action,
                    "rebaselined": decision.rebaselined,
                    "polls": polls,
                    "waited_s": round(wake.waited_s, 1),
                },
            )
            return wake
        waited = clock() - start
        if max_wait_s is not None:
            remaining = max_wait_s - waited
            if remaining <= 0:
                # timed out AFTER a poll at (or past) the deadline — the
                # documented max wait is honored, never cut a full interval
                # short, and the final control read happened
                runlog.event(
                    "run",
                    "park-wait-timeout",
                    {"polls": polls, "waited_s": round(waited, 1)},
                )
                return ParkWake(
                    outcome="timeout",
                    decision=decision,
                    polls=polls,
                    waited_s=waited,
                )
            sleep(min(poll_interval_s, remaining))
        else:
            sleep(poll_interval_s)


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
