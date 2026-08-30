"""Usage fetcher against the OAuth usage endpoint.

Extracted from the operator's statusline script and shared with it: same
endpoint, same cache file, so the statusline benefits from omater's
refreshes and vice versa. A statusline can afford to render stale numbers;
a guardrail cannot — every read here is mtime-gated. Stale beyond
`max_stale` after a refresh attempt raises, but the raise carries the last
parsed reading so the guardrail can distinguish "stale but comfortably low"
from "genuinely unknown" (which stays fail-closed).

The fake-usage injection path (`OMATER_FAKE_USAGE` -> path to a JSON file)
exercises the same parse + staleness gates, which is what makes the
guardrail acceptance criteria testable in CI. Staleness for a fake file is
its mtime, so tests can backdate it with os.utime().
"""

from __future__ import annotations

import json
import math
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
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
# The staleness TTL must exceed the LONGEST phase timeout
# (phases.DEFAULT_TIMEOUT_S = 3600, pinned by a cross-module test): a reading
# taken at one spawn gate must still be usable at the next gate even if every
# refresh between them fails. At 300s the TTL was shorter than a typical
# phase, so EVERY gate forced a live fetch and the endpoint's 429s paused a
# run at 17% real usage (Epic 9 verification run, 2026-08-30). Staleness past
# this is still not an automatic pause: the raise carries the last reading
# and guardrails.evaluate pauses only when that reading PROJECTS near a pause
# threshold (staleness AND near-limit).
DEFAULT_MAX_STALE_S = 3900
# Refresh only when the cache is older than this. The statusline refreshes
# the same cache on its own 60s TTL, and the endpoint rate-limits (429) when
# hammered — a fresh cache IS the answer, no fetch needed.
REFRESH_TTL_S = 60

# HTTP transport: callable(url, headers, timeout) -> response body bytes.
HttpFn = Callable[[str, dict[str, str], float], bytes]


class UsageUnavailable(Exception):
    """Usage numbers are unknown (no creds, fetch failed, stale cache).
    Callers must treat this as over-threshold: pause + notify, never run
    blind — with ONE carve-out: when the failure is staleness of an
    otherwise-readable cache, the parsed last reading rides along as
    `snapshot` (source='stale') with its age in `age_s`, and
    guardrails.evaluate applies the staleness-AND-near-limit rule to it
    instead of pausing on staleness alone. No snapshot = truly unknown =
    fail closed, unchanged."""

    def __init__(
        self,
        message: str,
        snapshot: "UsageSnapshot | None" = None,
        age_s: float | None = None,
    ):
        super().__init__(message)
        self.snapshot = snapshot
        self.age_s = age_s


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
    """Strictly a plausible percentage or unknown. Guardrails fail CLOSED on
    None (a missing window pauses), so an unexpected type must map to None —
    never crash mid-guardrail with a TypeError, never guess at strings.
    NaN/±inf/negative map to None too: Python's JSON parser accepts `NaN`,
    and `NaN >= threshold` is False everywhere, so a malformed reading would
    otherwise sail PAST every threshold comparison and fail open. This is the
    single choke point every path (API payload, fake file) parses through.
    Over-100 values are kept — over quota is a real state and trips the
    thresholds naturally."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if not math.isfinite(value) or value < 0.0:
        return None
    return value


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
    if not isinstance(payload, dict) or "limits" not in payload:
        # isinstance first: "limits" in None/42 raises TypeError, and this
        # function's contract is (False, reason, account), never an exception
        return False, "fetch-failed: response is not a JSON object with 'limits'", account
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
        # Provenance rides INSIDE the payload (atomic with the numbers it
        # describes — a sidecar file could describe a cache the statusline
        # overwrote since). parse_limits ignores unknown keys and the
        # statusline reads `.limits`, so the extra key is inert there; a
        # statusline-written cache simply lacks it, which reads as "unknown
        # provenance" below.
        to_cache = dict(payload)
        to_cache["fetched_by"] = account
        tmp.write_text(json.dumps(to_cache), encoding="utf-8")
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
        scoped_model = data.get("scoped_model", "Fable")

        def _str_or_none(value: Any) -> str | None:
            return value if isinstance(value, str) else None

        fields = {
            # non-numeric values degrade to None = unknown = fail closed,
            # never a TypeError inside evaluate()
            "five_hour": _num_or_none(data.get("five_hour")),
            "seven_day": _num_or_none(data.get("seven_day")),
            "scoped": _num_or_none(data.get("scoped")),
            # string-or-unknown, same rules as parse_limits: a non-string
            # scoped_model would crash scope_applies, and non-string
            # resets_at values would leak inconsistent types into CLI
            # output and notification detail
            "scoped_model": _str_or_none(scoped_model),
            "five_hour_resets_at": _str_or_none(data.get("five_hour_resets_at")),
            "seven_day_resets_at": _str_or_none(data.get("seven_day_resets_at")),
            "scoped_resets_at": _str_or_none(data.get("scoped_resets_at")),
        }
    account = data.get("account")
    if not isinstance(account, dict):  # non-object account would crash .get() consumers
        account = {"fake": "true"}
    return UsageSnapshot(
        **fields,
        account=account,
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
                f"(max {max_stale}s)",
                snapshot=replace(snap, source="stale"),
                age_s=now - snap.fetched_at,
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
        message = f"stale-cache: usage data is {int(age)}s old (max {max_stale}s)" + (
            f"; refresh failed: {failure}" if failure else ""
        )
        # The reading is stale, not unknown — hand it to the guardrail so
        # staleness alone (Epic 9: a 429'd refresh at 17% real usage) cannot
        # pause a run whose last reading is nowhere near a limit. But ONLY
        # when its recorded provenance matches the active account: quota is
        # account-global, and in a multi-account setup account A's low cache
        # must never pass the carve-out as account B's stale reading. No
        # recorded provenance (a statusline-written cache) = unverifiable =
        # no carve-out, fail closed.
        provenance = payload.get("fetched_by")
        current = account_identity(env=env)
        stale_snapshot = None
        if isinstance(provenance, dict) and provenance == current:
            stale_snapshot = UsageSnapshot(
                **parse_limits(payload),
                account=provenance,
                fetched_at=mtime,
                source="stale",
            )
        elif isinstance(provenance, dict):
            message += "; stale reading belongs to a different account — not usable"
        else:
            message += "; stale reading has no recorded account provenance — not usable"
        raise UsageUnavailable(message, snapshot=stale_snapshot, age_s=age)

    # Attribution for a cache we did not refresh: recorded provenance first
    # (a prior omater refresh embedded who fetched it — the numbers describe
    # THAT account, whoever reads them now), the current login as fallback
    # for caches without one (statusline-written). Mislabeling a fresh-ish
    # cache with the reader's identity would hide an account switch from the
    # guardrail's re-baseline check.
    if refreshed:
        account = fetch_account
    else:
        provenance = payload.get("fetched_by")
        account = (
            provenance if isinstance(provenance, dict) else account_identity(env=env)
        )
    return UsageSnapshot(
        **parse_limits(payload),
        account=account,
        fetched_at=mtime,
        source="live" if refreshed else "cache",
    )
