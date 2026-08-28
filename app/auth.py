"""Authentication helpers for WSI capability and annotation API requests."""

import base64
import hashlib
import hmac
import json
import time

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import settings


class InvalidWsiToken(ValueError):
    """Raised when a WSI capability cannot be trusted."""


def source_digest(source: str) -> str:
    """Hash the exact source URL representation signed by cBioPortal."""
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _b64decode(value: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as exc:
        raise InvalidWsiToken("invalid token encoding") from exc


def validate_wsi_auth_configuration(secret: str, audience: str, max_ttl: int) -> None:
    if not secret or len(secret.encode()) < 32:
        raise InvalidWsiToken("WSI authentication is not configured")
    if not audience or not audience.strip() or not 1 <= max_ttl <= 300:
        raise InvalidWsiToken("WSI authentication is not configured")


def validate_wsi_token(
    token: str,
    secret: str,
    audience: str,
    max_ttl: int = 300,
    expected_study_id: str | None = None,
    required_scopes: set[str] | None = None,
) -> dict:
    validate_wsi_auth_configuration(secret, audience, max_ttl)
    parts = token.split(".")
    if len(parts) != 3:
        raise InvalidWsiToken("invalid token")
    encoded_header, encoded_payload, encoded_signature = parts
    try:
        header = json.loads(_b64decode(encoded_header))
        payload = json.loads(_b64decode(encoded_payload))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidWsiToken("invalid token payload") from exc
    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise InvalidWsiToken("invalid token structure")
    if header.get("alg") != "HS256" or header.get("typ") != "JWT":
        raise InvalidWsiToken("unsupported token algorithm")
    expected = hmac.new(
        secret.encode(), f"{encoded_header}.{encoded_payload}".encode(), hashlib.sha256
    ).digest()
    if not hmac.compare_digest(expected, _b64decode(encoded_signature)):
        raise InvalidWsiToken("invalid token signature")
    now = int(time.time())
    scopes = set(str(payload.get("scope", "")).split())
    required = required_scopes or {"wsi:read"}
    if payload.get("aud") != audience or not required.issubset(scopes):
        raise InvalidWsiToken("invalid token audience or scope")
    if not isinstance(payload.get("sub"), str) or not payload["sub"]:
        raise InvalidWsiToken("invalid token subject")
    if not isinstance(payload.get("study_id"), str) or not payload["study_id"].strip():
        raise InvalidWsiToken("invalid token study")
    if expected_study_id is not None and payload["study_id"] != expected_study_id:
        raise InvalidWsiToken("token study scope does not match request")
    if "wsi:read" in required:
        if payload.get("wsi_auth_version") != 2:
            raise InvalidWsiToken("unsupported WSI authorization contract")
        if (
            not isinstance(payload.get("image_id"), str)
            or not payload["image_id"].strip()
        ):
            raise InvalidWsiToken("invalid token image")
        for claim in ("tile_source_sha256", "thumbnail_source_sha256"):
            value = payload.get(claim)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(char not in "0123456789abcdef" for char in value)
            ):
                raise InvalidWsiToken("invalid token source binding")
        for claim in ("thumbnail_width", "thumbnail_height"):
            value = payload.get(claim)
            if type(value) is not int or value <= 0 or value > 8192:
                raise InvalidWsiToken("invalid token thumbnail dimensions")
    if type(payload.get("exp")) is not int or payload["exp"] <= now:
        raise InvalidWsiToken("expired token")
    if type(payload.get("iat")) is not int or payload["iat"] > now + 60:
        raise InvalidWsiToken("invalid token issued-at")
    if payload["exp"] <= payload["iat"] or payload["exp"] - payload["iat"] > max_ttl:
        raise InvalidWsiToken("token lifetime exceeds configured maximum")
    return payload


_bearer = HTTPBearer(auto_error=False)


async def require_user(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict:
    """Return the subject and study scope from a cBioPortal capability."""
    if not settings.annotation_auth_enabled:
        return {"sub": "dev-user", "groups": []}
    if creds is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    required_scopes = {"annotations:read"}
    if request.method in {"POST", "PUT", "DELETE"}:
        required_scopes.add("annotations:write")
    try:
        capability = validate_wsi_token(
            creds.credentials,
            settings.wsi_auth_secret,
            settings.wsi_auth_audience,
            required_scopes=required_scopes,
        )
        return {
            "sub": capability["sub"],
            "groups": [],
            "study_id": capability["study_id"],
        }
    except InvalidWsiToken as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid annotation capability",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
