"""Credential-provider abstraction for the usage fetcher.

Providers are tried in order; the first that yields a token wins. If NO
provider yields a token the guardrails fail closed (pause + notify) — a
locked keychain, expired token, or missing file must never silently disable
them. This is what makes Linux boxes and public adopters first-class.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ENV_TOKEN_VARS = ("OMATER_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")
KEYCHAIN_SERVICE = "Claude Code-credentials"
CREDS_FILE = Path("~/.claude/.credentials.json")
CLAUDE_JSON = Path("~/.claude.json")


class CredentialsUnavailable(Exception):
    """No provider yielded a token — guardrails must fail closed."""


class EnvTokenProvider:
    name = "env"

    def __init__(self, env: dict[str, str] | None = None):
        import os

        self._env = env if env is not None else os.environ

    def get_token(self) -> str | None:
        for var in ENV_TOKEN_VARS:
            token = self._env.get(var)
            if token:
                return token.strip()
        return None


class KeychainProvider:
    """macOS keychain: the same entry Claude Code itself writes."""

    name = "keychain"

    def __init__(self, service: str = KEYCHAIN_SERVICE):
        self.service = service

    def get_token(self) -> str | None:
        if sys.platform != "darwin":
            return None
        try:
            proc = subprocess.run(
                ["security", "find-generic-password", "-s", self.service, "-w"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0:
            return None
        return _token_from_credentials_blob(proc.stdout.strip())


class CredsFileProvider:
    """Linux/headless: Claude Code's `~/.claude/.credentials.json`."""

    name = "creds-file"

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else CREDS_FILE.expanduser()

    def get_token(self) -> str | None:
        try:
            blob = self.path.read_text(encoding="utf-8")
        except OSError:
            return None
        return _token_from_credentials_blob(blob)


def _token_from_credentials_blob(blob: str) -> str | None:
    try:
        data = json.loads(blob)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    oauth = data.get("claudeAiOauth")
    token = oauth.get("accessToken") if isinstance(oauth, dict) else None
    # strictly a non-empty string: a number/object here would crash
    # token.encode() in account_identity and breach the never-raises
    # contract of the refresh path
    return token if isinstance(token, str) and token else None


def default_providers() -> list[Any]:
    return [EnvTokenProvider(), KeychainProvider(), CredsFileProvider()]


def acquire_token(providers: list[Any] | None = None) -> tuple[str, str]:
    """Return (token, provider_name), or raise CredentialsUnavailable."""
    errors: list[str] = []
    for provider in providers if providers is not None else default_providers():
        try:
            token = provider.get_token()
        except Exception as exc:  # a broken provider must not mask the rest
            errors.append(f"{provider.name}: {exc}")
            continue
        if token:
            return token, provider.name
        errors.append(f"{provider.name}: no token")
    raise CredentialsUnavailable("; ".join(errors) or "no providers configured")


ACCOUNT_ID_ENV = "OMATER_ACCOUNT_ID"


def account_identity(
    claude_json_path: Path | str | None = None,
    token: str | None = None,
    provider: str | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Which account do these usage numbers describe? Quota is account-global
    and an account switch must re-baseline the guardrails, so every snapshot
    records identity.

    Resolution order:
    - `OMATER_ACCOUNT_ID` env override (headless boxes, multi-account setups);
    - for an env-provided token, a fingerprint of the token itself — an env
      token is not necessarily the account Claude Code is logged into, so
      the logged-in identity must not be attributed to it;
    - Claude Code's own account record (keychain/creds-file tokens belong to
      the logged-in account);
    - a token fingerprint as the last resort.
    """
    import os

    env = env if env is not None else dict(os.environ)
    override = env.get(ACCOUNT_ID_ENV)
    if override:
        return {"id": override}

    def fingerprint() -> dict[str, str]:
        return {"fingerprint": hashlib.sha256(token.encode()).hexdigest()[:12]}

    if provider == "env" and token:
        return fingerprint()
    path = Path(claude_json_path) if claude_json_path else CLAUDE_JSON.expanduser()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        acct = data.get("oauthAccount") or {}
        if acct.get("accountUuid"):
            return {
                "uuid": acct["accountUuid"],
                "email": acct.get("emailAddress", ""),
            }
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    if token:
        return fingerprint()
    return {"unknown": "true"}
