"""Usage fetcher against the OAuth usage endpoint.

Extracted from the operator's statusline script and shared with it: same
endpoint, same cache file, so the statusline benefits from omater's
refreshes and vice versa. A statusline can afford to render stale numbers;
a guardrail cannot — every read here is mtime-gated, and stale beyond
`max_stale` after a refresh attempt = unknown = fail closed.

The fake-usage injection path (`OMATER_FAKE_USAGE` -> path to a JSON file)
exercises the same parse + staleness gates, which is what makes the
guardrail acceptance criteria testable in CI. Staleness for a fake file is
its mtime, so tests can backdate it with os.utime().
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from claudomater.credentials import (
    CredentialsUnavailable,
    account_identity,
    acquire_token,
)

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA_HEADER = "oauth-2025-04-20"
DEFAULT_CACHE = Path("~/.cache/claude-statusline/usage.json")
FAKE_USAGE_ENV = "OMATER_FAKE_USAGE"
DEFAULT_MAX_STALE_S = 300
# Refresh only when the cache is older than this. The statusline refreshes
# the same cache on its own 60s TTL, and the endpoint rate-limits (429) when
# hammered — a fresh cache IS the answer, no fetch needed.
REFRESH_TTL_S = 60

# HTTP transport: callable(url, headers, timeout) -> response body bytes.
HttpFn = Callable[[str, dict[str, str], float], bytes]


class UsageUnavailable(Exception):
    """Usage numbers are unknown (no creds, fetch failed, stale cache).
    Callers must treat this as over-threshold: pause + notify, never run blind."""


@dataclass
class UsageSnapshot:
    five_hour: float | None
    seven_day: float | None
    scoped: float | None
    scoped_model: str | None
    five_hour_resets_at: str | None
    seven_day_resets_at: str | None
    scoped_resets_at: str | None
    account: dict[str, str] = field(default_factory=dict)
    fetched_at: float = 0.0  # epoch seconds (cache mtime)
    source: str = "cache"  # live | cache | fake

    def as_dict(self) -> dict[str, Any]:
        return {
            "five_hour": self.five_hour,
            "seven_day": self.seven_day,
            "scoped": self.scoped,
            "scoped_model": self.scoped_model,
            "five_hour_resets_at": self.five_hour_resets_at,
            "seven_day_resets_at": self.seven_day_resets_at,
            "scoped_resets_at": self.scoped_resets_at,
            "account": self.account,
            "fetched_at": self.fetched_at,
            "source": self.source,
        }


def _default_http(url: str, headers: dict[str, str], timeout: float) -> bytes:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return resp.read()


def _num_or_none(value: Any) -> float | None:
    """Strictly numeric or unknown. Guardrails fail CLOSED on None (a
    missing window pauses), so an unexpected type must map to None — never
    crash mid-guardrail with a TypeError, never guess at strings."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def parse_limits(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract the three windows from the API response's `limits` array.
    Unexpected shapes degrade to None fields, which the guardrails treat as
    unknown = over-threshold."""
    out: dict[str, Any] = {
        "five_hour": None,
        "seven_day": None,
        "scoped": None,
        "scoped_model": None,
        "five_hour_resets_at": None,
        "seven_day_resets_at": None,
        "scoped_resets_at": None,
    }
    limits = payload.get("limits")
    if not isinstance(limits, list):
        return out
    for limit in limits:
        if not isinstance(limit, dict):
            continue
        kind = limit.get("kind")
        pct = _num_or_none(limit.get("percent"))
        resets = limit.get("resets_at")
        if not isinstance(resets, str):
            resets = None
        if kind == "session":
            out["five_hour"], out["five_hour_resets_at"] = pct, resets
        elif kind == "weekly_all":
            out["seven_day"], out["seven_day_resets_at"] = pct, resets
        elif kind == "weekly_scoped":
            out["scoped"], out["scoped_resets_at"] = pct, resets
            scope = limit.get("scope")
            model = scope.get("model") if isinstance(scope, dict) else None
            if isinstance(model, dict):
                model = model.get("display_name")
            # strictly a string or unknown — a non-string here would crash
            # scope_applies() (.lower()) instead of degrading
            out["scoped_model"] = model if isinstance(model, str) else None
    return out


def refresh_cache(
    cache_path: Path,
    providers: list[Any] | None = None,
    http: HttpFn | None = None,
    timeout: float = 10.0,
    env: dict[str, str] | None = None,
) -> tuple[bool, str | None, dict[str, str] | None]:
    """Fetch fresh usage into the cache (atomic write). Returns
    (refreshed, failure_reason, account_identity). Never raises — the
    staleness gate in read_usage() is what turns a failed refresh into a
    pause. The identity is derived from the credential that actually
    fetched, so multi-account setups attribute the numbers correctly."""
    try:
        token, provider = acquire_token(providers)
    except CredentialsUnavailable as exc:
        return False, f"no-credentials: {exc}", None
    account = account_identity(token=token, provider=provider, env=env)
    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": OAUTH_BETA_HEADER,
    }
    try:
        body = (http or _default_http)(USAGE_URL, headers, timeout)
        payload = json.loads(body)
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError) as exc:
        return False, f"fetch-failed: {exc}", account
    if "limits" not in payload:
        return False, "fetch-failed: response has no 'limits'", account
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(cache_path)
    except OSError as exc:
        return False, f"cache-write-failed: {exc}", account
    return True, None, account


def _read_fake(path: Path) -> UsageSnapshot:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        mtime = path.stat().st_mtime
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise UsageUnavailable(f"fake-usage unreadable: {exc}") from exc
    if not isinstance(data, dict):
        # fail closed, not TypeError: valid-but-wrong-shaped JSON is still
        # unknown usage
        raise UsageUnavailable(
            f"fake-usage unreadable: expected a JSON object, got {type(data).__name__}"
        )
    if "limits" in data:
        fields = parse_limits(data)
    else:  # simplified shape: {"five_hour": 50, "seven_day": 60, "scoped": 70, ...}
        fields = {
            # non-numeric values degrade to None = unknown = fail closed,
            # never a TypeError inside evaluate()
            "five_hour": _num_or_none(data.get("five_hour")),
            "seven_day": _num_or_none(data.get("seven_day")),
            "scoped": _num_or_none(data.get("scoped")),
            "scoped_model": data.get("scoped_model", "Fable"),
            "five_hour_resets_at": data.get("five_hour_resets_at"),
            "seven_day_resets_at": data.get("seven_day_resets_at"),
            "scoped_resets_at": data.get("scoped_resets_at"),
        }
    return UsageSnapshot(
        **fields,
        account=data.get("account") or {"fake": "true"},
        fetched_at=mtime,
        source="fake",
    )


def read_usage(
    cache_path: Path | str | None = None,
    providers: list[Any] | None = None,
    http: HttpFn | None = None,
    now: float | None = None,
    max_stale: int = DEFAULT_MAX_STALE_S,
    refresh_ttl: int = REFRESH_TTL_S,
    env: dict[str, str] | None = None,
) -> UsageSnapshot:
    """Refresh + read the usage snapshot, or raise UsageUnavailable.

    Every read is mtime-gated: if the cache is stale beyond `max_stale`
    after the refresh attempt, the numbers are UNKNOWN and the caller must
    fail closed. The fake-usage env path goes through the same gate.
    """
    env = env if env is not None else os.environ  # type: ignore[assignment]
    now = now if now is not None else time.time()

    fake = env.get(FAKE_USAGE_ENV)
    if fake:
        snap = _read_fake(Path(fake))
        if now - snap.fetched_at > max_stale:
            raise UsageUnavailable(
                f"stale-cache: fake usage is {int(now - snap.fetched_at)}s old "
                f"(max {max_stale}s)"
            )
        return snap

    cache = Path(cache_path).expanduser() if cache_path else DEFAULT_CACHE.expanduser()
    try:
        cache_age = now - cache.stat().st_mtime
    except OSError:
        cache_age = float("inf")
    refreshed, failure, fetch_account = False, None, None
    if cache_age > refresh_ttl:
        refreshed, failure, fetch_account = refresh_cache(
            cache, providers=providers, http=http, env=env
        )

    try:
        payload = json.loads(cache.read_text(encoding="utf-8"))
        mtime = cache.stat().st_mtime
    except (OSError, json.JSONDecodeError, ValueError):
        raise UsageUnavailable(
            f"no-usage-data: cache unreadable at {cache}"
            + (f" ({failure})" if failure else "")
        ) from None
    if not isinstance(payload, dict):
        raise UsageUnavailable(
            f"no-usage-data: cache at {cache} is not a JSON object "
            f"(got {type(payload).__name__})"
        )

    age = now - mtime
    if age > max_stale:
        raise UsageUnavailable(
            f"stale-cache: usage data is {int(age)}s old (max {max_stale}s)"
            + (f"; refresh failed: {failure}" if failure else "")
        )

    # A cache we did not refresh was written by the logged-in account's own
    # refresher (statusline or a prior omater call) — attribute accordingly.
    return UsageSnapshot(
        **parse_limits(payload),
        account=fetch_account if refreshed else account_identity(env=env),
        fetched_at=mtime,
        source="live" if refreshed else "cache",
    )
