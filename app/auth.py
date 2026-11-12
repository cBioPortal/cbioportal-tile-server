"""Opening the sealed slide source (`enc`) a request names alongside its key."""

import base64
import binascii
import hashlib
import json
from functools import lru_cache

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.sealed_source import (
    SEALED_SOURCE_MIN_BYTES,
    SEALED_SOURCE_NONCE_BYTES,
    SEALED_SOURCE_PATTERN,
    b64url_decode,
)


SOURCE_SEAL_KEY_BYTES = 32
# Claims that carry real slide identifiers or source bindings.  The browser
# sees only the sealed `enc`, never these values.
_SEALED_CLAIMS = ("image_id", "tile_source", "thumbnail_source")


class InvalidSealedSource(ValueError):
    """Raised when a sealed source (or the key that opens it) cannot be trusted."""


def source_digest(source: str) -> str:
    """Hash a source URL for cache keys and log correlation without exposing it."""
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def decode_source_seal_key(value: str) -> bytes:
    """Decode `WSI_SOURCE_SEAL_KEY`: standard base64 of exactly 32 random bytes.

    The key seals `enc` at the data provider and is held only by the data
    provider and this service.  Failures never echo the configured value.
    """
    if not isinstance(value, str) or not value.strip():
        raise InvalidSealedSource("WSI source seal key is not configured")
    try:
        key = base64.b64decode(value.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InvalidSealedSource("WSI source seal key is not configured") from exc
    if len(key) != SOURCE_SEAL_KEY_BYTES:
        raise InvalidSealedSource("WSI source seal key is not configured")
    return key


def decrypt_sealed_claims(enc: object, seal_key: bytes, slide_key: str) -> dict:
    """Decrypt a sealed source (`enc`) with the raw seal key, bound to `slide_key`.

    `enc` is base64url-without-padding of nonce(12) || ciphertext || tag(16)
    from AES-256-GCM with AAD = UTF-8 `slide_key`.  Failures never echo the
    ciphertext, plaintext, or slide key.
    """
    if not isinstance(seal_key, bytes) or len(seal_key) != SOURCE_SEAL_KEY_BYTES:
        raise InvalidSealedSource("WSI source seal key is not configured")
    if not isinstance(enc, str) or not SEALED_SOURCE_PATTERN.fullmatch(enc):
        raise InvalidSealedSource("invalid sealed source binding")
    return dict(_decrypt_sealed_claims_cached(enc, seal_key, slide_key))


# A viewer sends the same key+enc on hundreds of tile requests; decrypt it once.
# `enc` is sealed once per slide (random nonce), so every request for a slide
# shares one entry; failures raise and are therefore never cached.
@lru_cache(maxsize=4096)
def _decrypt_sealed_claims_cached(enc: str, seal_key: bytes, slide_key: str) -> tuple[tuple[str, str], ...]:
    try:
        sealed = b64url_decode(enc)
    except (binascii.Error, ValueError) as exc:
        raise InvalidSealedSource("invalid sealed source binding") from exc
    if len(sealed) < SEALED_SOURCE_MIN_BYTES:
        raise InvalidSealedSource("invalid sealed source binding")
    try:
        plaintext = AESGCM(seal_key).decrypt(
            sealed[:SEALED_SOURCE_NONCE_BYTES],
            sealed[SEALED_SOURCE_NONCE_BYTES:],
            slide_key.encode("utf-8"),
        )
        claims = json.loads(plaintext.decode("utf-8"))
    except (InvalidTag, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidSealedSource("invalid sealed source binding") from exc
    if not isinstance(claims, dict):
        raise InvalidSealedSource("invalid sealed source binding")
    for claim in _SEALED_CLAIMS:
        value = claims.get(claim)
        if not isinstance(value, str) or not value.strip():
            raise InvalidSealedSource("invalid sealed source binding")
    return tuple((claim, claims[claim]) for claim in _SEALED_CLAIMS)
