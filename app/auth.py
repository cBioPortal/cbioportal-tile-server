"""Validation for short-lived cBioPortal WSI access capabilities."""

import base64
import hashlib
import hmac
import json
import time


class InvalidWsiToken(ValueError):
    """Raised when a WSI capability cannot be trusted."""


def _b64decode(value: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as exc:
        raise InvalidWsiToken("invalid token encoding") from exc


def validate_wsi_token(
    token: str, secret: str, audience: str, max_ttl: int = 300
) -> dict:
    if not secret or len(secret.encode()) < 32:
        raise InvalidWsiToken("WSI authentication is not configured")
    if not audience or not audience.strip() or max_ttl < 1:
        raise InvalidWsiToken("WSI authentication is not configured")

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
    if type(payload.get("wsi_auth_version")) is not int or payload["wsi_auth_version"] != 1:
        raise InvalidWsiToken("unsupported WSI authorization contract")
    if type(payload.get("exp")) is not int or payload["exp"] <= now:
        raise InvalidWsiToken("expired token")
    if type(payload.get("iat")) is not int or payload["iat"] > now + 60:
        raise InvalidWsiToken("invalid token issued-at")
    if payload["exp"] <= payload["iat"] or payload["exp"] - payload["iat"] > max_ttl:
        raise InvalidWsiToken("token lifetime exceeds configured maximum")

    return payload
