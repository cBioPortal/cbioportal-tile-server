"""Validation for the source-bound WSI tile metadata contract."""
from __future__ import annotations

import math
from typing import Any

from .config import settings
from .identity import TILE_METADATA_SCHEMA_VERSION, decode_policy_version


ALLOWED_TILE_METADATA_KEYS = {
    "dimensions",
    "levels",
    "level_dimensions",
    "level_downsamples",
    "max_zoom",
    "tile_size",
    "mpp",
    "objective_power",
    "vendor",
    "identity_version",
    "safe_min_level",
    "tile_metadata_schema_version",
    "decode_policy_version",
    "max_decode_pixels",
    "thumbnail_max_decode_pixels",
    "source_fingerprint",
}


def validate_tile_metadata(
    metadata: Any,
    *,
    allow_legacy: bool = True,
) -> tuple[bool, str | None]:
    """Return whether metadata is usable by the tile server.

    Schema-null records are retained for the migration window, but an
    explicitly versioned record must satisfy the complete v2 contract.
    """
    if not isinstance(metadata, dict):
        return False, "metadata_not_object"
    if set(metadata) - ALLOWED_TILE_METADATA_KEYS:
        return False, "unknown_metadata_field"

    schema = metadata.get("tile_metadata_schema_version")
    if schema is None:
        if not allow_legacy:
            return False, "legacy_metadata_not_allowed"
        return _validate_common(metadata, require_v2=False)
    if (
        not isinstance(schema, int)
        or isinstance(schema, bool)
        or schema != TILE_METADATA_SCHEMA_VERSION
    ):
        return False, "unsupported_metadata_schema"

    valid, reason = _validate_common(metadata, require_v2=True)
    if not valid:
        return valid, reason
    if metadata.get("safe_min_level") is None:
        return False, "missing_safe_min_level"
    if metadata.get("decode_policy_version") != decode_policy_version():
        return False, "stale_decode_policy"
    if (
        not isinstance(metadata.get("max_decode_pixels"), int)
        or isinstance(metadata.get("max_decode_pixels"), bool)
        or metadata.get("max_decode_pixels") != settings.max_decode_pixels
    ):
        return False, "stale_tile_decode_limit"
    if (
        not isinstance(metadata.get("thumbnail_max_decode_pixels"), int)
        or isinstance(metadata.get("thumbnail_max_decode_pixels"), bool)
        or metadata.get("thumbnail_max_decode_pixels") != settings.thumbnail_max_decode_pixels
    ):
        return False, "stale_thumbnail_decode_limit"
    return True, None


def _validate_common(metadata: dict[str, Any], *, require_v2: bool) -> tuple[bool, str | None]:
    dimensions = metadata.get("dimensions")
    if not isinstance(dimensions, dict):
        return False, "invalid_dimensions"
    width = dimensions.get("width")
    height = dimensions.get("height")
    if not _positive_int(width) or not _positive_int(height):
        return False, "invalid_dimensions"

    levels = metadata.get("levels")
    if not _positive_int(levels):
        return False, "invalid_levels"
    level_dimensions = metadata.get("level_dimensions")
    if not isinstance(level_dimensions, list) or len(level_dimensions) != levels:
        return False, "invalid_level_dimensions"
    for level in level_dimensions:
        if (
            not isinstance(level, dict)
            or not _positive_int(level.get("width"))
            or not _positive_int(level.get("height"))
        ):
            return False, "invalid_level_dimensions"

    downsample_values = metadata.get("level_downsamples")
    if require_v2:
        if not isinstance(downsample_values, list) or len(downsample_values) != levels:
            return False, "invalid_level_downsamples"
        if any(
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
            or float(value) <= 0
            for value in downsample_values
        ):
            return False, "invalid_level_downsamples"

    max_zoom = metadata.get("max_zoom")
    tile_size = metadata.get("tile_size")
    if not isinstance(max_zoom, int) or isinstance(max_zoom, bool) or max_zoom < 0:
        return False, "invalid_max_zoom"
    if not _positive_int(tile_size):
        return False, "invalid_tile_size"

    safe_min_level = metadata.get("safe_min_level")
    if safe_min_level is not None and (
        not isinstance(safe_min_level, int)
        or isinstance(safe_min_level, bool)
        or safe_min_level < 0
        or safe_min_level > max_zoom
    ):
        return False, "invalid_safe_min_level"
    return True, None


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0
