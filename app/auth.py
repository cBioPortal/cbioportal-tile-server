"""Validation for short-lived cBioPortal WSI access capabilities."""

import base64
import binascii
import hashlib
import hmac
import json
import time
from functools import lru_cache

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.deid import (
    SEALED_SOURCE_MIN_BYTES,
    SEALED_SOURCE_NONCE_BYTES,
    SEALED_SOURCE_PATTERN,
    SLIDE_KEY_PATTERN,
    b64url_decode,
)
from app.config import settings


WSI_AUTH_VERSION = 4
SOURCE_SEAL_KEY_BYTES = 32
# Claims that carry real slide identifiers or source bindings.  A JWT
# payload is readable by the browser, so these may only travel inside `enc`.
_SEALED_CLAIMS = ("image_id", "tile_source", "thumbnail_source")


class InvalidWsiToken(ValueError):
    """Raised when a WSI capability cannot be trusted."""


def source_digest(source: str) -> str:
    """Hash a source URL for cache keys and log correlation without exposing it."""
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
        return b64url_decode(value)
    except Exception as exc:
        raise InvalidWsiToken("invalid token encoding") from exc


def decode_source_seal_key(value: str) -> bytes:
    """Decode `WSI_SOURCE_SEAL_KEY`: standard base64 of exactly 32 random bytes.

    The key seals `enc` at the data provider and is never derived from
    `WSI_AUTH_SECRET`.  Failures never echo the configured value.
    """
    if not isinstance(value, str) or not value.strip():
        raise InvalidWsiToken("WSI source seal key is not configured")
    try:
        key = base64.b64decode(value.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InvalidWsiToken("WSI source seal key is not configured") from exc
    if len(key) != SOURCE_SEAL_KEY_BYTES:
        raise InvalidWsiToken("WSI source seal key is not configured")
    return key


def decrypt_sealed_claims(enc: object, seal_key: bytes, slide_key: str) -> dict:
    """Decrypt the `enc` claim with the raw source seal key, bound to `slide_key`.

    `enc` is base64url-without-padding of nonce(12) || ciphertext || tag(16)
    from AES-256-GCM with AAD = UTF-8 `slide_key`.  Failures never echo the
    ciphertext, plaintext, or slide key.
    """
    if not isinstance(enc, str) or not SEALED_SOURCE_PATTERN.fullmatch(enc):
        raise InvalidWsiToken("invalid token source binding")
    return dict(_decrypt_sealed_claims_cached(enc, seal_key, slide_key))


# One token authorizes hundreds of tile requests; decrypt its `enc` once. The
# cache is consulted only after the signature/expiry checks pass on every
# request. `enc` is sealed once per slide (random nonce), so every token for a
# slide shares one entry; failures raise and are therefore never cached.
@lru_cache(maxsize=4096)
def _decrypt_sealed_claims_cached(enc: str, seal_key: bytes, slide_key: str) -> tuple[tuple[str, str], ...]:
    try:
        sealed = b64url_decode(enc)
    except (binascii.Error, ValueError) as exc:
        raise InvalidWsiToken("invalid token source binding") from exc
    if len(sealed) < SEALED_SOURCE_MIN_BYTES:
        raise InvalidWsiToken("invalid token source binding")
    try:
        plaintext = AESGCM(seal_key).decrypt(
            sealed[:SEALED_SOURCE_NONCE_BYTES],
            sealed[SEALED_SOURCE_NONCE_BYTES:],
            slide_key.encode("utf-8"),
        )
        claims = json.loads(plaintext.decode("utf-8"))
    except (InvalidTag, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidWsiToken("invalid token source binding") from exc
    if not isinstance(claims, dict):
        raise InvalidWsiToken("invalid token source binding")
    for claim in _SEALED_CLAIMS:
        value = claims.get(claim)
        if not isinstance(value, str) or not value.strip():
            raise InvalidWsiToken("invalid token source binding")
    return tuple((claim, claims[claim]) for claim in _SEALED_CLAIMS)


def validate_wsi_auth_configuration(
    secret: str, audience: str, max_ttl: int, seal_key: bytes
) -> None:
    if not secret or len(secret.encode()) < 32:
        raise InvalidWsiToken("WSI authentication is not configured")
    if not audience or not audience.strip() or not 1 <= max_ttl <= 300:
        raise InvalidWsiToken("WSI authentication is not configured")
    if not isinstance(seal_key, bytes) or len(seal_key) != SOURCE_SEAL_KEY_BYTES:
        raise InvalidWsiToken("WSI source seal key is not configured")


def _verified_payload(token: str, secret: str) -> dict:
    """Return the payload of an HS256 JWT after checking its signature."""
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
    return payload


def _check_lifetime(payload: dict, max_ttl: int, now: int) -> None:
    if type(payload.get("exp")) is not int or payload["exp"] <= now:
        raise InvalidWsiToken("expired token")
    if type(payload.get("iat")) is not int or payload["iat"] > now + 60:
        raise InvalidWsiToken("invalid token issued-at")
    if payload["exp"] <= payload["iat"] or payload["exp"] - payload["iat"] > max_ttl:
        raise InvalidWsiToken("token lifetime exceeds configured maximum")


def validate_wsi_token(
    token: str, secret: str, audience: str, max_ttl: int = 300, *, seal_key: bytes
) -> dict:
    """Verify a capability signed with `secret` and open its sealed source."""
    validate_wsi_auth_configuration(secret, audience, max_ttl, seal_key)
    payload = _verified_payload(token, secret)

    now = int(time.time())
    if payload.get("aud") != audience or payload.get("scope") != "wsi:read":
        raise InvalidWsiToken("invalid token audience or scope")
    if not isinstance(payload.get("sub"), str) or not payload["sub"]:
        raise InvalidWsiToken("invalid token subject")
    if not isinstance(payload.get("study_id"), str) or not payload["study_id"].strip():
        raise InvalidWsiToken("invalid token study")
    if payload.get("wsi_auth_version") != WSI_AUTH_VERSION:
        raise InvalidWsiToken("unsupported WSI authorization contract")
    slide_key = payload.get("slide_key")
    if not isinstance(slide_key, str) or not SLIDE_KEY_PATTERN.fullmatch(slide_key):
        raise InvalidWsiToken("invalid token slide")
    if any(claim in payload for claim in _SEALED_CLAIMS):
        raise InvalidWsiToken("token exposes sealed slide claims")
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
    _check_lifetime(payload, max_ttl, now)

    # Decrypt last so expired or mis-scoped tokens never reach the cipher.
    sealed = decrypt_sealed_claims(payload.get("enc"), seal_key, slide_key)
    claims = {key: value for key, value in payload.items() if key != "enc"}
    claims.update(sealed)
    return claims


# Claims that bind a capability to one slide.  Study-scoped capabilities
# (annotations, the assistant) must carry none of them, so a leaked one can
# never open tiles and a slide capability can never write annotations.
_SLIDE_CLAIMS = ("slide_key", "enc", "wsi_auth_version") + _SEALED_CLAIMS


def validate_scoped_capability(
    token: str,
    secret: str,
    audience: str,
    required_scopes: set[str],
    max_ttl: int = 300,
) -> dict:
    """Verify a study-scoped capability that names no slide.

    cBioPortal issues these from ``/access-token?purpose=...``: ``scope`` is a
    space-separated list that must include every ``required_scopes`` entry.
    """
    if not secret or len(secret.encode()) < 32:
        raise InvalidWsiToken("WSI authentication is not configured")
    if not audience or not audience.strip() or not 1 <= max_ttl <= 300:
        raise InvalidWsiToken("WSI authentication is not configured")
    payload = _verified_payload(token, secret)

    scopes = str(payload.get("scope", "")).split()
    if payload.get("aud") != audience or "wsi:read" in scopes:
        raise InvalidWsiToken("invalid token audience or scope")
    if not required_scopes or not required_scopes.issubset(scopes):
        raise InvalidWsiToken("invalid token audience or scope")
    if any(claim in payload for claim in _SLIDE_CLAIMS):
        raise InvalidWsiToken("study capability names a slide")
    if not isinstance(payload.get("sub"), str) or not payload["sub"]:
        raise InvalidWsiToken("invalid token subject")
    if not isinstance(payload.get("study_id"), str) or not payload["study_id"].strip():
        raise InvalidWsiToken("invalid token study")
    _check_lifetime(payload, max_ttl, int(time.time()))
    return payload


_bearer = HTTPBearer(auto_error=False)


def _authenticate_scoped(
    creds: HTTPAuthorizationCredentials | None, required_scopes: set[str]
) -> dict:
    if not settings.annotation_auth_enabled:
        return {"sub": "dev-user", "groups": [], "study_id": None, "scopes": set(required_scopes)}
    if creds is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    capability = None
    for secret in (settings.wsi_auth_secret, settings.wsi_auth_previous_secret):
        if not secret:
            continue
        try:
            capability = validate_scoped_capability(
                creds.credentials,
                secret,
                settings.wsi_auth_audience,
                required_scopes,
                settings.wsi_auth_max_ttl,
            )
            break
        except InvalidWsiToken:
            continue
    if capability is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid study capability",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return {
        "sub": capability["sub"],
        "groups": [],
        "study_id": capability["study_id"],
        "scopes": set(str(capability["scope"]).split()),
    }


def scoped_user_dependency(required_scopes: set[str]):
    """FastAPI dependency requiring a study capability with ``required_scopes``."""

    async def dependency(
        creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    ) -> dict:
        return _authenticate_scoped(creds, required_scopes)

    return dependency


async def require_user(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict:
    """Return the subject and study scope from an annotation capability."""
    required_scopes = {"annotations:read"}
    if request.method in {"POST", "PUT", "DELETE"}:
        required_scopes.add("annotations:write")
    return _authenticate_scoped(creds, required_scopes)
