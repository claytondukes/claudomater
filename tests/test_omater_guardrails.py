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

    def test_stale_fake_fails_closed(self, tmp_path, monkeypatch):
        """AC: guardrails fail CLOSED on a stale usage cache."""
        write_fake(
            tmp_path, monkeypatch, {"five_hour": 1, "seven_day": 1, "scoped": 1}, age_s=600
        )
        with pytest.raises(UsageUnavailable, match="stale-cache"):
            read_usage()
        # and evaluate() turns that into a pause:
        try:
            read_usage()
        except UsageUnavailable as exc:
            assert evaluate(exc, UserConfig()).action == "pause"

    def test_unreadable_fake_fails_closed(self, tmp_path, monkeypatch):
        monkeypatch.setenv(FAKE_USAGE_ENV, str(tmp_path / "missing.json"))
        with pytest.raises(UsageUnavailable):
            read_usage()


class TestRealPathFailClosed:
    def test_no_credentials_and_no_cache_fails_closed(self, tmp_path, monkeypatch):
        monkeypatch.delenv(FAKE_USAGE_ENV, raising=False)
        with pytest.raises(UsageUnavailable, match="no-usage-data"):
            read_usage(
                cache_path=tmp_path / "cache.json",
                providers=[],  # no provider yields a token
            )

    def test_stale_cache_with_failed_refresh_fails_closed(self, tmp_path, monkeypatch):
        monkeypatch.delenv(FAKE_USAGE_ENV, raising=False)
        cache = tmp_path / "cache.json"
        cache.write_text(json.dumps({"limits": []}), encoding="utf-8")
        old = time.time() - 3600
        os.utime(cache, (old, old))
        with pytest.raises(UsageUnavailable, match="stale-cache"):
            read_usage(cache_path=cache, providers=[])

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

    def test_scope_model_as_plain_string(self):
        out = parse_limits(
            {"limits": [{"kind": "weekly_scoped", "percent": 5, "scope": {"model": "Fable"}}]}
        )
        assert out["scoped_model"] == "Fable"
