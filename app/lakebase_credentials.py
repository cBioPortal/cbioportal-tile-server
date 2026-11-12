"""Short-lived Lakebase passwords for the annotation database pool.

With ``ANNOTATION_DATABASE_PASSWORD_PROVIDER=databricks`` the annotation pool
does not keep a static password.  Each new connection asks
``database_password()``, which returns a Lakebase database credential (an
OAuth token valid for about an hour) generated for
``ANNOTATION_LAKEBASE_INSTANCE`` and cached until shortly before it expires.

The credential is requested from the workspace REST API
(``POST /api/2.0/database/credentials``) as a service principal:
``DATABRICKS_HOST`` with ``DATABRICKS_CLIENT_ID`` and
``DATABRICKS_CLIENT_SECRET`` (OAuth machine-to-machine), or
``DATABRICKS_TOKEN`` when no client is configured.  Only the standard library
is used.  Tokens and secrets are never logged.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger(__name__)

PROVIDER_ENV = "ANNOTATION_DATABASE_PASSWORD_PROVIDER"
INSTANCE_ENV = "ANNOTATION_LAKEBASE_INSTANCE"
# Refresh a cached token this long before it expires.
REFRESH_MARGIN_SECONDS = 300
# Assumed lifetime when a response gives no expiry.
DEFAULT_LIFETIME_SECONDS = 3600
HTTP_TIMEOUT_SECONDS = 30


class CredentialError(RuntimeError):
    """The Lakebase credential could not be obtained."""


@dataclass
class _Cached:
    value: str = ""
    expires_at: float = 0.0

    def fresh(self, now: float) -> bool:
        return bool(self.value) and now < self.expires_at - REFRESH_MARGIN_SECONDS


_workspace_token = _Cached()
_database_token = _Cached()
_lock = asyncio.Lock()


def provider() -> str:
    """The configured password provider: ``""`` (static DSN) or ``databricks``."""
    return os.environ.get(PROVIDER_ENV, "").strip().lower()


def validate_provider() -> None:
    """Raise ``CredentialError`` for an unknown provider or missing settings."""
    name = provider()
    if not name:
        return
    if name != "databricks":
        raise CredentialError(f"unsupported {PROVIDER_ENV}")
    if not _instances():
        raise CredentialError(f"{INSTANCE_ENV} is required")
    if not _host():
        raise CredentialError("DATABRICKS_HOST is required")
    client_id = os.environ.get("DATABRICKS_CLIENT_ID", "").strip()
    client_secret = os.environ.get("DATABRICKS_CLIENT_SECRET", "").strip()
    if not (client_id and client_secret) and not os.environ.get("DATABRICKS_TOKEN", "").strip():
        raise CredentialError(
            "DATABRICKS_CLIENT_ID and DATABRICKS_CLIENT_SECRET (or DATABRICKS_TOKEN) are required"
        )


def _instances() -> list[str]:
    return [item.strip() for item in os.environ.get(INSTANCE_ENV, "").split(",") if item.strip()]


def _host() -> str:
    host = os.environ.get("DATABRICKS_HOST", "").strip().rstrip("/")
    if host and not host.startswith(("https://", "http://")):
        host = "https://" + host
    return host


def _post(url: str, data: bytes, headers: dict[str, str]) -> dict:
    """POST and decode a JSON response (blocking; run in a thread)."""
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise CredentialError(f"credential request failed with HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise CredentialError(f"credential request failed: {type(exc).__name__}") from None


def _expiry(value: object, now: float) -> float:
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return now + DEFAULT_LIFETIME_SECONDS


async def _workspace_bearer(now: float) -> str:
    client_id = os.environ.get("DATABRICKS_CLIENT_ID", "").strip()
    client_secret = os.environ.get("DATABRICKS_CLIENT_SECRET", "").strip()
    if not (client_id and client_secret):
        token = os.environ.get("DATABRICKS_TOKEN", "").strip()
        if not token:
            raise CredentialError("no Databricks credentials configured")
        return token
    if _workspace_token.fresh(now):
        return _workspace_token.value
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    payload = await asyncio.to_thread(
        _post,
        f"{_host()}/oidc/v1/token",
        urllib.parse.urlencode({"grant_type": "client_credentials", "scope": "all-apis"}).encode(),
        {
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    token = payload.get("access_token")
    if not isinstance(token, str) or not token:
        raise CredentialError("workspace token response has no access_token")
    lifetime = payload.get("expires_in")
    lifetime = lifetime if isinstance(lifetime, (int, float)) and lifetime > 0 else DEFAULT_LIFETIME_SECONDS
    _workspace_token.value = token
    _workspace_token.expires_at = now + float(lifetime)
    return token


async def database_password() -> str:
    """A current Lakebase credential; asyncpg calls this for each new connection."""
    async with _lock:
        now = time.time()
        if _database_token.fresh(now):
            return _database_token.value
        bearer = await _workspace_bearer(now)
        payload = await asyncio.to_thread(
            _post,
            f"{_host()}/api/2.0/database/credentials",
            json.dumps({"instance_names": _instances(), "request_id": str(uuid.uuid4())}).encode(),
            {
                "Authorization": f"Bearer {bearer}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        token = payload.get("token")
        if not isinstance(token, str) or not token:
            raise CredentialError("database credential response has no token")
        _database_token.value = token
        _database_token.expires_at = _expiry(payload.get("expiration_time"), now)
        logger.info(
            "Generated a Lakebase database credential valid for %d s",
            max(0, int(_database_token.expires_at - now)),
        )
        return token


def password_option():
    """The ``password`` argument for asyncpg: a provider callable, or None."""
    validate_provider()
    return database_password if provider() else None


def reset() -> None:
    """Forget cached tokens (tests)."""
    global _lock
    _lock = asyncio.Lock()
    _workspace_token.value, _workspace_token.expires_at = "", 0.0
    _database_token.value, _database_token.expires_at = "", 0.0
