"""Fail-closed checks for the de-identified WSI publication contract."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from urllib.parse import unquote, urlsplit


_ABSOLUTE_DATE = re.compile(
    r"(?<!\d)(?:19|20)\d{2}[-_/](?:0?[1-9]|1[0-2])[-_/](?:0?[1-9]|[12]\d|3[01])(?!\d)"
)
_COMPACT_DATE = re.compile(r"(?<!\d)(?:19|20)\d{6}(?!\d)")
_LABELLED_MRN = re.compile(r"(?i)\b(?:mrn|medical[ _-]?record(?:[ _-]?number)?)\b\s*[:=#-]?\s*\d{4,}")
_URI_EXTENSION = {
    "source": {".svs", ".tif", ".tiff", ".ndpi", ".mrxs", ".scn"},
    "thumbnail": {".jpg", ".jpeg", ".png"},
}
_FORBIDDEN_FIELDS = {
    "mrn",
    "mrn_id",
    "medical_record_number",
    "medical_record_id",
    "patient_mrn",
    "procedure_date",
    "diagnosis_date",
    "date_at_first_icdo_dx",
    "release_id",
    "procedure_date_days",
}
_APPROVED_IDENTIFIER_FIELDS = {
    "patient_id",
    "reference_sample_id",
    "sample_id",
    "image_id",
}
_WSI_NON_TEXT_FIELDS = {
    "is_hne",
    "is_ihc",
    "can_serve_tiles",
    "file_size_bytes",
    "thumbnail_width",
    "thumbnail_height",
    "tile_metadata_json",
}
_THUMBNAIL_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}
_TILE_METADATA_FIELDS = {
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


class DeidViolation(ValueError):
    """Raised when a public WSI/timeline row would violate the contract."""


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


def _assert_safe_text(field: str, value: object) -> None:
    text = _text(value)
    normalized_field = re.sub(r"[^a-z0-9]+", "_", field.lower()).strip("_")
    if (
        normalized_field in _FORBIDDEN_FIELDS
        or normalized_field.endswith("_mrn")
        or normalized_field.endswith("_medical_record_number")
        or normalized_field in {"date", "procedure_dt", "diagnosis_dt"}
    ):
        raise DeidViolation(f"forbidden de-id field: {field}")
    if not text:
        return
    if _LABELLED_MRN.search(text):
        raise DeidViolation(f"labelled MRN in {field}")
    if _ABSOLUTE_DATE.search(text) or _COMPACT_DATE.search(text):
        raise DeidViolation(f"absolute date in {field}")


def _assert_safe_metadata_text(value: object, field: str = "TILE_METADATA_JSON") -> None:
    """Scan only JSON string values; numeric geometry is not date text."""
    if isinstance(value, str):
        _assert_safe_text(field, value)
    elif isinstance(value, Mapping):
        for key, child in value.items():
            _assert_safe_metadata_text(child, f"{field}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_safe_metadata_text(child, f"{field}[{index}]")


def _validate_thumbnail_content_type(uri: object, content_type: object) -> None:
    value = _text(uri)
    media_type = _text(content_type).lower()
    if not value or not media_type:
        return
    try:
        path = unquote(unquote(urlsplit(value).path))
    except ValueError as error:
        raise DeidViolation("malformed thumbnail URI") from error
    extension = "." + path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
    expected = _THUMBNAIL_CONTENT_TYPES.get(extension)
    if expected is None or media_type != expected:
        raise DeidViolation("thumbnail content type does not match URI")


def _uri_is_under_prefix(uri: str, prefixes: Iterable[str]) -> bool:
    normalized = uri.rstrip("/")
    return any(normalized.startswith(prefix.rstrip("/") + "/") for prefix in prefixes)


def validate_artifact_uri(
    uri: object,
    *,
    image_id: str,
    kind: str,
    prefixes: Iterable[str] = (),
    related_identifiers: Iterable[str] = (),
) -> None:
    """Validate a source/thumbnail URI without exposing source identifiers."""
    value = _text(uri)
    if not value:
        return
    if kind not in _URI_EXTENSION:
        raise DeidViolation(f"unknown URI kind: {kind}")
    try:
        parsed = urlsplit(value)
    except ValueError as error:
        raise DeidViolation(f"malformed {kind} URI") from error
    if not parsed.scheme or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise DeidViolation(f"unsafe {kind} URI")
    if parsed.scheme.lower() not in {"s3", "file"}:
        raise DeidViolation(f"unsupported {kind} URI scheme")
    decoded_path = unquote(unquote(parsed.path))
    if not decoded_path or decoded_path.endswith("/"):
        raise DeidViolation(f"malformed {kind} URI")
    if parsed.scheme.lower() == "s3" and not parsed.netloc:
        raise DeidViolation(f"malformed {kind} URI")
    if parsed.scheme.lower() == "file":
        if parsed.netloc not in {"", "localhost"} or not decoded_path.startswith("/"):
            raise DeidViolation(f"malformed {kind} URI")
    if any(segment in {".", ".."} for segment in decoded_path.split("/")):
        raise DeidViolation(f"unsafe {kind} URI path")
    if prefixes and not _uri_is_under_prefix(value, prefixes):
        raise DeidViolation(f"unapproved {kind} URI prefix")

    filename = decoded_path.rsplit("/", 1)[-1]
    stem, dot, extension = filename.rpartition(".")
    if not dot or not stem or f".{extension.lower()}" not in _URI_EXTENSION[kind]:
        raise DeidViolation(f"{kind} URI basename is not image-scoped")
    # Source slides may retain scanner filenames and legacy thumbnail manifests
    # may use a different artifact key. The approved prefix and identifier
    # checks below are the privacy boundary; they must not be bypassed by a
    # user-controlled query or path traversal.
    lowered = (value + " " + decoded_path).lower()
    if (
        _ABSOLUTE_DATE.search(value)
        or _ABSOLUTE_DATE.search(decoded_path)
        or _COMPACT_DATE.search(value)
        or _COMPACT_DATE.search(decoded_path)
        or _LABELLED_MRN.search(value)
        or _LABELLED_MRN.search(decoded_path)
    ):
        raise DeidViolation(f"identifier/date in {kind} URI")
    for identifier in related_identifiers:
        token = _text(identifier)
        if token and token.lower() in lowered:
            raise DeidViolation(f"related identifier in {kind} URI")


def validate_wsi_public_row(
    row: Mapping[str, object],
    *,
    source_prefixes: Iterable[str] = (),
    thumbnail_prefixes: Iterable[str] = (),
) -> None:
    """Validate the exact fields projected into a public WSI study row."""
    image_id = _text(row.get("IMAGE_ID"))
    if not image_id:
        raise DeidViolation("IMAGE_ID is required")
    for field, value in row.items():
        normalized_field = field.lower()
        if normalized_field not in _APPROVED_IDENTIFIER_FIELDS and normalized_field not in _WSI_NON_TEXT_FIELDS:
            _assert_safe_text(field, value)
        if field.upper() == "TILE_METADATA_JSON" and _text(value):
            try:
                metadata = json.loads(_text(value))
            except json.JSONDecodeError as error:
                raise DeidViolation("invalid TILE_METADATA_JSON") from error
            if not isinstance(metadata, dict):
                raise DeidViolation("TILE_METADATA_JSON must be an object")
            # Keep the small legacy fixture shape readable during migration;
            # production registry rows use the explicit allowlisted schema.
            unknown = set(metadata) - _TILE_METADATA_FIELDS
            if unknown and not set(metadata) <= {"width", "height"}:
                raise DeidViolation("unknown TILE_METADATA_JSON field")
            _assert_safe_metadata_text(metadata)
    related = (
        row.get("PATIENT_ID"),
        row.get("REFERENCE_SAMPLE_ID"),
        row.get("SAMPLE_ID"),
        row.get("BARCODE"),
    )
    validate_artifact_uri(
        row.get("SOURCE_URL"),
        image_id=image_id,
        kind="source",
        prefixes=source_prefixes,
        related_identifiers=related,
    )
    _validate_thumbnail_content_type(row.get("THUMBNAIL_URL"), row.get("THUMBNAIL_CONTENT_TYPE"))
    validate_artifact_uri(
        row.get("THUMBNAIL_URL"),
        image_id=image_id,
        kind="thumbnail",
        prefixes=thumbnail_prefixes,
        related_identifiers=related,
    )


def validate_timeline_public_row(row: Mapping[str, object]) -> None:
    """Validate a timeline event, which may contain only relative timing."""
    forbidden = {"MRN", "DATE", "DIAGNOSIS_DATE", "PROCEDURE_DATE"}
    for field, value in row.items():
        if field.upper() in forbidden or field.lower() in _FORBIDDEN_FIELDS:
            raise DeidViolation(f"forbidden timeline field: {field}")
        if field.upper() not in {"PATIENT_ID", "SAMPLE_ID"}:
            _assert_safe_text(field, value)
    for field in ("START_DATE", "STOP_DATE"):
        value = _text(row.get(field))
        if value and not re.fullmatch(r"-?\d+", value):
            raise DeidViolation(f"timeline {field} must be a relative integer")
        if value and _COMPACT_DATE.fullmatch(value.lstrip("-")):
            raise DeidViolation(f"timeline {field} must be relative, not an absolute date")
