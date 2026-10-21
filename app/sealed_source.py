"""Shape of the sealed slide source carried in a capability's `enc` claim.

The data provider seals `{image_id, tile_source, thumbnail_source}` with
WSI_SOURCE_SEAL_KEY (AAD = slide_key); only this server can open it. These
definitions are also imported by the provider's study-file writer so the two
cannot drift.
"""

from __future__ import annotations

import base64
import re

SLIDE_KEY_PATTERN = re.compile(r"^[0-9a-f]{12}$")
# SEALED_SOURCE is base64url without padding of nonce(12) || AES-256-GCM
# ciphertext (at least one byte) || tag(16).
SEALED_SOURCE_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
SEALED_SOURCE_MAX_CHARS = 4096
SEALED_SOURCE_NONCE_BYTES = 12
SEALED_SOURCE_TAG_BYTES = 16
SEALED_SOURCE_MIN_BYTES = SEALED_SOURCE_NONCE_BYTES + SEALED_SOURCE_TAG_BYTES + 1


def b64url_decode(value: str) -> bytes:
    """Decode base64url with or without padding; raises binascii.Error/ValueError."""
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
