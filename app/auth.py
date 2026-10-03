"""Validation for short-lived cBioPortal WSI access capabilities."""

import base64
import binascii
import hashlib
import hmac
import json
import re
import time
from functools import lru_cache

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.deid import SLIDE_KEY_PATTERN


WSI_AUTH_VERSION = 3
CLAIM_ENCRYPTION_INFO = b"wsi-claim-enc-v3"
_GCM_NONCE_BYTES = 12
_GCM_TAG_BYTES = 16
_BASE64URL = re.compile(r"[A-Za-z0-9_-]+")
# Claims that carry real slide identifiers or source bindings.  A v3 JWT
# payload is readable by the browser, so these may only travel inside `enc`.
_PLAINTEXT_FORBIDDEN_CLAIMS = (
    "image_id",
    "tile_source",
    "thumbnail_source",
    "tile_source_sha256",
    "thumbnail_source_sha256",
)
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
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as exc:
        raise InvalidWsiToken("invalid token encoding") from exc


@lru_cache(maxsize=4)
def _claim_encryption_key(secret: str) -> bytes:
    """Derive the AES-256-GCM key for sealed claims (contract section 4)."""
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=CLAIM_ENCRYPTION_INFO,
    ).derive(secret.encode("utf-8"))


def decrypt_sealed_claims(enc: object, secret: str, slide_key: str) -> dict:
    """Decrypt the `enc` claim bound to `slide_key`.

    Failures never echo the ciphertext, plaintext, or slide key.
    """
    if not isinstance(enc, str) or not _BASE64URL.fullmatch(enc):
        raise InvalidWsiToken("invalid token source binding")
    return dict(_decrypt_sealed_claims_cached(enc, secret, slide_key))


# One token authorizes hundreds of tile requests; decrypt its `enc` once. The
# cache is consulted only after the signature/expiry checks pass on every
# request, ciphertexts are unique per token (random nonce), and failures raise
# and are therefore never cached.
@lru_cache(maxsize=4096)
def _decrypt_sealed_claims_cached(enc: str, secret: str, slide_key: str) -> tuple[tuple[str, str], ...]:
    try:
        sealed = base64.urlsafe_b64decode(enc + "=" * (-len(enc) % 4))
    except (binascii.Error, ValueError) as exc:
        raise InvalidWsiToken("invalid token source binding") from exc
    if len(sealed) <= _GCM_NONCE_BYTES + _GCM_TAG_BYTES:
        raise InvalidWsiToken("invalid token source binding")
    try:
        plaintext = AESGCM(_claim_encryption_key(secret)).decrypt(
            sealed[:_GCM_NONCE_BYTES],
            sealed[_GCM_NONCE_BYTES:],
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
    if payload.get("wsi_auth_version") != WSI_AUTH_VERSION:
        raise InvalidWsiToken("unsupported WSI authorization contract")
    slide_key = payload.get("slide_key")
    if not isinstance(slide_key, str) or not SLIDE_KEY_PATTERN.fullmatch(slide_key):
        raise InvalidWsiToken("invalid token slide")
    if any(claim in payload for claim in _PLAINTEXT_FORBIDDEN_CLAIMS):
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
    if type(payload.get("exp")) is not int or payload["exp"] <= now:
        raise InvalidWsiToken("expired token")
    if type(payload.get("iat")) is not int or payload["iat"] > now + 60:
        raise InvalidWsiToken("invalid token issued-at")
    if payload["exp"] <= payload["iat"] or payload["exp"] - payload["iat"] > max_ttl:
        raise InvalidWsiToken("token lifetime exceeds configured maximum")

    # Decrypt last so expired or mis-scoped tokens never reach the cipher.
    sealed = decrypt_sealed_claims(payload.get("enc"), secret, slide_key)
    claims = {key: value for key, value in payload.items() if key != "enc"}
    claims.update(sealed)
    return claims
