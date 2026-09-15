#!/usr/bin/env python3
"""
Generate canonical pathology timeline study files from the shared pathology ETL.

This writes a standard cBioPortal TIMELINE file so pathology slide events load
through the existing clinical_event import path and become available from the
ClickHouse-backed clinical events API after study reload.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.constants import (  # noqa: E402
    CANONICAL_ASSOCIATION_TABLE as _CANONICAL_ASSOCIATION_TABLE,
    DEFAULT_WAREHOUSE_ID as _DEFAULT_WAREHOUSE,
)
from app.associations import (  # noqa: E402
    build_specimen_key,
    canonicalize_association_rows,
    derive_block_fields as _derive_block_fields,
)
from app.deid import DeidViolation, validate_timeline_public_row  # noqa: E402

_TIMELINE_META_FILENAME = "meta_clinical_timeline_pathology_slides.txt"
_TIMELINE_DATA_FILENAME = "data_clinical_timeline_pathology_slides.txt"

_TIMELINE_DATE_PATTERNS = (
    re.compile(r"(?<!\d)(?:19|20)\d{2}[-_/](?:0?[1-9]|1[0-2])[-_/](?:0?[1-9]|[12]\d|3[01])(?!\d)"),
    re.compile(r"(?<!\d)(?:0?[1-9]|1[0-2])[-_/](?:0?[1-9]|[12]\d|3[01])[-_/](?:19|20)\d{2}(?!\d)"),
    re.compile(r"(?<!\d)(?:0?[1-9]|[12]\d|3[01])[-_/](?:0?[1-9]|1[0-2])[-_/](?:19|20)\d{2}(?!\d)"),
    re.compile(
        r"(?i)(?<![a-z0-9])(?:(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|"
        r"may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
        r"nov(?:ember)?|dec(?:ember)?)\s+(?:0?[1-9]|[12]\d|3[01])(?:st|nd|rd|th)?"
        r"(?:,)?\s+(?:19|20)\d{2})(?![a-z0-9])"
    ),
    re.compile(r"(?<!\d)(?:19|20)\d{6}(?!\d)"),
)
_TIMELINE_MRN_PATTERN = re.compile(
    r"(?i)\b(?:mrn|medical[ _-]?record(?:[ _-]?number)?)\b\s*[:=#-]?\s*\d{4,}"
)

_ASSOCIATION_QUERY = """
SELECT
    patient_id,
    sample_id,
    match_level,
    image_id,
    part_key,
    part_number,
    block_key,
    block_number,
    block_label,
    part_description,
    stain_name,
    stain_group,
    is_hne,
    is_ihc,
    slide_path,
    can_serve_tiles,
    specimen_key,
    timeline_start_days,
    timeline_date_status
FROM {canonical_table}
WHERE patient_id IN ({placeholders})
ORDER BY
    patient_id,
    timeline_start_days,
    sample_id,
    match_level,
    image_id
"""

# Older production refreshes exposed the relative date under the retired
# ``procedure_date_days``/``timepoint_source`` names.  Keep the public query
# above on the versioned contract, but make the exporter able to read one of
# those tables while the migration is rolling through the warehouse.  The
# result aliases are deliberately the current names before they reach the
# timeline formatter.
_LEGACY_ASSOCIATION_QUERY = """
SELECT
    patient_id,
    sample_id,
    match_level,
    image_id,
    part_key,
    part_number,
    block_key,
    block_number,
    block_label,
    part_description,
    stain_name,
    stain_group,
    is_hne,
    is_ihc,
    slide_path,
    can_serve_tiles,
    specimen_key,
    procedure_date_days AS timeline_start_days,
    CASE
        WHEN procedure_date_days IS NOT NULL THEN 'AVAILABLE'
        ELSE 'MISSING_PROCEDURE_DATE'
    END AS timeline_date_status,
    timepoint_source AS slide_timepoint_source
FROM {canonical_table}
WHERE patient_id IN ({placeholders})
ORDER BY
    patient_id,
    procedure_date_days,
    sample_id,
    match_level,
    image_id
"""


@dataclass
class _GroupedTimelineRow:
    patient_id: str
    start_date: int
    sample_id: str
    match_level: str
    specimen: str
    specimen_key: str
    subtype: str
    timepoint_sources: set[str] = field(default_factory=set)
    servable_image_ids: set[str] = field(default_factory=set)
    non_servable_image_ids: set[str] = field(default_factory=set)

    def add_image(
        self,
        image_id: str,
        can_serve_tiles: bool,
        timepoint_source: str | None,
    ) -> None:
        if can_serve_tiles:
            self.servable_image_ids.add(image_id)
        else:
            self.non_servable_image_ids.add(image_id)
        if timepoint_source:
            self.timepoint_sources.add(timepoint_source)

    @property
    def image_count(self) -> int:
        return len(self.servable_image_ids)

    @property
    def non_servable_image_count(self) -> int:
        return len(self.non_servable_image_ids)

    @property
    def total_image_count(self) -> int:
        return self.image_count + self.non_servable_image_count

    @property
    def timepoint_source(self) -> str:
        return _clean_timeline_text(", ".join(sorted(self.timepoint_sources)))


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--study-dir",
        required=True,
        type=Path,
        help="Path to the cBioPortal study directory.",
    )
    parser.add_argument(
        "--warehouse-id",
        default=os.environ.get("DATABRICKS_WAREHOUSE_ID", _DEFAULT_WAREHOUSE),
        help="Databricks SQL warehouse ID.",
    )
    return parser.parse_args(argv)


def _run_query(wc, warehouse_id: str, sql: str) -> list[dict]:
    import time
    from databricks.sdk.service.sql import StatementState

    stmt = wc.statement_execution.execute_statement(
        warehouse_id=warehouse_id,
        statement=sql,
        wait_timeout="50s",
    )
    while stmt.status.state in (StatementState.PENDING, StatementState.RUNNING):
        time.sleep(3)
        stmt = wc.statement_execution.get_statement(stmt.statement_id)

    if stmt.status.state != StatementState.SUCCEEDED:
        err = getattr(stmt.status, "error", None)
        raise RuntimeError(f"Databricks query failed: {err}")

    columns = [column.name for column in stmt.manifest.schema.columns]
    return [dict(zip(columns, row)) for row in (stmt.result.data_array or [])]


def _table_columns(wc, warehouse_id: str, table_name: str) -> set[str]:
    """Return the warehouse columns used to select the migration-safe query."""
    rows = _run_query(wc, warehouse_id, f"DESCRIBE TABLE {table_name}")
    return {
        str(row.get("col_name") or row.get("column_name") or "").strip().lower()
        for row in rows
        if str(row.get("col_name") or row.get("column_name") or "").strip()
    }


def _chunk(items: list[str], size: int):
    for index in range(0, len(items), size):
        yield items[index : index + size]


def _read_patient_ids(study_dir: Path) -> list[str]:
    clinical_path = study_dir / "data_clinical_sample.txt"
    if not clinical_path.exists():
        raise FileNotFoundError(
            f"data_clinical_sample.txt not found in {study_dir}"
        )

    patient_ids: list[str] = []
    seen: set[str] = set()
    with clinical_path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(
            (line for line in handle if not line.startswith("#")),
            delimiter="\t",
        )
        for row in reader:
            patient_id = (row.get("PATIENT_ID") or "").strip()
            if patient_id and patient_id not in seen:
                seen.add(patient_id)
                patient_ids.append(patient_id)

    if not patient_ids:
        raise ValueError("No PATIENT_ID values found in data_clinical_sample.txt")
    return patient_ids


def _read_study_identifier(study_dir: Path) -> str:
    meta_path = study_dir / "meta_study.txt"
    if not meta_path.exists():
        return study_dir.name
    for line in meta_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("cancer_study_identifier:"):
            return line.split(":", 1)[1].strip()
    return study_dir.name


def _infer_slide_type(
    stain_group: str | None,
    stain_name: str | None,
    is_hne: bool | None = None,
    is_ihc: bool | None = None,
) -> str | None:
    # The canonical association pipeline resolves these flags for every real
    # slide.  Preserve a third category when both are explicitly false so a
    # valid slide (for example an unstained recut) is not silently dropped
    # from the clinical timeline.  Rows without resolved flags retain the
    # legacy text-based behavior, which keeps synthetic/legacy records such as
    # "SLIDES SUBMITTED" out of the pathology-slide timeline.
    def as_bool(value: bool | str | None) -> bool | None:
        if isinstance(value, bool):
            return value
        if value is None:
            return None
        normalized = str(value).strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
        return None

    is_hne = as_bool(is_hne)
    is_ihc = as_bool(is_ihc)
    if is_ihc is True:
        return "IHC"
    if is_hne is True:
        return "H&E"
    if is_hne is False and is_ihc is False:
        return "Other"
    group = (stain_group or "").lower()
    name = re.sub(r"\s+", " ", (stain_name or "").lower()).strip()
    if group == "ihc":
        return "IHC"
    if group in {"h&e", "h&e (initial)", "h&e (other)"} or name in {"h&e", "he"}:
        return "H&E"
    return None


def _clean_timeline_text(value: str | None) -> str:
    text = re.sub(r"\s+", " ", (value or "")).strip()
    text = _TIMELINE_MRN_PATTERN.sub("[REDACTED_MRN]", text)
    for pattern in _TIMELINE_DATE_PATTERNS:
        text = pattern.sub("[REDACTED_DATE]", text)
    return text


def _as_bool(value: object) -> bool | None:
    """Parse warehouse boolean values without treating ``"false"`` as true.

    Databricks SQL exports booleans as strings when the result is serialized
    through JSON/CSV.  Calling ``bool(value)`` on those strings is unsafe:
    both ``"true"`` and ``"false"`` are non-empty and therefore truthy.  A
    missing/unknown value is kept as ``None`` so the legacy slide-path
    fallback remains explicit at the call site.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    return None


def _safe_specimen_key(value: str | None, fallback: str) -> str:
    """Normalize a source specimen key to the same public key shape as WSI."""
    text = _clean_timeline_text(value)
    text = re.sub(r"[^A-Za-z0-9_.:/-]+", "_", text).strip("_")
    return text or fallback


def _format_specimen_label(
    match_level: str,
    part_number: int | None,
    part_description: str | None,
    block_label: str | None,
    block_number: str,
) -> str:
    part_label = (
        f"Part {part_number}"
        if part_number is not None
        else (_clean_timeline_text(part_description) or "Specimen")
    )
    block_token = _clean_timeline_text(block_label) or block_number or None
    if match_level == "BLOCK" and block_token:
        return f"{part_label} / Block {block_token}"
    return part_label


def _sample_display_value(sample_id: str | None, match_level: str) -> str:
    if sample_id:
        return sample_id
    return "Unmatched" if match_level == "UNMATCHED" else ""


def _match_level_display_value(match_level: str) -> str:
    return "Unmatched" if match_level == "UNMATCHED" else match_level


def _build_linkout(
    study_id: str,
    patient_id: str,
    sample_id: str,
    subtype: str,
    match_level: str,
    specimen_key: str,
    image_count: int,
) -> str:
    if image_count < 1:
        return ""

    params = {
        "studyId": study_id,
        "caseId": patient_id,
        "stainFilter": (
            "hne" if subtype == "H&E" else "ihc" if subtype == "IHC" else "all"
        ),
        "matchLevel": match_level,
        "specimenKey": specimen_key,
    }
    if sample_id and sample_id != "Unmatched":
        params["sampleId"] = sample_id
    return f"/patient/wsiHESlides?{urlencode(params)}"


def _canonicalize_association_rows(rows: list[dict]) -> list[dict]:
    return canonicalize_association_rows(rows, ("patient_id", "image_id"))


def _fetch_canonical_associations(
    patient_ids: list[str], warehouse_id: str
) -> list[dict]:
    from databricks.sdk import WorkspaceClient

    wc = WorkspaceClient()
    columns = _table_columns(wc, warehouse_id, _CANONICAL_ASSOCIATION_TABLE)
    current_timing = {"timeline_start_days", "timeline_date_status"}.issubset(columns)
    legacy_timing = {"procedure_date_days", "timepoint_source"}.issubset(columns)
    if not current_timing and not legacy_timing:
        raise RuntimeError(
            f"{_CANONICAL_ASSOCIATION_TABLE} has neither the current timeline "
            "columns nor the legacy migration columns"
        )
    query_template = _ASSOCIATION_QUERY if current_timing else _LEGACY_ASSOCIATION_QUERY

    rows: list[dict] = []
    # Keep the IN-list below Databricks' 25 MB inline-result limit. Parallel
    # batches make a full-study export practical without changing the result
    # contract or retaining any PHI in the query itself.
    batches = list(_chunk(patient_ids, 500))

    def fetch(batch: list[str]) -> list[dict]:
        escaped = [patient_id.replace("'", "\\'") for patient_id in batch]
        placeholders = ", ".join(f"'{patient_id}'" for patient_id in escaped)
        from databricks.sdk import WorkspaceClient

        return _run_query(
            WorkspaceClient(),
            warehouse_id,
            query_template.format(
                canonical_table=_CANONICAL_ASSOCIATION_TABLE,
                placeholders=placeholders,
            ),
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        for batch_rows in executor.map(fetch, batches):
            rows.extend(batch_rows)
    return rows


def build_pathology_timeline_rows(
    association_rows: list[dict], study_id: str
) -> list[list[str]]:
    grouped_rows: dict[
        tuple[str, int, str, str, str, str, str], _GroupedTimelineRow
    ] = {}

    for row in _canonicalize_association_rows(association_rows):
        patient_id = str(row.get("patient_id") or "").strip()
        image_id = str(row.get("image_id") or "").strip()
        if not patient_id or not image_id:
            continue

        slide_timepoint_days = row.get("timeline_start_days")
        if slide_timepoint_days is None:
            # Keep local legacy fixtures readable; production SQL never
            # selects the retired procedure-relative column.
            slide_timepoint_days = row.get("slide_timepoint_days")
        if slide_timepoint_days is None:
            continue
        timeline_status = str(row.get("timeline_date_status") or "").strip().upper()
        if timeline_status and timeline_status != "AVAILABLE":
            continue
        try:
            start_date = int(slide_timepoint_days)
        except (TypeError, ValueError):
            continue

        subtype = _infer_slide_type(
            row.get("stain_group"),
            row.get("stain_name"),
            row.get("is_hne"),
            row.get("is_ihc"),
        )
        if subtype is None:
            continue

        raw_match_level = str(row.get("match_level") or "UNMATCHED").upper()
        part_number_value = row.get("part_number")
        part_number = (
            int(part_number_value)
            if isinstance(part_number_value, (int, str)) and str(part_number_value).isdigit()
            else None
        )
        block_number = str(row.get("block_number") or "").strip()
        block_label = row.get("block_label")
        if not block_number:
            part_number, block_number, block_label = _derive_block_fields(
                row.get("block_id"), row.get("block_label")
            )
        specimen_key = _safe_specimen_key(
            row.get("specimen_key"),
            build_specimen_key(raw_match_level, part_number, block_number),
        )
        specimen = _format_specimen_label(
            raw_match_level,
            part_number,
            row.get("part_description"),
            block_label,
            block_number,
        )
        parsed_can_serve_tiles = _as_bool(row.get("can_serve_tiles"))
        can_serve_tiles = (
            parsed_can_serve_tiles
            if parsed_can_serve_tiles is not None
            else str(row.get("slide_path") or "").startswith("s3://")
        )
        sample_display = _sample_display_value(row.get("sample_id"), raw_match_level)
        match_level = _match_level_display_value(raw_match_level)
        grouping_specimen_token = specimen_key if can_serve_tiles else specimen
        group_key = (
            patient_id,
            start_date,
            sample_display,
            match_level,
            specimen,
            grouping_specimen_token,
            subtype,
        )
        grouped = grouped_rows.get(group_key)
        if grouped is None:
            grouped = _GroupedTimelineRow(
                patient_id=patient_id,
                start_date=start_date,
                sample_id=sample_display,
                match_level=match_level,
                specimen=specimen,
                specimen_key=specimen_key,
                subtype=subtype,
            )
            grouped_rows[group_key] = grouped

        timepoint_source = (
            "Procedure date relative to first ICD-O diagnosis"
            if timeline_status == "AVAILABLE"
            else row.get("timeline_date_status") or row.get("slide_timepoint_source")
        )
        grouped.add_image(
            image_id=image_id,
            can_serve_tiles=can_serve_tiles,
            timepoint_source=timepoint_source,
        )

    ordered_groups = sorted(
        grouped_rows.values(),
        key=lambda group: (
            group.patient_id,
            group.start_date,
            group.sample_id,
            group.match_level,
            group.specimen,
            group.subtype,
        ),
    )

    rows: list[list[str]] = []
    for group in ordered_groups:
        rows.append(
            [
                group.patient_id,
                str(group.start_date),
                "",
                "PATHOLOGY SLIDES",
                group.sample_id,
                group.subtype,
                group.match_level,
                group.specimen,
                str(group.image_count),
                str(group.non_servable_image_count),
                str(group.total_image_count),
                group.timepoint_source,
                _build_linkout(
                    study_id=study_id,
                    patient_id=group.patient_id,
                    sample_id=group.sample_id,
                    subtype=group.subtype,
                    match_level=group.match_level,
                    specimen_key=group.specimen_key,
                    image_count=group.image_count,
                ),
            ]
        )

    return rows


def _write_timeline_meta(study_dir: Path, study_id: str) -> None:
    (study_dir / _TIMELINE_META_FILENAME).write_text(
        "\n".join(
            [
                f"cancer_study_identifier: {study_id}",
                "genetic_alteration_type: CLINICAL",
                "datatype: TIMELINE",
                f"data_filename: {_TIMELINE_DATA_FILENAME}",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _write_timeline_data(study_dir: Path, rows: list[list[str]]) -> None:
    columns = [
        "PATIENT_ID", "START_DATE", "STOP_DATE", "EVENT_TYPE", "SAMPLE_ID",
        "SUBTYPE", "MATCH_LEVEL", "SPECIMEN", "IMAGE_COUNT",
        "NON_SERVABLE_IMAGE_COUNT", "TOTAL_IMAGE_COUNT", "TIMEPOINT_SOURCE", "LINKOUT",
    ]
    for row_number, row in enumerate(rows, start=1):
        try:
            validate_timeline_public_row(dict(zip(columns, row)))
        except DeidViolation as error:
            raise ValueError(
                f"timeline row {row_number} violates the de-identification contract: {error}"
            ) from error
    with (study_dir / _TIMELINE_DATA_FILENAME).open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(
            [
                "PATIENT_ID",
                "START_DATE",
                "STOP_DATE",
                "EVENT_TYPE",
                "SAMPLE_ID",
                "SUBTYPE",
                "MATCH_LEVEL",
                "SPECIMEN",
                "IMAGE_COUNT",
                "NON_SERVABLE_IMAGE_COUNT",
                "TOTAL_IMAGE_COUNT",
                "TIMEPOINT_SOURCE",
                "LINKOUT",
            ]
        )
        writer.writerows(rows)


def write_pathology_timeline_files(
    study_dir: Path, study_id: str, association_rows: list[dict]
) -> tuple[Path, Path, int]:
    """Write the canonical pathology timeline pair from association rows."""
    timeline_rows = build_pathology_timeline_rows(association_rows, study_id)
    _write_timeline_meta(study_dir, study_id)
    _write_timeline_data(study_dir, timeline_rows)
    return (
        study_dir / _TIMELINE_META_FILENAME,
        study_dir / _TIMELINE_DATA_FILENAME,
        len(timeline_rows),
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    study_dir = args.study_dir.expanduser().resolve()
    if not study_dir.is_dir():
        print(f"ERROR: study directory not found: {study_dir}", file=sys.stderr)
        return 1

    patient_ids = _read_patient_ids(study_dir)
    study_id = _read_study_identifier(study_dir)
    association_rows = _fetch_canonical_associations(
        patient_ids, args.warehouse_id
    )
    meta_path, data_path, row_count = write_pathology_timeline_files(
        study_dir, study_id, association_rows
    )

    print(f"Study dir: {study_dir}")
    print(f"Study id: {study_id}")
    print(f"Pathology timeline rows: {row_count}")
    print(f"Written: {meta_path.name}")
    print(f"Written: {data_path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
