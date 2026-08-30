"""Usage guardrails — the AC: demonstrably pause/degrade/notify at the
configured thresholds via the fake-usage injection path, and fail CLOSED on
a stale usage cache or missing credentials."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

import pytest

from claudomater.config import UserConfig
from claudomater.credentials import (
    CredentialsUnavailable,
    CredsFileProvider,
    EnvTokenProvider,
    account_identity,
    acquire_token,
)
from claudomater.guardrails import (
    Decision,
    evaluate,
    model_for_phase,
    next_model,
    scope_applies,
)
from claudomater.usage import (
    FAKE_USAGE_ENV,
    UsageSnapshot,
    UsageUnavailable,
    parse_limits,
    read_usage,
)


def snapshot(five=10.0, seven=10.0, scoped=10.0, account=None, **kw):
    return UsageSnapshot(
        five_hour=five,
        seven_day=seven,
        scoped=scoped,
        scoped_model="Fable",
        five_hour_resets_at="2026-08-28T22:49:59Z",
        seven_day_resets_at="2026-08-31T04:59:59Z",
        scoped_resets_at="2026-08-31T04:59:59Z",
        account=account or {"uuid": "acct-1"},
        fetched_at=time.time(),
        source="fake",
        **kw,
    )


def iso_utc(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def write_fake(tmp_path, monkeypatch, data, age_s=0):
    path = tmp_path / "fake-usage.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    if age_s:
        old = time.time() - age_s
        os.utime(path, (old, old))
    monkeypatch.setenv(FAKE_USAGE_ENV, str(path))
    return path


class TestEvaluate:
    def test_below_thresholds_is_ok(self):
        d = evaluate(snapshot(), UserConfig())
        assert d.action == "ok"

    def test_five_hour_at_pause_threshold_pauses(self):
        d = evaluate(snapshot(five=95), UserConfig())
        assert d.action == "pause"
        assert d.window == "five_hour"
        assert d.resets_at == "2026-08-28T22:49:59Z"

    def test_seven_day_degrade_when_configured(self):
        cfg = UserConfig()
        cfg.usage.on_threshold["seven_day"] = "degrade"
        d = evaluate(snapshot(seven=96), cfg)
        assert d.action == "degrade"
        assert d.window == "seven_day"

    def test_pause_dominates_degrade_when_both_windows_trip(self):
        cfg = UserConfig()
        cfg.usage.on_threshold["seven_day"] = "degrade"
        d = evaluate(snapshot(five=99, seven=99), cfg)
        assert d.action == "pause"

    def test_scoped_quota_triggers_degrade(self):
        d = evaluate(snapshot(scoped=80), UserConfig())
        assert d.action == "degrade"
        assert d.window == "scoped"

    def test_fail_closed_on_unavailable_usage(self):
        d = evaluate(UsageUnavailable("stale-cache: 900s old"), UserConfig())
        assert d.action == "pause"
        assert "failing closed" in d.reasons[0]

    def test_fail_closed_on_missing_window(self):
        d = evaluate(snapshot(five=None), UserConfig())
        assert d.action == "pause"
        assert "failing closed" in d.reasons[0]

    def test_account_switch_rebaselines(self):
        d = evaluate(
            snapshot(account={"uuid": "acct-2"}),
            UserConfig(),
            baseline_account={"uuid": "acct-1"},
        )
        assert d.rebaselined
        assert d.action == "ok"

    def test_same_account_does_not_rebaseline(self):
        d = evaluate(
            snapshot(account={"uuid": "acct-1"}),
            UserConfig(),
            baseline_account={"uuid": "acct-1"},
        )
        assert not d.rebaselined


class TestDegradePath:
    def test_walks_the_chain(self):
        path = ["claude-opus-5", "claude-sonnet-5"]
        assert next_model("claude-fable-5", path) == "claude-opus-5"
        assert next_model("claude-opus-5", path) == "claude-sonnet-5"
        assert next_model("claude-sonnet-5", path) == "pause"

    def test_default_path_steps_down_once_then_pauses(self):
        path = UserConfig().usage.degrade_path
        assert next_model("claude-fable-5", path) == "claude-opus-5"
        assert next_model("claude-opus-5", path) == "pause"

    def test_never_steps_a_model_up_the_chain(self):
        # sonnet with path [opus, pause]: opus would be an UPGRADE — degrading
        # must leave it alone, not burn more quota
        assert next_model("claude-sonnet-5", ["claude-opus-5", "pause"]) == "claude-sonnet-5"

    def test_unknown_model_is_left_alone(self):
        assert next_model("some-local-model", ["claude-opus-5", "pause"]) == "some-local-model"


class TestScopeApplies:
    def test_scoped_model_name_matches_family(self):
        path = ["claude-opus-5", "pause"]
        assert scope_applies("claude-fable-5", "Fable", path)
        assert not scope_applies("claude-opus-5", "Fable", path)
        assert not scope_applies("claude-sonnet-5", "Fable", path)

    def test_unknown_scope_falls_back_to_rank(self):
        path = ["claude-opus-5", "pause"]
        assert scope_applies("claude-fable-5", None, path)  # above the chain start
        assert not scope_applies("claude-opus-5", None, path)

    def test_renamed_display_name_cannot_disarm_the_scope(self):
        """'Fable' -> 'Fable 5' must still match the fable family by rank —
        a display-name change must not silently disable the scoped guardrail."""
        path = ["claude-opus-5", "pause"]
        assert scope_applies("claude-fable-5", "Fable 5", path)
        assert not scope_applies("claude-opus-5", "Fable 5", path)


class TestModelForPhase:
    def test_ok_returns_required_model(self):
        model, reason = model_for_phase(
            "claude-fable-5", Decision(action="ok"), UserConfig()
        )
        assert model == "claude-fable-5" and reason is None

    def test_pause_returns_none(self):
        model, reason = model_for_phase(
            "claude-fable-5",
            Decision(action="pause", reasons=["5h window at 96%"]),
            UserConfig(),
        )
        assert model is None
        assert "5h window" in reason

    def test_window_degrade_steps_down(self):
        model, reason = model_for_phase(
            "claude-fable-5",
            Decision(action="degrade", window="seven_day", reasons=["7d at 96%"]),
            UserConfig(),
        )
        assert model == "claude-opus-5"

    def test_window_degrade_path_exhaustion_pauses(self):
        # the operator configured [opus, pause]: one step down, then pause
        model, reason = model_for_phase(
            "claude-opus-5", Decision(action="degrade", window="seven_day"), UserConfig()
        )
        assert model is None
        assert "pausing for the user" in reason

    def test_window_degrade_never_upgrades_a_lower_tier(self):
        model, reason = model_for_phase(
            "claude-sonnet-5", Decision(action="degrade", window="seven_day"), UserConfig()
        )
        assert model == "claude-sonnet-5"

    def test_scoped_degrade_only_touches_the_scoped_tier(self):
        """A Fable-scoped trip must not pause opus dev phases whose quota is
        untouched — they keep working at full tier."""
        decision = Decision(
            action="degrade", window="scoped", reasons=["scoped at 85%"], snapshot=snapshot()
        )
        model, reason = model_for_phase("claude-opus-5", decision, UserConfig())
        assert (model, reason) == ("claude-opus-5", None)
        model, _ = model_for_phase("claude-fable-5", decision, UserConfig())
        assert model == "claude-opus-5"

    def test_skip_sentinel_passes_through_degrade(self):
        model, reason = model_for_phase(
            "skip", Decision(action="degrade", window="seven_day"), UserConfig()
        )
        assert (model, reason) == ("skip", None)

    def test_escalated_story_never_runs_degraded(self):
        model, reason = model_for_phase(
            "claude-fable-5",
            Decision(action="degrade", reasons=["scoped at 85%"]),
            UserConfig(),
            escalated=True,
        )
        assert model is None
        assert "never runs degraded" in reason

    def test_escalated_story_on_an_unaffected_tier_keeps_running(self):
        """The escalation rule pauses a story whose required tier is
        UNAVAILABLE. An escalated opus story during a Fable-scoped trip has
        its tier fully available — a 2am false pause here is the exact
        failure class the system exists to remove."""
        decision = Decision(
            action="degrade", window="scoped", reasons=["scoped at 85%"], snapshot=snapshot()
        )
        model, reason = model_for_phase(
            "claude-opus-5", decision, UserConfig(), escalated=True
        )
        assert (model, reason) == ("claude-opus-5", None)

    def test_escalated_story_below_the_degrade_path_keeps_running(self):
        # window degrade, but sonnet has nothing lower on [opus, pause]:
        # its tier is available, escalation does not pause it
        model, reason = model_for_phase(
            "claude-sonnet-5",
            Decision(action="degrade", window="seven_day"),
            UserConfig(),
            escalated=True,
        )
        assert (model, reason) == ("claude-sonnet-5", None)


class TestFakeUsageInjection:
    """The fake-usage injection path makes the guardrail ACs testable in CI."""

    def test_simplified_fake_shape(self, tmp_path, monkeypatch):
        write_fake(
            tmp_path,
            monkeypatch,
            {"five_hour": 50, "seven_day": 60, "scoped": 70, "account": {"uuid": "a"}},
        )
        snap = read_usage()
        assert snap.source == "fake"
        assert (snap.five_hour, snap.seven_day, snap.scoped) == (50, 60, 70)
        assert snap.account == {"uuid": "a"}

    def test_raw_api_fake_shape(self, tmp_path, monkeypatch):
        write_fake(
            tmp_path,
            monkeypatch,
            {
                "limits": [
                    {"kind": "session", "percent": 46, "resets_at": "2026-08-28T22:49:59Z"},
                    {"kind": "weekly_all", "percent": 56, "resets_at": "2026-08-31T04:59:59Z"},
                    {
                        "kind": "weekly_scoped",
                        "percent": 66,
                        "resets_at": "2026-08-31T04:59:59Z",
                        "scope": {"model": {"display_name": "Fable"}},
                    },
                ]
            },
        )
        snap = read_usage()
        assert (snap.five_hour, snap.seven_day, snap.scoped) == (46, 56, 66)
        assert snap.scoped_model == "Fable"
        assert snap.five_hour_resets_at == "2026-08-28T22:49:59Z"

    def test_fake_over_threshold_pauses_end_to_end(self, tmp_path, monkeypatch):
        write_fake(tmp_path, monkeypatch, {"five_hour": 97, "seven_day": 40, "scoped": 10})
        d = evaluate(read_usage(), UserConfig())
        assert d.action == "pause"

    def test_stale_fake_still_raises_and_carries_the_last_reading(self, tmp_path, monkeypatch):
        """AC (revised 2026-08-30): staleness still raises, but the raise
        carries the parsed last reading so the guardrail can apply the
        staleness-AND-near-limit rule instead of pausing blind."""
        write_fake(
            tmp_path, monkeypatch, {"five_hour": 1, "seven_day": 1, "scoped": 1}, age_s=4000
        )
        with pytest.raises(UsageUnavailable, match="stale-cache") as excinfo:
            read_usage()
        exc = excinfo.value
        assert exc.snapshot is not None and exc.snapshot.source == "stale"
        assert exc.snapshot.five_hour == 1
        assert exc.age_s is not None and exc.age_s > 3900

    def test_unreadable_fake_fails_closed(self, tmp_path, monkeypatch):
        monkeypatch.setenv(FAKE_USAGE_ENV, str(tmp_path / "missing.json"))
        with pytest.raises(UsageUnavailable):
            read_usage()

    def test_non_object_fake_json_fails_closed(self, tmp_path, monkeypatch):
        path = tmp_path / "fake-usage.json"
        path.write_text("[]", encoding="utf-8")
        monkeypatch.setenv(FAKE_USAGE_ENV, str(path))
        with pytest.raises(UsageUnavailable, match="JSON object"):
            read_usage()

    def test_non_object_cache_json_fails_closed(self, tmp_path, monkeypatch):
        monkeypatch.delenv(FAKE_USAGE_ENV, raising=False)
        cache = tmp_path / "cache.json"
        cache.write_text("[]", encoding="utf-8")
        with pytest.raises(UsageUnavailable, match="JSON object"):
            read_usage(cache_path=cache, providers=[])


class TestStaleTtlAndNearLimitRule:
    """Epic 9 rough edge #4: the 300s TTL was shorter than a typical phase,
    so every spawn gate forced a live fetch, the endpoint 429'd, and the run
    paused at 17% real usage. Two fixes, both pinned here: the TTL exceeds
    the longest phase, and a pause past the TTL requires staleness AND a
    near-limit last reading (projected at STALE_DRIFT_PP_PER_MIN)."""

    def test_stale_ttl_exceeds_the_longest_phase_timeout(self):
        """A reading taken at one spawn gate must survive to the next gate
        even if every refresh between them fails. Cross-module pin: nobody
        gets to grow a phase timeout past the TTL (or shrink the TTL) without
        this test naming the invariant they broke."""
        from claudomater.phases import DEFAULT_TIMEOUT_S
        from claudomater.usage import DEFAULT_MAX_STALE_S

        assert DEFAULT_MAX_STALE_S > DEFAULT_TIMEOUT_S

    def test_reading_older_than_the_old_300s_ttl_is_no_longer_stale(
        self, tmp_path, monkeypatch
    ):
        """The Epic 9 incident's exact shape: a 346s-old reading (fresh by
        any phase-length standard) must read fine, not raise."""
        write_fake(
            tmp_path, monkeypatch, {"five_hour": 17, "seven_day": 2, "scoped": 4}, age_s=346
        )
        snap = read_usage()
        assert snap.five_hour == 17

    def _stale_exc(self, tmp_path, monkeypatch, data, age_s):
        write_fake(tmp_path, monkeypatch, data, age_s=age_s)
        with pytest.raises(UsageUnavailable) as excinfo:
            read_usage()
        return excinfo.value

    def test_stale_low_reading_proceeds_at_degraded_confidence(
        self, tmp_path, monkeypatch
    ):
        """Staleness alone is not evidence of exhaustion: a 17%-style last
        reading far from every threshold proceeds, with the degraded
        confidence named in the decision's reasons."""
        exc = self._stale_exc(
            tmp_path, monkeypatch,
            {"five_hour": 1, "seven_day": 1, "scoped": 1}, age_s=4000,
        )
        d = evaluate(exc, UserConfig())
        assert d.action == "ok"
        assert "degraded confidence" in d.reasons[0] and "stale" in d.reasons[0]
        assert d.snapshot is not None  # the reading rides into the run event

    def test_stale_near_limit_reading_pauses(self, tmp_path, monkeypatch):
        """The AND's other arm: stale + a reading that projects to a pause
        threshold pauses, naming the projection."""
        exc = self._stale_exc(
            tmp_path, monkeypatch,
            {"five_hour": 90, "seven_day": 1, "scoped": 1}, age_s=4000,
        )
        d = evaluate(exc, UserConfig())
        assert d.action == "pause" and d.window == "five_hour"
        assert "near-limit" in d.reasons[0] and "projects" in d.reasons[0]

    def test_pre_reset_reading_rebases_projection_at_the_reset(self):
        """Round-13 finding: the projection drifted the PRE-RESET percentage
        forward when the window reset during the stale interval — a 90%
        reading paused a window that had already restarted (the false-deny
        class the staleness-AND-near-limit rule exists to avoid). The reset
        is a known zero point strictly better than the expired reading:
        projection rebases from 0% at the reset."""
        now = time.time()
        snap = snapshot(five=90.0)
        snap.fetched_at = now - 4000  # stale past the TTL
        snap.five_hour_resets_at = iso_utc(now - 600)  # reset 10 min ago
        exc = UsageUnavailable("stale-cache", snapshot=snap, age_s=4000.0)
        d = evaluate(exc, UserConfig())
        assert d.action == "ok"
        assert any("rebased from 0%" in r for r in d.reasons)
        # a reset grants no free pass: drift from the reset's zero point
        # still self-caps once enough stale time passes
        snap.fetched_at = now - 200000
        snap.five_hour_resets_at = iso_utc(now - 190000)
        exc = UsageUnavailable("stale-cache", snapshot=snap, age_s=200000.0)
        d = evaluate(exc, UserConfig())
        assert d.action == "pause" and d.window == "five_hour"
        assert "rebased from 0%" in d.reasons[0]

    def test_reading_taken_after_the_reset_is_not_rebased(self):
        """Round-14 finding: rebasing keyed only on `reset <= now`, so a
        resets_at OLDER than the reading itself (incoherent cache, an API
        reporting the LAST reset) discarded a valid 90% reading for a lower
        from-zero projection — fail OPEN. Rebasing requires the reset
        strictly inside the stale interval (fetched_at < reset <= now); a
        reading taken after the reset already belongs to the current window
        and projects normally."""
        now = time.time()
        for reset_offset in (5000, 4000):  # before the reading; exactly at it
            snap = snapshot(five=90.0)
            snap.fetched_at = now - 4000
            snap.five_hour_resets_at = iso_utc(now - reset_offset)
            exc = UsageUnavailable("stale-cache", snapshot=snap, age_s=4000.0)
            d = evaluate(exc, UserConfig())
            assert d.action == "pause" and d.window == "five_hour"
            assert "rebased" not in d.reasons[0]

    def test_unparseable_reset_keeps_the_conservative_projection(self):
        """Garbage resets_at maps to 'cannot detect a reset' (the
        usage._num_or_none choke-point discipline): the old reading drifts
        forward, which can only pause EARLIER — never a crash mid-guardrail,
        never a local-time guess at a naive timestamp."""
        now = time.time()
        for bad in ("soon", "2026-08-30 12:00:00", None):  # naive incl.
            snap = snapshot(five=90.0)
            snap.fetched_at = now - 4000
            snap.five_hour_resets_at = bad
            exc = UsageUnavailable("stale-cache", snapshot=snap, age_s=4000.0)
            d = evaluate(exc, UserConfig())
            assert d.action == "pause", bad  # 90% + drift >= 95, no rebase

    def test_projection_caps_unbounded_staleness(self, tmp_path, monkeypatch):
        """Self-capping: even a near-zero reading pauses once it has been
        stale long enough to have plausibly burned to the threshold
        (0.5 pp/min drift) — 'proceed on stale' can never hold forever."""
        exc = self._stale_exc(
            tmp_path, monkeypatch,
            {"five_hour": 1, "seven_day": 1, "scoped": 1}, age_s=12000,
        )
        assert evaluate(exc, UserConfig()).action == "pause"

    def test_stale_reading_missing_a_window_fails_closed(
        self, tmp_path, monkeypatch
    ):
        """A stale snapshot missing a pause window is unknown, not stale."""
        exc = self._stale_exc(
            tmp_path, monkeypatch, {"five_hour": 1, "scoped": 1}, age_s=4000
        )
        d = evaluate(exc, UserConfig())
        assert d.action == "pause" and "failing closed" in d.reasons[0]

    def test_unavailable_without_a_reading_still_fails_closed(self):
        """No carve-out for genuinely unknown usage: an UsageUnavailable
        that carries no snapshot (no creds, unreadable cache) pauses."""
        d = evaluate(UsageUnavailable("no-credentials: nothing configured"), UserConfig())
        assert d.action == "pause" and "failing closed" in d.reasons[0]

    def test_stale_projection_over_a_degrade_threshold_reports_but_proceeds(
        self, tmp_path, monkeypatch
    ):
        """Round-5 finding: the stale path hard-paused every projected
        crossing, including windows the user configured as `degrade` —
        harder than the fresh-path contract for a soft threshold. A
        degrade-configured crossing on stale data is OBSERVED in the
        reasons, never acted on (degrades need fresh numbers); only
        pause-configured windows trigger the stale pause."""
        from claudomater.config import UsageConfig

        cfg = UserConfig(
            usage=UsageConfig(
                on_threshold={"five_hour": "pause", "seven_day": "degrade"}
            )
        )
        exc = self._stale_exc(
            tmp_path, monkeypatch,
            {"five_hour": 1, "seven_day": 90, "scoped": 1}, age_s=4000,
        )
        d = evaluate(exc, cfg)
        assert d.action == "ok"
        assert any("degrade-configured" in r for r in d.reasons)
        # the same crossing on a pause-configured window still pauses
        assert evaluate(exc, UserConfig()).action == "pause"

    def test_stale_hard_stop_when_no_pause_window_exists(self, tmp_path, monkeypatch):
        """Round-8 finding: with BOTH windows degrade-configured (valid
        config), the observe-don't-act rule returned OK forever — no pause
        window existed to self-cap. A crossing that can neither degrade
        (stale) nor ever pause is the stale hard stop."""
        from claudomater.config import UsageConfig

        cfg = UserConfig(
            usage=UsageConfig(
                on_threshold={"five_hour": "degrade", "seven_day": "degrade"}
            )
        )
        exc = self._stale_exc(
            tmp_path, monkeypatch,
            {"five_hour": 90, "seven_day": 1, "scoped": 1}, age_s=4000,
        )
        d = evaluate(exc, cfg)
        assert d.action == "pause"
        assert "hard stop" in d.reasons[0]
        # below every threshold, both-degrade config still proceeds
        exc2 = self._stale_exc(
            tmp_path, monkeypatch,
            {"five_hour": 1, "seven_day": 1, "scoped": 1}, age_s=4000,
        )
        assert evaluate(exc2, cfg).action == "ok"

    def test_degrade_never_acts_on_stale_data(self, tmp_path, monkeypatch):
        """A stale scoped reading past degrade_scoped_at does NOT degrade:
        degrading is a positive step that needs fresh numbers. Worst case is
        running top-tier slightly past the soft threshold until a refresh
        succeeds — the pause windows above stay the hard stop."""
        exc = self._stale_exc(
            tmp_path, monkeypatch,
            {"five_hour": 1, "seven_day": 1, "scoped": 95}, age_s=4000,
        )
        assert evaluate(exc, UserConfig()).action == "ok"

    def test_stale_path_rebaselines_account_switches(self, tmp_path, monkeypatch):
        """Round-3 finding: the stale path skipped the account-switch
        re-baselining the fresh path performs — a run baselined to account A
        could proceed on B's stale reading with rebaselined=False and no
        recorded switch reason."""
        exc = self._stale_exc(
            tmp_path, monkeypatch,
            {"five_hour": 1, "seven_day": 1, "scoped": 1,
             "account": {"uuid": "acct-b"}},
            age_s=4000,
        )
        d = evaluate(exc, UserConfig(), baseline_account={"uuid": "acct-a"})
        assert d.action == "ok" and d.rebaselined
        assert "account switch detected" in d.reasons[0]
        # same baseline, no switch: flag stays down
        d2 = evaluate(exc, UserConfig(), baseline_account={"uuid": "acct-b"})
        assert d2.action == "ok" and not d2.rebaselined

    def test_malformed_readings_fail_closed_not_open(self, tmp_path, monkeypatch):
        """json.loads accepts NaN, and NaN sails past every `>= threshold`
        comparison as False — so a malformed reading would otherwise walk the
        projection loop straight to OK. All non-finite/negative values map to
        unknown at the single parse choke point, which is a pause on the
        pause windows — stale AND fresh alike."""
        for bad in (float("nan"), float("inf"), -5):
            # stale path: the carve-out must not accept a malformed reading
            exc = self._stale_exc(
                tmp_path, monkeypatch,
                {"five_hour": bad, "seven_day": 1, "scoped": 1}, age_s=4000,
            )
            d = evaluate(exc, UserConfig())
            assert d.action == "pause", bad
            assert "failing closed" in d.reasons[0], bad
            # fresh path: same hole, same fix
            write_fake(
                tmp_path, monkeypatch,
                {"five_hour": bad, "seven_day": 1, "scoped": 1},
            )
            assert evaluate(read_usage(), UserConfig()).action == "pause", bad

    def test_over_100_percent_is_a_real_reading_not_malformed(
        self, tmp_path, monkeypatch
    ):
        """Over quota is a real state: 120% must trip the thresholds like any
        high reading, never be discarded as garbage."""
        write_fake(
            tmp_path, monkeypatch, {"five_hour": 120, "seven_day": 1, "scoped": 1}
        )
        assert evaluate(read_usage(), UserConfig()).action == "pause"


class TestStaleProvenance:
    """Round-1 finding on the carve-out: quota is account-global, so account
    A's low cache must never pass as account B's stale reading. omater's own
    refreshes record who fetched (`fetched_by`, inside the payload — atomic
    with the numbers); the carve-out requires a match with the active
    account, and a cache without provenance (statusline-written) is
    unverifiable = fail closed."""

    def _stale_cache(self, tmp_path, payload, age_s=4000):
        cache = tmp_path / "cache.json"
        cache.write_text(json.dumps(payload), encoding="utf-8")
        old = time.time() - age_s
        os.utime(cache, (old, old))
        return cache

    LIMITS = [{"kind": "session", "percent": 1, "resets_at": None},
              {"kind": "weekly_all", "percent": 1, "resets_at": None}]

    def test_matching_provenance_gets_the_carve_out(self, tmp_path):
        cache = self._stale_cache(
            tmp_path, {"limits": self.LIMITS, "fetched_by": {"id": "acct-a"}}
        )
        with pytest.raises(UsageUnavailable) as excinfo:
            read_usage(
                cache_path=cache, providers=[], env={"OMATER_ACCOUNT_ID": "acct-a"}
            )
        exc = excinfo.value
        assert exc.snapshot is not None and exc.snapshot.account == {"id": "acct-a"}
        assert evaluate(exc, UserConfig()).action == "ok"

    def test_foreign_provenance_fails_closed(self, tmp_path):
        """The multi-account hole: A's low reading, B's session."""
        cache = self._stale_cache(
            tmp_path, {"limits": self.LIMITS, "fetched_by": {"id": "acct-a"}}
        )
        with pytest.raises(UsageUnavailable, match="different account") as excinfo:
            read_usage(
                cache_path=cache, providers=[], env={"OMATER_ACCOUNT_ID": "acct-b"}
            )
        assert excinfo.value.snapshot is None
        assert evaluate(excinfo.value, UserConfig()).action == "pause"

    def test_unrecorded_provenance_fails_closed(self, tmp_path):
        """A statusline-written cache carries no fetched_by: unverifiable."""
        cache = self._stale_cache(tmp_path, {"limits": self.LIMITS})
        with pytest.raises(UsageUnavailable, match="no recorded account") as excinfo:
            read_usage(
                cache_path=cache, providers=[], env={"OMATER_ACCOUNT_ID": "acct-a"}
            )
        assert excinfo.value.snapshot is None
        assert evaluate(excinfo.value, UserConfig()).action == "pause"

    def test_env_token_identity_survives_a_failed_refresh(self, tmp_path):
        """Round-2 finding: the carve-out compared provenance against a bare
        account_identity(env=...) recomputation, which cannot reproduce an
        env token's fingerprint (it has no token) — locking env-token users
        out of the carve-out forever. The active identity is the credential
        the failed refresh actually ACQUIRED."""
        import urllib.error

        from claudomater.credentials import EnvTokenProvider
        from claudomater.usage import refresh_cache

        provider = EnvTokenProvider(env={"OMATER_OAUTH_TOKEN": "tok"})
        cache = tmp_path / "cache.json"
        ok, _, account = refresh_cache(
            cache,
            providers=[provider],
            http=lambda url, headers, timeout: json.dumps(
                {"limits": self.LIMITS}
            ).encode(),
            env={},
        )
        assert ok is True
        old = time.time() - 4000
        os.utime(cache, (old, old))

        def failing_http(url, headers, timeout):
            raise urllib.error.URLError("429: Too Many Requests")

        with pytest.raises(UsageUnavailable) as excinfo:
            read_usage(cache_path=cache, providers=[provider], http=failing_http, env={})
        exc = excinfo.value
        assert exc.snapshot is not None  # fingerprint matched fingerprint
        assert exc.snapshot.account == account
        assert evaluate(exc, UserConfig()).action == "ok"

    def test_fresh_ish_cache_from_another_account_fails_closed(self, tmp_path):
        """Round-8 finding: the provenance gate lived only in the stale
        branch — a YOUNGER-than-TTL cache from account A was still served
        while the active credential was B, letting B proceed on A's quota
        (and the 3900s TTL keeps such caches usable far longer than 300s
        did). A failed refresh that positively identifies a different
        account than the cache provenance fails closed at ANY age."""
        import urllib.error

        from claudomater.credentials import EnvTokenProvider

        cache = self._stale_cache(
            tmp_path,
            {"limits": self.LIMITS, "fetched_by": {"id": "acct-a"}},
            age_s=120,  # younger than max_stale, old enough to attempt refresh
        )

        def failing_http(url, headers, timeout):
            raise urllib.error.URLError("429: Too Many Requests")

        with pytest.raises(UsageUnavailable, match="account-mismatch") as excinfo:
            read_usage(
                cache_path=cache,
                providers=[EnvTokenProvider(env={"OMATER_OAUTH_TOKEN": "tok"})],
                http=failing_http,
                env={},
            )
        assert excinfo.value.snapshot is None
        assert evaluate(excinfo.value, UserConfig()).action == "pause"

    def test_unknown_to_unknown_identity_gets_no_carve_out(
        self, tmp_path, monkeypatch
    ):
        """Round-11 finding: on a headless box with no resolvable account,
        both sides of the provenance equality can be the {'unknown': 'true'}
        placeholder — and unknown == unknown is not proof the cache belongs
        to the active account. The carve-out requires a POSITIVE identity."""
        monkeypatch.setattr(
            "claudomater.usage.account_identity",
            lambda **kw: {"unknown": "true"},
        )
        cache = self._stale_cache(
            tmp_path,
            {"limits": self.LIMITS, "fetched_by": {"unknown": "true"}},
            age_s=4000,
        )
        with pytest.raises(UsageUnavailable, match="unknown-identity") as excinfo:
            read_usage(cache_path=cache, providers=[], env={})
        assert excinfo.value.snapshot is None
        assert evaluate(excinfo.value, UserConfig()).action == "pause"

    def test_unprovenanced_cache_with_a_foreign_credential_fails_closed(
        self, tmp_path
    ):
        """Round-9 finding: an unprovenanced (statusline-written) cache
        bypassed the mismatch check entirely. The cache's owner is now
        derived from the LOADED payload — recorded provenance, else the
        logged-in identity under the shared-cache assumption — and a
        positively-identified foreign credential fails closed either way."""
        import urllib.error

        from claudomater.credentials import EnvTokenProvider

        cache = self._stale_cache(tmp_path, {"limits": self.LIMITS}, age_s=120)

        def failing_http(url, headers, timeout):
            raise urllib.error.URLError("429: Too Many Requests")

        # active credential: env-token fingerprint; cache owner: the
        # logged-in identity (no OMATER_ACCOUNT_ID) — never the same value
        with pytest.raises(UsageUnavailable, match="account-mismatch"):
            read_usage(
                cache_path=cache,
                providers=[EnvTokenProvider(env={"OMATER_OAUTH_TOKEN": "tok"})],
                http=failing_http,
                env={},
            )

    def test_unprovenanced_cache_with_the_same_identity_still_serves(
        self, tmp_path
    ):
        """The common single-account case must keep working: statusline
        cache, refresh 429s, but the acquired credential IS the logged-in
        identity (here both pinned via OMATER_ACCOUNT_ID) — the shared-cache
        assumption applies and the reading serves."""
        import urllib.error

        from claudomater.credentials import EnvTokenProvider

        cache = self._stale_cache(tmp_path, {"limits": self.LIMITS}, age_s=120)

        def failing_http(url, headers, timeout):
            raise urllib.error.URLError("429: Too Many Requests")

        snap = read_usage(
            cache_path=cache,
            providers=[EnvTokenProvider(env={"OMATER_OAUTH_TOKEN": "tok"})],
            http=failing_http,
            env={"OMATER_ACCOUNT_ID": "acct-a"},
        )
        assert snap.source == "cache"
        assert snap.account == {"id": "acct-a"}

    def test_refresh_embeds_provenance_and_fresh_reads_prefer_it(self, tmp_path):
        """The write side of the contract: omater's refresh records who
        fetched, and an unrefreshed later read attributes the numbers to the
        recorded account — not to whoever happens to be reading (which would
        hide an account switch from the re-baseline check)."""
        from claudomater.credentials import EnvTokenProvider
        from claudomater.usage import refresh_cache

        cache = tmp_path / "cache.json"
        ok, reason, account = refresh_cache(
            cache,
            providers=[EnvTokenProvider(env={"OMATER_OAUTH_TOKEN": "tok"})],
            http=lambda url, headers, timeout: json.dumps(
                {"limits": self.LIMITS}
            ).encode(),
            env={"OMATER_ACCOUNT_ID": "acct-a"},
        )
        assert ok is True
        assert json.loads(cache.read_text())["fetched_by"] == account
        snap = read_usage(
            cache_path=cache, providers=[], env={"OMATER_ACCOUNT_ID": "acct-b"}
        )
        assert snap.source == "cache"
        assert snap.account == account  # recorded provenance, not the reader


class TestRealPathFailClosed:
    def test_no_credentials_and_no_cache_fails_closed(self, tmp_path, monkeypatch):
        monkeypatch.delenv(FAKE_USAGE_ENV, raising=False)
        with pytest.raises(UsageUnavailable, match="no-usage-data"):
            read_usage(
                cache_path=tmp_path / "cache.json",
                providers=[],  # no provider yields a token
            )

    def test_stale_cache_with_failed_refresh_fails_closed(self, tmp_path, monkeypatch):
        """Stale beyond the TTL still raises; a payload with no readable
        windows gives the near-limit rule nothing to project, so evaluate()
        stays fail-closed."""
        monkeypatch.delenv(FAKE_USAGE_ENV, raising=False)
        cache = tmp_path / "cache.json"
        cache.write_text(json.dumps({"limits": []}), encoding="utf-8")
        old = time.time() - 4000
        os.utime(cache, (old, old))
        with pytest.raises(UsageUnavailable, match="stale-cache") as excinfo:
            read_usage(cache_path=cache, providers=[])
        assert evaluate(excinfo.value, UserConfig()).action == "pause"

    def test_fresh_cache_without_creds_still_reads(self, tmp_path, monkeypatch):
        """A fresh cache (e.g. the statusline just refreshed it) is usable
        even when omater itself cannot acquire a token right now."""
        monkeypatch.delenv(FAKE_USAGE_ENV, raising=False)
        cache = tmp_path / "cache.json"
        cache.write_text(
            json.dumps({"limits": [{"kind": "session", "percent": 12, "resets_at": None}]}),
            encoding="utf-8",
        )
        snap = read_usage(cache_path=cache, providers=[])
        assert snap.five_hour == 12
        assert snap.source == "cache"

    def test_fresh_cache_skips_the_network(self, tmp_path):
        """The statusline refreshes the same cache on its own TTL; a fresh
        cache must not trigger another fetch (the endpoint 429s when hammered)."""
        cache = tmp_path / "cache.json"
        cache.write_text(
            json.dumps({"limits": [{"kind": "session", "percent": 5, "resets_at": None}]}),
            encoding="utf-8",
        )

        def exploding_http(url, headers, timeout):
            raise AssertionError("network must not be touched for a fresh cache")

        snap = read_usage(
            cache_path=cache,
            providers=[EnvTokenProvider(env={"OMATER_OAUTH_TOKEN": "tok"})],
            http=exploding_http,
            env={},
        )
        assert snap.five_hour == 5
        assert snap.source == "cache"

    def test_non_object_api_response_never_raises(self, tmp_path):
        """refresh_cache's contract is (False, reason, account), never an
        exception — json.loads('null') must not TypeError on the 'limits'
        membership test."""
        from claudomater.usage import refresh_cache

        for body in (b"null", b"42", b'"oops"', b"[]"):
            ok, reason, _ = refresh_cache(
                tmp_path / "cache.json",
                providers=[EnvTokenProvider(env={"OMATER_OAUTH_TOKEN": "tok"})],
                http=lambda url, headers, timeout, b=body: b,
            )
            assert ok is False
            assert "fetch-failed" in reason

    def test_non_string_resets_at_in_fake_degrades_to_none(self, tmp_path, monkeypatch):
        write_fake(
            tmp_path,
            monkeypatch,
            {"five_hour": 96, "seven_day": 1, "scoped": 1, "five_hour_resets_at": 12345},
        )
        snap = read_usage()
        assert snap.five_hour_resets_at is None  # str|None, same as the API path
        assert evaluate(snap, UserConfig()).action == "pause"  # decision unaffected

    def test_non_object_fake_account_degrades_to_placeholder(self, tmp_path, monkeypatch):
        write_fake(
            tmp_path,
            monkeypatch,
            {"five_hour": 1, "seven_day": 1, "scoped": 1, "account": "just-a-string"},
        )
        snap = read_usage()
        assert snap.account == {"fake": "true"}  # dict consumers stay safe

    def test_non_string_scoped_model_in_fake_degrades_to_none(self, tmp_path, monkeypatch):
        write_fake(
            tmp_path,
            monkeypatch,
            {"five_hour": 1, "seven_day": 1, "scoped": 85, "scoped_model": 42},
        )
        snap = read_usage()
        assert snap.scoped_model is None
        # and the degrade decision that follows must not crash
        assert evaluate(snap, UserConfig()).action == "degrade"

    def test_refresh_writes_cache_via_injected_http(self, tmp_path):
        payload = {
            "limits": [
                {"kind": "session", "percent": 33, "resets_at": None},
                {"kind": "weekly_all", "percent": 44, "resets_at": None},
            ]
        }
        calls = {}

        def fake_http(url, headers, timeout):
            calls["url"] = url
            calls["auth"] = headers.get("Authorization")
            return json.dumps(payload).encode()

        cache = tmp_path / "cache.json"
        snap = read_usage(
            cache_path=cache,
            providers=[EnvTokenProvider(env={"OMATER_OAUTH_TOKEN": "tok-123"})],
            http=fake_http,
            env={},
        )
        assert snap.source == "live"
        assert snap.five_hour == 33
        assert calls["auth"] == "Bearer tok-123"
        assert json.loads(cache.read_text())["limits"][0]["percent"] == 33


class TestCredentialProviders:
    def test_env_provider(self):
        provider = EnvTokenProvider(env={"CLAUDE_CODE_OAUTH_TOKEN": "tok"})
        assert provider.get_token() == "tok"
        assert EnvTokenProvider(env={}).get_token() is None

    def test_creds_file_provider(self, tmp_path):
        path = tmp_path / ".credentials.json"
        path.write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "file-tok"}}), encoding="utf-8"
        )
        assert CredsFileProvider(path).get_token() == "file-tok"
        assert CredsFileProvider(tmp_path / "missing").get_token() is None

    def test_creds_file_malformed_is_none(self, tmp_path):
        path = tmp_path / ".credentials.json"
        path.write_text("not json", encoding="utf-8")
        assert CredsFileProvider(path).get_token() is None

    def test_non_string_access_token_is_none(self, tmp_path):
        """A number/object accessToken would crash token.encode() downstream
        and breach the refresh path's never-raises contract."""
        path = tmp_path / ".credentials.json"
        for blob in (
            {"claudeAiOauth": {"accessToken": 12345}},
            {"claudeAiOauth": {"accessToken": {"nested": "x"}}},
            {"claudeAiOauth": {"accessToken": ""}},
            {"claudeAiOauth": "not-a-dict"},
            ["not-a-dict-at-all"],
        ):
            path.write_text(json.dumps(blob), encoding="utf-8")
            assert CredsFileProvider(path).get_token() is None, blob

    def test_acquire_token_chain_order_and_fail_closed(self, tmp_path):
        with pytest.raises(CredentialsUnavailable):
            acquire_token([EnvTokenProvider(env={})])
        token, name = acquire_token(
            [
                EnvTokenProvider(env={}),
                EnvTokenProvider(env={"OMATER_OAUTH_TOKEN": "second"}),
            ]
        )
        assert (token, name) == ("second", "env")

    def test_nonconforming_providers_fail_closed_not_crash(self):
        """A provider returning a non-string token, or lacking .name, must
        degrade to 'no token' — never hand bytes to token.encode()."""

        class BytesToken:  # no .name attribute either
            def get_token(self):
                return b"raw-bytes"

        with pytest.raises(CredentialsUnavailable, match="non-string token"):
            acquire_token([BytesToken()])
        token, label = acquire_token(
            [BytesToken(), EnvTokenProvider(env={"OMATER_OAUTH_TOKEN": "ok"})]
        )
        assert (token, label) == ("ok", "env")

    def test_broken_provider_does_not_mask_the_chain(self):
        class Broken:
            name = "broken"

            def get_token(self):
                raise RuntimeError("keychain locked")

        token, _ = acquire_token(
            [Broken(), EnvTokenProvider(env={"OMATER_OAUTH_TOKEN": "ok"})]
        )
        assert token == "ok"

    def test_account_identity_from_claude_json(self, tmp_path):
        path = tmp_path / "claude.json"
        path.write_text(
            json.dumps(
                {"oauthAccount": {"accountUuid": "u-1", "emailAddress": "a@b.c"}}
            ),
            encoding="utf-8",
        )
        assert account_identity(path, env={}) == {"uuid": "u-1", "email": "a@b.c"}

    def test_account_identity_fallback_fingerprint(self, tmp_path):
        ident = account_identity(tmp_path / "missing.json", token="secret-token", env={})
        assert "fingerprint" in ident
        assert "secret-token" not in str(ident)

    def test_env_token_is_never_attributed_to_the_logged_in_account(self, tmp_path):
        """An env-provided token may belong to a different account than the
        Claude Code login — identity must come from the token, not
        ~/.claude.json (the multi-account design case)."""
        path = tmp_path / "claude.json"
        path.write_text(
            json.dumps({"oauthAccount": {"accountUuid": "logged-in"}}), encoding="utf-8"
        )
        ident = account_identity(path, token="env-token", provider="env", env={})
        assert "fingerprint" in ident
        assert ident.get("uuid") != "logged-in"

    def test_account_id_env_override_wins(self, tmp_path):
        ident = account_identity(
            tmp_path / "missing.json",
            token="t",
            provider="env",
            env={"OMATER_ACCOUNT_ID": "box-b"},
        )
        assert ident == {"id": "box-b"}

    def test_refresh_path_honors_injected_env_override(self, tmp_path):
        """OMATER_ACCOUNT_ID in the caller-provided env must reach the
        refreshed (source=live) snapshot's identity, not just the cache path."""
        payload = {"limits": [{"kind": "session", "percent": 1, "resets_at": None}]}
        snap = read_usage(
            cache_path=tmp_path / "cache.json",
            providers=[EnvTokenProvider(env={"OMATER_OAUTH_TOKEN": "tok-A"})],
            http=lambda url, headers, timeout: json.dumps(payload).encode(),
            env={"OMATER_ACCOUNT_ID": "box-b"},
        )
        assert snap.source == "live"
        assert snap.account == {"id": "box-b"}

    def test_snapshot_account_follows_the_fetching_credential(self, tmp_path):
        """read_usage attributes a refreshed snapshot to the credential that
        fetched it, so an account switch is detectable."""
        payload = {"limits": [{"kind": "session", "percent": 1, "resets_at": None}]}
        snap = read_usage(
            cache_path=tmp_path / "cache.json",
            providers=[EnvTokenProvider(env={"OMATER_OAUTH_TOKEN": "tok-A"})],
            http=lambda url, headers, timeout: json.dumps(payload).encode(),
            env={},
        )
        assert "fingerprint" in snap.account


class TestParseLimits:
    def test_missing_kinds_are_none(self):
        out = parse_limits({"limits": []})
        assert out["five_hour"] is None
        assert out["scoped_model"] is None

    def test_malformed_shapes_degrade_to_none_not_crash(self):
        """Unknown shapes must map to None (which evaluate() fails closed
        on), never raise mid-guardrail."""
        for payload in (
            {"limits": {"kind": "session"}},  # dict, not list
            {"limits": ["session", 42]},  # list of non-dicts
            {"limits": [{"kind": "session", "percent": "46%"}]},  # string pct
            {"limits": [{"kind": "session", "percent": True}]},  # bool pct
            {"limits": [{"kind": "session", "percent": 5, "resets_at": 12}]},
        ):
            out = parse_limits(payload)
            assert out["five_hour"] in (None, 5.0)
            assert out["five_hour_resets_at"] is None
        d = evaluate(
            UsageSnapshot(**parse_limits({"limits": ["junk"]}), account={}, fetched_at=0),
            UserConfig(),
        )
        assert d.action == "pause"

    def test_string_percentages_in_fake_fail_closed(self, tmp_path, monkeypatch):
        write_fake(
            tmp_path, monkeypatch, {"five_hour": "97", "seven_day": 10, "scoped": 10}
        )
        snap = read_usage()
        assert snap.five_hour is None  # strict: no guessing at strings
        assert evaluate(snap, UserConfig()).action == "pause"

    def test_scope_model_as_plain_string(self):
        out = parse_limits(
            {"limits": [{"kind": "weekly_scoped", "percent": 5, "scope": {"model": "Fable"}}]}
        )
        assert out["scoped_model"] == "Fable"

    def test_malformed_scope_shapes_degrade_to_none(self):
        """A non-string scoped_model would crash scope_applies (.lower())."""
        for scope in ("Fable", 42, ["Fable"], {"model": 42}, {"model": {"display_name": 7}}):
            out = parse_limits(
                {"limits": [{"kind": "weekly_scoped", "percent": 5, "scope": scope}]}
            )
            assert out["scoped_model"] is None, scope


# ---- Phase 1 parity findings F5/F6 (first live parity run) -----------------


from claudomater.guardrails import (
    ParkWake,
    baseline_account_from_log,
    make_guardrail_check,
    wait_for_unpark,
)
from claudomater.runlog import RunLog


def guardrail_event(account, action="ok"):
    """The exact shape PhaseRunner._gate writes: decision.as_dict()."""
    return {
        "event": "guardrail-check",
        "phase": "dev",
        "detail": Decision(action=action, snapshot=snapshot(account=account)).as_dict(),
    }


class TestBaselineFromLog:
    """Parity finding F5: the account baseline lived in process memory, so a
    park/resume boundary (fresh process) started empty and the live account
    switch was handled safely but recorded as rebaselined=False."""

    def test_seeds_from_the_last_positive_reading(self):
        events = [
            guardrail_event({"uuid": "acct-a"}),
            guardrail_event({"uuid": "acct-b"}),
        ]
        assert baseline_account_from_log(events) == {"uuid": "acct-b"}

    def test_placeholder_and_malformed_readings_cannot_anchor_a_switch(self):
        events = [
            guardrail_event({"uuid": "acct-a"}),
            guardrail_event({"unknown": "true"}),  # placeholder: nobody
            {"event": "guardrail-check", "detail": {"usage": None}},  # pause, no snap
            {"event": "guardrail-check", "detail": "garbage"},
        ]
        assert baseline_account_from_log(events) == {"uuid": "acct-a"}

    def test_no_guardrail_history_reads_as_no_baseline(self):
        assert baseline_account_from_log([]) is None
        assert baseline_account_from_log([{"event": "phase-spawn"}]) is None


class TestMakeGuardrailCheck:
    def test_switch_across_a_process_boundary_is_reported(self, tmp_path):
        """The live F5 shape: the run's log recorded account A before the
        park; the fresh process's first reading is account B. The seeded
        baseline makes the switch visible (rebaselined=True) exactly once."""
        log = RunLog.create(tmp_path)
        log.event("dev", "guardrail-check", guardrail_event({"uuid": "acct-a"})["detail"])
        check = make_guardrail_check(
            UserConfig(), runlog=log, read=lambda: snapshot(account={"uuid": "acct-b"})
        )
        first = check()
        assert first.rebaselined
        assert "acct-a" in first.reasons[0] and "acct-b" in first.reasons[0]
        # the baseline advanced: the same account again is not a switch
        assert not check().rebaselined

    def test_without_a_run_log_the_first_reading_is_the_baseline(self):
        check = make_guardrail_check(
            UserConfig(), read=lambda: snapshot(account={"uuid": "acct-b"})
        )
        assert not check().rebaselined

    def test_unavailable_reading_is_handed_to_evaluate_not_raised(self):
        def read():
            raise UsageUnavailable("no creds")

        check = make_guardrail_check(UserConfig(), read=read)
        decision = check()
        assert decision.action == "pause"
        assert "failing closed" in decision.reasons[0]

    def test_placeholder_reading_never_becomes_the_baseline(self):
        readings = [
            snapshot(account={"uuid": "acct-a"}),
            snapshot(account={"unknown": "true"}),
            snapshot(account={"uuid": "acct-b"}),
        ]
        check = make_guardrail_check(UserConfig(), read=lambda: readings.pop(0))
        assert not check().rebaselined  # seeds acct-a
        assert not check().rebaselined  # placeholder: no switch, no advance
        third = check()
        assert third.rebaselined  # acct-a -> acct-b, anchored past the gap
        assert "acct-a" in third.reasons[0]


class TestWaitForUnpark:
    """Parity finding F6: park-recovery was clock-or-human — the operator
    had already switched accounts and still had to ping manually. The wait
    loop polls the spawn gate and the control channel."""

    def _parked(self, tmp_path):
        log = RunLog.create(tmp_path)
        log.park("5h window at 100%")
        return log

    def test_wakes_on_capacity_and_logs_the_wake(self, tmp_path):
        log = self._parked(tmp_path)
        decisions = [
            Decision(action="pause"),
            Decision(action="pause"),
            Decision(action="ok", rebaselined=True),
        ]
        sleeps = []
        wake = wait_for_unpark(
            log,
            lambda: decisions.pop(0),
            poll_interval_s=300,
            sleep=sleeps.append,
            clock=lambda: float(len(sleeps)),
        )
        assert wake.outcome == "capacity" and wake.polls == 3
        assert wake.decision.action == "ok"
        assert sleeps == [300, 300]
        wakes = [e for e in log.events() if e["event"] == "park-wake"]
        assert wakes[-1]["detail"]["source"] == "capacity"
        assert wakes[-1]["detail"]["rebaselined"] is True
        # entry was write-ahead
        names = [e["event"] for e in log.events()]
        assert names.index("park-wait") < names.index("park-wake")

    def test_first_poll_is_immediate(self, tmp_path):
        """The live park was resumable the moment waiting would have begun
        (the operator had already switched accounts) — no initial sleep."""
        log = self._parked(tmp_path)
        sleeps = []
        wake = wait_for_unpark(
            log, lambda: Decision(action="ok"), sleep=sleeps.append
        )
        assert wake.outcome == "capacity" and wake.polls == 1 and sleeps == []

    def test_operator_resume_wakes_between_polls(self, tmp_path):
        log = self._parked(tmp_path)

        def sleep(_s):
            log.write_control("resume")

        wake = wait_for_unpark(log, lambda: Decision(action="pause"), sleep=sleep)
        assert wake.outcome == "resume"
        assert wake.control["action"] == "resume"

    def test_abort_dominates_resume(self, tmp_path):
        log = self._parked(tmp_path)
        log.write_control("resume")
        log.write_control("abort")
        wake = wait_for_unpark(log, lambda: Decision(action="pause"), sleep=lambda s: None)
        assert wake.outcome == "abort"

    def test_controls_from_before_the_park_are_not_consumed(self, tmp_path):
        """A resume answered to an EARLIER state must not wake a later park:
        only commands at-or-after the park are pending."""
        log = RunLog.create(tmp_path)
        log.write_control("resume")
        time.sleep(1.1)  # event ts has 1s resolution; the park must be later
        log.park("5h window at 100%")
        decisions = [Decision(action="pause"), Decision(action="ok")]
        wake = wait_for_unpark(log, lambda: decisions.pop(0), sleep=lambda s: None)
        assert wake.outcome == "capacity"

    def test_timeout_leaves_the_run_parked(self, tmp_path):
        log = self._parked(tmp_path)
        clock_box = [0.0]

        def sleep(s):
            clock_box[0] += s

        wake = wait_for_unpark(
            log,
            lambda: Decision(action="pause"),
            poll_interval_s=300,
            max_wait_s=1000,
            sleep=sleep,
            clock=lambda: clock_box[0],
        )
        assert wake.outcome == "timeout"
        assert wake.polls >= 1
        names = [e["event"] for e in log.events()]
        assert "park-wait-timeout" in names
        # still parked: completing it must be refused, aborting allowed
        from claudomater.runlog import RunError

        with pytest.raises(RunError, match="parked"):
            log.finish("run-complete")
        assert log.is_live()
