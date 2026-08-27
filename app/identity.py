"""Source identity and decode-policy helpers shared by runtime tooling."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import re
from typing import Any


IDENTITY_VERSION = "v2"
TILE_METADATA_SCHEMA_VERSION = 2


def timestamp_to_epoch_ms(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        numeric = int(value)
        return numeric if abs(numeric) >= 100_000_000_000 else numeric * 1000
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"-?\d+(?:\.\d+)?", text):
        numeric = int(float(text))
        return numeric if abs(numeric) >= 100_000_000_000 else numeric * 1000
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.astimezone(timezone.utc).timestamp() * 1000)


def source_fingerprint(path: Any, size: Any, last_modified: Any) -> str | None:
    if path in (None, "") or size in (None, "") or last_modified in (None, ""):
        return None
    payload = (
        f"{IDENTITY_VERSION}||{str(path)}||{int(size)}||"
        f"{timestamp_to_epoch_ms(last_modified)}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def decode_policy_version() -> str:
    return f"geometry-v2;tile-max={settings.max_decode_pixels};thumbnail-max={settings.thumbnail_max_decode_pixels}"


from .config import settings  # noqa: E402  (settings is needed by policy helper)
