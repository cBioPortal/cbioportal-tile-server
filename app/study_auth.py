"""Study-scoped bearer tokens for the annotation, assistant and research routes.

cBioPortal signs these with ``WSI_AUTH_SECRET`` (HS256, ``typ: JWT``) from
``/api/wsi/access-token?purpose=...``: ``sub``, a string ``aud``, a
space-separated ``scope``, ``study_id`` and ``iat``/``exp`` at most
``STUDY_TOKEN_MAX_TTL`` seconds apart.  They grant study capabilities only.
Tiles and thumbnails never read them: pixels are served from the sealed
source alone, behind the ingress's study check.
"""

import hashlib
import hmac
import json
import time

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import settings
from app.sealed_source import b64url_decode

STUDY_TOKEN_MAX_TTL = 300
STUDY_TOKEN_MIN_SECRET_BYTES = 32
# Claims that bind a token to one slide or resource row.  A study token
# carries none of them, so it can never stand in for a slide binding.
_SLIDE_CLAIMS = (
    "slide_key",
    "resource_id",
    "enc",
    "wsi_auth_version",
    "image_id",
    "tile_source",
    "thumbnail_source",
)


class InvalidStudyToken(ValueError):
    """Raised when a study token is malformed, unsigned, expired or foreign."""


class InsufficientStudyScope(InvalidStudyToken):
    """Raised when a valid study token lacks a scope the route requires."""


def study_token_auth_configured() -> bool:
    """Whether ``WSI_AUTH_SECRET`` can verify study tokens."""
    return (
        len(settings.wsi_auth_secret.encode()) >= STUDY_TOKEN_MIN_SECRET_BYTES
        and bool(settings.wsi_auth_audience.strip())
    )


def study_token_auth_status() -> str:
    """``disabled`` (dev flag), ``configured`` or ``unconfigured``."""
    if not settings.annotation_auth_enabled:
        return "disabled"
    return "configured" if study_token_auth_configured() else "unconfigured"


def _b64decode(value: str) -> bytes:
    try:
        return b64url_decode(value)
    except Exception as exc:
        raise InvalidStudyToken("invalid token encoding") from exc


def _verified_payload(token: str, secret: str) -> dict:
    """Return the payload of an HS256 JWT after checking its signature."""
    parts = token.split(".")
    if len(parts) != 3:
        raise InvalidStudyToken("invalid token")
    encoded_header, encoded_payload, encoded_signature = parts
    try:
        header = json.loads(_b64decode(encoded_header))
        payload = json.loads(_b64decode(encoded_payload))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidStudyToken("invalid token payload") from exc
    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise InvalidStudyToken("invalid token structure")
    if header.get("alg") != "HS256" or header.get("typ") != "JWT":
        raise InvalidStudyToken("unsupported token algorithm")
    expected = hmac.new(
        secret.encode(),
        f"{encoded_header}.{encoded_payload}".encode(),
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(expected, _b64decode(encoded_signature)):
        raise InvalidStudyToken("invalid token signature")
    return payload


def validate_study_token(
    token: str,
    secret: str,
    audience: str,
    required_scopes: set[str],
    max_ttl: int = STUDY_TOKEN_MAX_TTL,
) -> dict:
    """Verify a study token and require every ``required_scopes`` entry.

    Raises ``InsufficientStudyScope`` only for an otherwise valid token, so
    callers can answer 403 rather than 401.
    """
    if not secret or len(secret.encode()) < STUDY_TOKEN_MIN_SECRET_BYTES:
        raise InvalidStudyToken("study token authentication is not configured")
    if not audience or not audience.strip() or not 1 <= max_ttl <= STUDY_TOKEN_MAX_TTL:
        raise InvalidStudyToken("study token authentication is not configured")
    payload = _verified_payload(token, secret)

    if payload.get("aud") != audience:
        raise InvalidStudyToken("invalid token audience")
    if not isinstance(payload.get("scope"), str):
        raise InvalidStudyToken("invalid token scope")
    if any(claim in payload for claim in _SLIDE_CLAIMS):
        raise InvalidStudyToken("study token names a slide")
    if not isinstance(payload.get("sub"), str) or not payload["sub"]:
        raise InvalidStudyToken("invalid token subject")
    if not isinstance(payload.get("study_id"), str) or not payload["study_id"].strip():
        raise InvalidStudyToken("invalid token study")
    now = int(time.time())
    if type(payload.get("exp")) is not int or payload["exp"] <= now:
        raise InvalidStudyToken("expired token")
    if type(payload.get("iat")) is not int or payload["iat"] > now + 60:
        raise InvalidStudyToken("invalid token issued-at")
    if payload["exp"] <= payload["iat"] or payload["exp"] - payload["iat"] > max_ttl:
        raise InvalidStudyToken("token lifetime exceeds configured maximum")

    scopes = payload["scope"].split()
    if not required_scopes or not required_scopes.issubset(scopes):
        raise InsufficientStudyScope("token lacks a required scope")
    return payload


_bearer = HTTPBearer(auto_error=False)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _authenticate_study(
    creds: HTTPAuthorizationCredentials | None, required_scopes: set[str]
) -> dict:
    if not settings.annotation_auth_enabled:
        return {"sub": "dev-user", "groups": [], "study_id": None, "scopes": set(required_scopes)}
    if not study_token_auth_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Study token authentication is not configured",
        )
    if creds is None:
        raise _unauthorized("Missing Authorization header")
    insufficient_scope = False
    for secret in (settings.wsi_auth_secret, settings.wsi_auth_previous_secret):
        if not secret:
            continue
        try:
            token = validate_study_token(
                creds.credentials, secret, settings.wsi_auth_audience, required_scopes
            )
        except InsufficientStudyScope:
            insufficient_scope = True
            break
        except InvalidStudyToken:
            continue
        return {
            "sub": token["sub"],
            "groups": [],
            "study_id": token["study_id"],
            "scopes": set(token["scope"].split()),
        }
    if insufficient_scope:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Study token lacks the required scope",
        )
    raise _unauthorized("Invalid study token")


def study_scope_dependency(required_scopes: set[str]):
    """FastAPI dependency requiring a study token with ``required_scopes``."""

    async def dependency(
        creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    ) -> dict:
        return _authenticate_study(creds, required_scopes)

    return dependency


async def require_user(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict:
    """Return the subject and study of an annotation study token.

    Reads need ``annotations:read``; writes also need ``annotations:write``.
    """
    required_scopes = {"annotations:read"}
    if request.method in {"POST", "PUT", "DELETE"}:
        required_scopes.add("annotations:write")
    return _authenticate_study(creds, required_scopes)
