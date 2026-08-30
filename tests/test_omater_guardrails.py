"""Usage guardrails — the AC: demonstrably pause/degrade/notify at the
configured thresholds via the fake-usage injection path, and fail CLOSED on
a stale usage cache or missing credentials."""

from __future__ import annotations

import json
import os
import time

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
            tmp_path, monkeypatch, {"five_hour": 17, "seven_day": 2, "scoped": 4}, age_s=600
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
