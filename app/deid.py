"""Fail-closed checks for the de-identified WSI publication contract.

These checks are institution-neutral (dates, labelled MRNs, opaque slide keys).
Site-specific identifier formats, such as specimen accession numbers, are the
data provider's to reject before rows are published.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Iterable, Mapping
from urllib.parse import unquote, urlsplit


_ABSOLUTE_DATE = re.compile(
    r"(?<!\d)(?:19|20)\d{2}[-_/](?:0?[1-9]|1[0-2])[-_/](?:0?[1-9]|[12]\d|3[01])(?!\d)"
)
_MONTH_FIRST_DATE = re.compile(
    r"(?<!\d)(?:0?[1-9]|1[0-2])[-_/](?:0?[1-9]|[12]\d|3[01])[-_/](?:19|20)\d{2}(?!\d)"
)
_DAY_FIRST_DATE = re.compile(
    r"(?<!\d)(?:0?[1-9]|[12]\d|3[01])[-_/](?:0?[1-9]|1[0-2])[-_/](?:19|20)\d{2}(?!\d)"
)
_NAMED_MONTH_DATE = re.compile(
    r"(?i)(?<![a-z0-9])(?:(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|"
    r"may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?)\s+(?:0?[1-9]|[12]\d|3[01])(?:st|nd|rd|th)?"
    r"(?:,)?\s+(?:19|20)\d{2}|(?:0?[1-9]|[12]\d|3[01])[-/\s]+"
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?)[-/\s]+(?:19|20)\d{2})(?![a-z0-9])"
)
_COMPACT_DATE = re.compile(r"(?<!\d)(?:19|20)\d{6}(?!\d)")
_LABELLED_MRN = re.compile(r"(?i)\b(?:mrn|medical[ _-]?record(?:[ _-]?number)?)\b\s*[:=#-]?\s*\d{4,}")
SLIDE_KEY_PATTERN = re.compile(r"^[0-9a-f]{32}$")
# SEALED_SOURCE is base64url without padding of nonce(12) || AES-256-GCM
# ciphertext (at least one byte) || tag(16), as carried in the capability's
# `enc` claim.
SEALED_SOURCE_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
SEALED_SOURCE_MAX_CHARS = 4096
SEALED_SOURCE_MIN_BYTES = 12 + 16 + 1
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
# Identifiers allowed to reach the browser without date/MRN text scanning.
# slide_key is opaque hex and validated by SLIDE_KEY_PATTERN; image_id is
# server-side only (contract wsi-serving-v6) and never appears in a public row.
_APPROVED_IDENTIFIER_FIELDS = {
    "patient_id",
    "reference_sample_id",
    "sample_id",
    "slide_key",
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
_THUMBNAIL_CONTENT_TYPES = {"image/jpeg", "image/png"}
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


_SHA256_HEX = re.compile(r"[0-9a-fA-F]{64}")
# Columns that must never reach cBioPortal (data_wsi.txt format v4): image_id
# and the source/thumbnail URIs travel only inside SEALED_SOURCE and are
# validated by validate_artifact_uri when the tile server opens it.
_PLAINTEXT_SOURCE_FIELDS = ("IMAGE_ID", "SOURCE_URL", "THUMBNAIL_URL")
# Ciphertext columns: random base64url can look like a date or labelled MRN, so
# they are shape-checked instead of text-scanned.
_CIPHERTEXT_FIELDS = {"sealed_source"}
# Canonical opaque keys from the v10 pipeline are built from slide_key hex,
# which can contain YYYYMMDD-looking runs.
_OPAQUE_KEY_PATTERNS = {
    "part_key": re.compile(r"^part:[0-9a-f]{32}$"),
    "block_key": re.compile(r"^block:[0-9a-f]{32}$"),
    "specimen_key": re.compile(r"^(?:block|part|unmatched)::part:[0-9a-f]{32}::block:[0-9a-f]{32}$"),
}


def _skips_date_scan(field: str, value: object) -> bool:
    pattern = _OPAQUE_KEY_PATTERNS.get(field)
    return bool(pattern and pattern.fullmatch(_text(value)))


class DeidViolation(ValueError):
    """Raised when a public WSI/timeline row would violate the contract."""


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


def _contains_absolute_date(value: str) -> bool:
    return any(
        pattern.search(value)
        for pattern in (
            _ABSOLUTE_DATE,
            _MONTH_FIRST_DATE,
            _DAY_FIRST_DATE,
            _NAMED_MONTH_DATE,
        )
    )


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
    if _contains_absolute_date(text) or _COMPACT_DATE.search(text):
        raise DeidViolation(f"absolute date in {field}")


def _assert_safe_metadata_text(value: object, field: str = "TILE_METADATA_JSON") -> None:
    """Scan only JSON string values; numeric geometry is not date text."""
    if isinstance(value, str):
        _assert_safe_text(field, value)
    elif isinstance(value, Mapping):
        for key, child in value.items():
            if key == "source_fingerprint":
                # A SHA-256 identity digest, not free text: its hex can
                # contain an eight-digit run that looks like YYYYMMDD.
                if not isinstance(child, str) or not _SHA256_HEX.fullmatch(child):
                    raise DeidViolation(f"invalid {field}.{key}")
                continue
            _assert_safe_metadata_text(child, f"{field}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_safe_metadata_text(child, f"{field}[{index}]")


def _is_servable(value: object) -> bool:
    if isinstance(value, bool):
        return value
    text = _text(value).lower()
    if text in {"", "false"}:
        return False
    if text == "true":
        return True
    raise DeidViolation("CAN_SERVE_TILES must be TRUE or FALSE")


def _validate_sealed_source(value: object, *, servable: bool) -> None:
    """Check SEALED_SOURCE shape without decrypting or echoing it."""
    text = _text(value)
    if not text:
        if servable:
            raise DeidViolation("SEALED_SOURCE is required when CAN_SERVE_TILES is TRUE")
        return
    if not servable:
        raise DeidViolation("SEALED_SOURCE must be empty when CAN_SERVE_TILES is not TRUE")
    if len(text) > SEALED_SOURCE_MAX_CHARS or not SEALED_SOURCE_PATTERN.fullmatch(text):
        raise DeidViolation("SEALED_SOURCE must be base64url without padding")
    try:
        decoded = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as error:
        raise DeidViolation("SEALED_SOURCE must be base64url without padding") from error
    if len(decoded) < SEALED_SOURCE_MIN_BYTES:
        raise DeidViolation("SEALED_SOURCE is too short to be a sealed source")


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
    prefix_match = _uri_is_under_prefix(value, prefixes) if prefixes else False
    if prefixes and not prefix_match:
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
    # Approved object-store roots are deployment-controlled release boundaries;
    # their folder names may contain pipeline dates. Identifiers remain
    # forbidden regardless of prefix.
    if (
        (not prefix_match and (
            _contains_absolute_date(value)
            or _contains_absolute_date(decoded_path)
            or _COMPACT_DATE.search(value)
            or _COMPACT_DATE.search(decoded_path)
        ))
        or _LABELLED_MRN.search(value)
        or _LABELLED_MRN.search(decoded_path)
    ):
        raise DeidViolation(f"identifier/date in {kind} URI")
    for identifier in related_identifiers:
        token = _text(identifier)
        if token and token.lower() in lowered:
            raise DeidViolation(f"related identifier in {kind} URI")


def validate_wsi_public_row(row: Mapping[str, object]) -> None:
    """Validate one public WSI study row (data_wsi.txt format v4).

    SLIDE_KEY is required. IMAGE_ID, SOURCE_URL and THUMBNAIL_URL must be
    absent or empty: sources are published only as SEALED_SOURCE, which is
    required when CAN_SERVE_TILES is TRUE and empty otherwise. Every other
    column is scanned for absolute dates and labelled MRNs. Violations name
    the field, never its value.
    """
    slide_key = _text(row.get("SLIDE_KEY"))
    if not SLIDE_KEY_PATTERN.fullmatch(slide_key):
        raise DeidViolation("SLIDE_KEY must be 32 lowercase hex characters")
    for field, value in row.items():
        if field.upper() in _PLAINTEXT_SOURCE_FIELDS and _text(value):
            raise DeidViolation(
                f"{field.upper()} is not allowed; sources are published only as SEALED_SOURCE"
            )
    _validate_sealed_source(
        row.get("SEALED_SOURCE"), servable=_is_servable(row.get("CAN_SERVE_TILES"))
    )
    content_type = _text(row.get("THUMBNAIL_CONTENT_TYPE")).lower()
    if content_type and content_type not in _THUMBNAIL_CONTENT_TYPES:
        raise DeidViolation("unsupported THUMBNAIL_CONTENT_TYPE")
    for field, value in row.items():
        normalized_field = field.lower()
        if normalized_field in _CIPHERTEXT_FIELDS:
            continue
        if (
            normalized_field not in _APPROVED_IDENTIFIER_FIELDS
            and normalized_field not in _WSI_NON_TEXT_FIELDS
            and not _skips_date_scan(normalized_field, value)
        ):
            _assert_safe_text(field, value)
        elif _LABELLED_MRN.search(_text(value)):
            raise DeidViolation(f"labelled MRN in {field}")
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


def validate_timeline_public_row(row: Mapping[str, object]) -> None:
    """Validate a timeline event, which may contain only relative timing."""
    forbidden = {"MRN", "DATE", "DIAGNOSIS_DATE", "PROCEDURE_DATE"}
    for field, value in row.items():
        if field.upper() in forbidden or field.lower() in _FORBIDDEN_FIELDS:
            raise DeidViolation(f"forbidden timeline field: {field}")
        if field.upper() in {"IMAGE_ID", "IMAGE_IDS"}:
            # Real slide identifiers are server-side only (wsi-serving-v6).
            raise DeidViolation(f"forbidden timeline field: {field}")
        if field.upper() not in {"PATIENT_ID", "SAMPLE_ID"}:
            _assert_safe_text(field, value)
    for field in ("START_DATE", "STOP_DATE"):
        value = _text(row.get(field))
        if value and not re.fullmatch(r"-?\d+", value):
            raise DeidViolation(f"timeline {field} must be a relative integer")
        if value and _COMPACT_DATE.fullmatch(value.lstrip("-")):
            raise DeidViolation(f"timeline {field} must be relative, not an absolute date")
