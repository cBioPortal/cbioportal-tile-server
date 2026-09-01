"""Validation for short-lived cBioPortal WSI access capabilities."""

import base64
import hashlib
import hmac
import json
import time


class InvalidWsiToken(ValueError):
    """Raised when a WSI capability cannot be trusted."""


def source_digest(source: str) -> str:
    """Hash the exact source URL representation signed by cBioPortal."""
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def source_cache_identity(source: str, source_fingerprint: str | None = None) -> str:
    """Return a stable cache namespace for one published source version.

    The URL is still the authorization binding.  The optional serving-manifest
    fingerprint additionally distinguishes replacements of the object at the
    same URL, which prevents Redis and local block-cache entries from serving
    bytes certified for an older source version.
    """
    fingerprint = (source_fingerprint or "").strip()
    if not fingerprint:
        return source_digest(source)
    return hashlib.sha256(
        f"{source_digest(source)}\0{fingerprint}".encode("utf-8")
    ).hexdigest()


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
    token: str, secret: str, audience: str, max_ttl: int = 300
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
        secret.encode(),
        f"{encoded_header}.{encoded_payload}".encode(),
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(expected, _b64decode(encoded_signature)):
        raise InvalidWsiToken("invalid token signature")

    now = int(time.time())
    if payload.get("aud") != audience or payload.get("scope") != "wsi:read":
        raise InvalidWsiToken("invalid token audience or scope")
    if not isinstance(payload.get("sub"), str) or not payload["sub"]:
        raise InvalidWsiToken("invalid token subject")
    if not isinstance(payload.get("study_id"), str) or not payload["study_id"].strip():
        raise InvalidWsiToken("invalid token study")
    if payload.get("wsi_auth_version") != 2:
        raise InvalidWsiToken("unsupported WSI authorization contract")
    if not isinstance(payload.get("image_id"), str) or not payload["image_id"].strip():
        raise InvalidWsiToken("invalid token image")
    for claim in ("tile_source_sha256", "thumbnail_source_sha256"):
        value = payload.get(claim)
        if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise InvalidWsiToken("invalid token source binding")
    for claim in ("tile_source_fingerprint", "thumbnail_source_fingerprint"):
        value = payload.get(claim)
        if value is not None and (
            not isinstance(value, str)
            or len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)
        ):
            raise InvalidWsiToken("invalid token source fingerprint")
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
