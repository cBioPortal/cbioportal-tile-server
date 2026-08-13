"""Canonical cBioPortal study-file format for pathology slide data."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit


FORMAT_VERSION = 1
META_FILENAME = "meta_wsi.txt"
DATA_FILENAME = "data_wsi.txt"
GENETIC_ALTERATION_TYPE = "PATHOLOGY_SLIDES"
DATATYPE = "WSI"


@dataclass(frozen=True)
class Column:
    name: str
    display_name: str
    description: str
    datatype: str
    field: str


COLUMNS = (
    Column("PATIENT_ID", "Patient Identifier", "cBioPortal patient stable identifier.", "STRING", "patient_id"),
    Column("REFERENCE_SAMPLE_ID", "Reference Sample Identifier", "Optional patient reference sample stable identifier.", "STRING", "reference_sample_id"),
    Column("SAMPLE_ID", "Sample Identifier", "Matched cBioPortal sample stable identifier; empty for unmatched slides.", "STRING", "sample_id"),
    Column("IMAGE_ID", "Image Identifier", "Stable whole-slide image identifier, unique within the study.", "STRING", "image_id"),
    Column("PART_KEY", "Part Key", "Stable pathology part key within the patient.", "STRING", "part_key"),
    Column("PART_NUMBER", "Part Number", "Source pathology part number.", "STRING", "part_number"),
    Column("PART_DESIGNATOR", "Part Designator", "Source pathology part designator.", "STRING", "part_designator"),
    Column("PART_TYPE", "Part Type", "Pathology part type.", "STRING", "part_type"),
    Column("PART_DESCRIPTION", "Part Description", "Pathology part description.", "STRING", "part_description"),
    Column("SUBSPECIALTY", "Subspecialty", "Pathology subspecialty.", "STRING", "subspecialty"),
    Column("PATH_DX_TITLE", "Pathology Diagnosis", "Pathology diagnosis title.", "STRING", "path_dx_title"),
    Column("BLOCK_KEY", "Block Key", "Stable pathology block key within the part.", "STRING", "block_key"),
    Column("BLOCK_NUMBER", "Block Number", "Source pathology block number.", "STRING", "block_number"),
    Column("BLOCK_LABEL", "Block Label", "Human-readable pathology block label.", "STRING", "block_label"),
    Column("MATCH_LEVEL", "Match Level", "Slide association level: BLOCK, PART, or UNMATCHED.", "STRING", "match_level"),
    Column("SPECIMEN_KEY", "Specimen Key", "Stable key used to select this pathology specimen.", "STRING", "specimen_key"),
    Column("STAIN_NAME", "Stain Name", "Source stain name.", "STRING", "stain_name"),
    Column("STAIN_GROUP", "Stain Group", "Normalized stain group.", "STRING", "stain_group"),
    Column("IS_HNE", "Is H&E", "Whether the slide is an H&E slide.", "BOOLEAN", "is_hne"),
    Column("IS_IHC", "Is IHC", "Whether the slide is an immunohistochemistry slide.", "BOOLEAN", "is_ihc"),
    Column("MAGNIFICATION", "Magnification", "Scanner or source magnification.", "STRING", "magnification"),
    Column("FILE_SIZE_BYTES", "File Size", "Source slide size in bytes.", "NUMBER", "file_size_bytes"),
    Column("BARCODE", "Barcode", "Slide barcode, when available.", "STRING", "barcode"),
    Column("SLIDE_TYPE", "Slide Type", "Normalized slide type, such as H&E or IHC.", "STRING", "slide_type"),
    Column("PROCEDURE_DATE_DAYS", "Procedure Date (Days)", "Procedure date in days relative to the study reference event.", "NUMBER", "procedure_date_days"),
    Column("TIMEPOINT_SOURCE", "Timepoint Source", "Provenance of PROCEDURE_DATE_DAYS.", "STRING", "timepoint_source"),
    Column("CAN_SERVE_TILES", "Can Serve Tiles", "Whether all source-bound pixel artifacts are complete.", "BOOLEAN", "can_serve_tiles"),
    Column("SOURCE_URL", "Slide Source URL", "Exact source URL supplied to the tile server.", "STRING", "source_url"),
    Column("TILE_METADATA_JSON", "Tile Metadata", "Compact JSON tile-pyramid metadata consumed by the browser.", "STRING", "tile_metadata_json"),
    Column("THUMBNAIL_URL", "Thumbnail Source URL", "Exact pre-rendered thumbnail source URL.", "STRING", "thumbnail_url"),
    Column("THUMBNAIL_WIDTH", "Thumbnail Width", "Intrinsic thumbnail width in pixels.", "NUMBER", "thumbnail_width"),
    Column("THUMBNAIL_HEIGHT", "Thumbnail Height", "Intrinsic thumbnail height in pixels.", "NUMBER", "thumbnail_height"),
    Column("THUMBNAIL_CONTENT_TYPE", "Thumbnail Content Type", "Thumbnail media type, normally image/jpeg.", "STRING", "thumbnail_content_type"),
)

COLUMN_NAMES = tuple(column.name for column in COLUMNS)
REQUIRED_VALUES = {
    "PATIENT_ID",
    "IMAGE_ID",
    "PART_KEY",
    "BLOCK_KEY",
    "MATCH_LEVEL",
    "SPECIMEN_KEY",
    "IS_HNE",
    "IS_IHC",
    "CAN_SERVE_TILES",
}
BOOLEAN_COLUMNS = {"IS_HNE", "IS_IHC", "CAN_SERVE_TILES"}
INTEGER_COLUMNS = {
    "FILE_SIZE_BYTES",
    "PROCEDURE_DATE_DAYS",
    "THUMBNAIL_WIDTH",
    "THUMBNAIL_HEIGHT",
}


def _safe_text(value: object, *, field: str) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    text = str(value)
    if "\t" in text or "\r" in text or "\n" in text:
        raise ValueError(f"{field} cannot contain tabs or newlines")
    return text


def _row_value(row: dict, column: Column) -> object:
    if column.name == "SOURCE_URL":
        return row.get("source_url", row.get("slide_path"))
    if column.name == "TILE_METADATA_JSON":
        value = row.get("tile_metadata_json")
        if value in (None, ""):
            value = row.get("tile_metadata")
        return value
    return row.get(column.field)


def write_wsi_study_files(
    output_dir: Path,
    study_id: str,
    rows: Iterable[dict],
    *,
    meta_filename: str = META_FILENAME,
    data_filename: str = DATA_FILENAME,
) -> tuple[Path, Path]:
    """Write one versioned meta file and one canonical WSI TSV data file."""
    study_id = _safe_text(study_id, field="cancer_study_identifier").strip()
    if not study_id:
        raise ValueError("cancer_study_identifier is required")
    if Path(meta_filename).name != meta_filename or Path(data_filename).name != data_filename:
        raise ValueError("WSI study filenames must not contain a directory")

    output_dir.mkdir(parents=True, exist_ok=True)
    meta_path = output_dir / meta_filename
    data_path = output_dir / data_filename
    meta_path.write_text(
        "\n".join(
            (
                f"cancer_study_identifier: {study_id}",
                f"genetic_alteration_type: {GENETIC_ALTERATION_TYPE}",
                f"datatype: {DATATYPE}",
                f"data_filename: {data_filename}",
                f"format_version: {FORMAT_VERSION}",
                "",
            )
        ),
        encoding="utf-8",
    )

    serialized_rows: list[list[str]] = []
    for row_number, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ValueError(f"WSI row {row_number} must be an object")
        serialized_rows.append(
            [
                _safe_text(_row_value(row, column), field=column.name)
                for column in COLUMNS
            ]
        )
    serialized_rows.sort(
        key=lambda row: (
            row[COLUMN_NAMES.index("PATIENT_ID")],
            row[COLUMN_NAMES.index("IMAGE_ID")],
        )
    )

    with data_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(
            handle,
            delimiter="\t",
            lineterminator="\n",
            quoting=csv.QUOTE_NONE,
            quotechar=None,
        )
        writer.writerow([f"#{column.display_name}" for column in COLUMNS])
        writer.writerow([f"#{column.description}" for column in COLUMNS])
        writer.writerow([f"#{column.datatype}" for column in COLUMNS])
        writer.writerow(["#0" for _ in COLUMNS])
        writer.writerow(COLUMN_NAMES)
        writer.writerows(serialized_rows)
    return meta_path, data_path


def _read_meta(meta_path: Path) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for line_number, raw_line in enumerate(meta_path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition(":")
        if not separator or not key.strip() or not value.strip():
            raise ValueError(f"Invalid WSI metadata line {line_number}")
        metadata[key.strip()] = value.strip()
    required = {
        "cancer_study_identifier",
        "genetic_alteration_type",
        "datatype",
        "data_filename",
        "format_version",
    }
    missing = sorted(required - metadata.keys())
    if missing:
        raise ValueError(f"WSI metadata is missing: {', '.join(missing)}")
    if metadata["genetic_alteration_type"] != GENETIC_ALTERATION_TYPE:
        raise ValueError(f"genetic_alteration_type must be {GENETIC_ALTERATION_TYPE}")
    if metadata["datatype"] != DATATYPE:
        raise ValueError(f"datatype must be {DATATYPE}")
    if metadata["format_version"] != str(FORMAT_VERSION):
        raise ValueError(f"unsupported WSI format_version: {metadata['format_version']}")
    if Path(metadata["data_filename"]).name != metadata["data_filename"]:
        raise ValueError("data_filename must not contain a directory")
    return metadata


def _parse_boolean(value: str, column: str, line_number: int) -> bool:
    normalized = value.strip().upper()
    if normalized == "TRUE":
        return True
    if normalized == "FALSE":
        return False
    raise ValueError(f"{column} must be TRUE or FALSE on data line {line_number}")


def _parse_integer(value: str, column: str, line_number: int) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except ValueError as error:
        raise ValueError(f"{column} must be an integer on data line {line_number}") from error


def _validate_url(value: object, column: str, line_number: int) -> None:
    parsed = urlsplit(str(value))
    if not parsed.scheme or not (parsed.netloc or parsed.path):
        raise ValueError(f"{column} must be an absolute URL on data line {line_number}")


def read_wsi_study(meta_path: Path) -> tuple[str, list[dict]]:
    """Read and validate a canonical WSI study-file pair."""
    metadata = _read_meta(meta_path)
    data_path = meta_path.parent / metadata["data_filename"]
    if not data_path.is_file():
        raise ValueError(f"WSI data file not found: {data_path.name}")

    lines = data_path.read_text(encoding="utf-8").splitlines()
    if len(lines) < 5:
        raise ValueError("WSI data file needs four metadata rows and one column row")
    parsed_header_rows = [
        next(csv.reader([line], delimiter="\t", quoting=csv.QUOTE_NONE, quotechar=None))
        for line in lines[:5]
    ]
    for index, row in enumerate(parsed_header_rows[:4], 1):
        if len(row) != len(COLUMNS) or any(not value.startswith("#") for value in row):
            raise ValueError(f"Invalid WSI attribute metadata row {index}")
    if tuple(parsed_header_rows[4]) != COLUMN_NAMES:
        raise ValueError("WSI data columns do not match format_version 1")

    rows: list[dict] = []
    seen_images: set[str] = set()
    patient_reference_samples: dict[str, str] = {}
    for line_number, raw_line in enumerate(lines[5:], 6):
        if not raw_line.strip():
            continue
        values = next(
            csv.reader([raw_line], delimiter="\t", quoting=csv.QUOTE_NONE, quotechar=None)
        )
        if len(values) != len(COLUMNS):
            raise ValueError(f"WSI data line {line_number} has {len(values)} columns; expected {len(COLUMNS)}")
        raw = dict(zip(COLUMN_NAMES, values))
        missing = sorted(column for column in REQUIRED_VALUES if not raw[column].strip())
        if missing:
            raise ValueError(f"WSI data line {line_number} is missing: {', '.join(missing)}")

        image_id = raw["IMAGE_ID"].strip()
        if image_id in seen_images:
            raise ValueError(f"duplicate IMAGE_ID in study: {image_id}")
        seen_images.add(image_id)
        patient_id = raw["PATIENT_ID"].strip()
        reference_sample = raw["REFERENCE_SAMPLE_ID"].strip()
        previous_reference = patient_reference_samples.setdefault(patient_id, reference_sample)
        if previous_reference != reference_sample:
            raise ValueError(f"conflicting REFERENCE_SAMPLE_ID for patient {patient_id}")

        match_level = raw["MATCH_LEVEL"].strip().upper()
        sample_id = raw["SAMPLE_ID"].strip()
        if match_level not in {"BLOCK", "PART", "UNMATCHED"}:
            raise ValueError(f"invalid MATCH_LEVEL on data line {line_number}")
        if match_level == "UNMATCHED" and sample_id:
            raise ValueError(f"UNMATCHED row must have an empty SAMPLE_ID on data line {line_number}")
        if match_level != "UNMATCHED" and not sample_id:
            raise ValueError(f"matched row needs SAMPLE_ID on data line {line_number}")

        row: dict[str, object] = {}
        for column in COLUMNS:
            value = raw[column.name].strip()
            if column.name in BOOLEAN_COLUMNS:
                row[column.field] = _parse_boolean(value, column.name, line_number)
            elif column.name in INTEGER_COLUMNS:
                row[column.field] = _parse_integer(value, column.name, line_number)
            else:
                row[column.field] = value or None
        row["match_level"] = match_level
        row["slide_path"] = row.pop("source_url")

        tile_metadata_json = row.get("tile_metadata_json")
        if tile_metadata_json:
            try:
                tile_metadata = json.loads(str(tile_metadata_json))
            except json.JSONDecodeError as error:
                raise ValueError(f"invalid TILE_METADATA_JSON on data line {line_number}") from error
            if not isinstance(tile_metadata, dict):
                raise ValueError(f"TILE_METADATA_JSON must contain an object on data line {line_number}")

        if row.get("slide_path"):
            _validate_url(row["slide_path"], "SOURCE_URL", line_number)
        if row.get("thumbnail_url"):
            _validate_url(row["thumbnail_url"], "THUMBNAIL_URL", line_number)

        if row["can_serve_tiles"]:
            required_artifacts = (
                "slide_path",
                "tile_metadata_json",
                "thumbnail_url",
                "thumbnail_width",
                "thumbnail_height",
                "thumbnail_content_type",
            )
            missing_artifacts = [name for name in required_artifacts if row.get(name) in (None, "")]
            if missing_artifacts:
                raise ValueError(
                    f"servable WSI data line {line_number} is missing: {', '.join(missing_artifacts)}"
                )
            if row["thumbnail_width"] <= 0 or row["thumbnail_height"] <= 0:  # type: ignore[operator]
                raise ValueError(f"thumbnail dimensions must be positive on data line {line_number}")
        rows.append(row)
    if not rows:
        raise ValueError("WSI data file contains no rows")
    return metadata["cancer_study_identifier"], rows
