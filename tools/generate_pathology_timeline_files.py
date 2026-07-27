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

_TIMELINE_META_FILENAME = "meta_clinical_timeline_pathology_slides.txt"
_TIMELINE_DATA_FILENAME = "data_clinical_timeline_pathology_slides.txt"

_ASSOCIATION_QUERY = """
SELECT
    patient_id,
    sample_id,
    match_level,
    image_id,
    block_id,
    block_label,
    part_description,
    stain_name,
    stain_group,
    slide_path,
    slide_timepoint_days,
    slide_timepoint_source
FROM {canonical_table}
WHERE patient_id IN ({placeholders})
ORDER BY
    patient_id,
    slide_timepoint_days,
    sample_bucket,
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
    stain_group: str | None, stain_name: str | None
) -> str | None:
    group = (stain_group or "").lower()
    name = re.sub(r"\s+", " ", (stain_name or "").lower()).strip()
    if group == "ihc":
        return "IHC"
    if group in {"h&e", "h&e (initial)", "h&e (other)"} or name in {"h&e", "he"}:
        return "H&E"
    return None


def _clean_timeline_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "")).strip()


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
        "stainFilter": "hne" if subtype == "H&E" else "ihc",
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
    rows: list[dict] = []
    for batch in _chunk(patient_ids, 500):
        escaped = [patient_id.replace("'", "\\'") for patient_id in batch]
        placeholders = ", ".join(f"'{patient_id}'" for patient_id in escaped)
        rows.extend(
            _run_query(
                wc,
                warehouse_id,
                _ASSOCIATION_QUERY.format(
                    canonical_table=_CANONICAL_ASSOCIATION_TABLE,
                    placeholders=placeholders,
                ),
            )
        )
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

        slide_timepoint_days = row.get("slide_timepoint_days")
        if slide_timepoint_days is None:
            continue
        try:
            start_date = int(slide_timepoint_days)
        except (TypeError, ValueError):
            continue

        subtype = _infer_slide_type(row.get("stain_group"), row.get("stain_name"))
        if subtype is None:
            continue

        raw_match_level = str(row.get("match_level") or "UNMATCHED").upper()
        part_number, block_number, block_label = _derive_block_fields(
            row.get("block_id"),
            row.get("block_label"),
        )
        specimen_key = build_specimen_key(
            raw_match_level, part_number, block_number
        )
        specimen = _format_specimen_label(
            raw_match_level,
            part_number,
            row.get("part_description"),
            block_label,
            block_number,
        )
        can_serve_tiles = str(row.get("slide_path") or "").startswith("s3://")
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

        grouped.add_image(
            image_id=image_id,
            can_serve_tiles=can_serve_tiles,
            timepoint_source=row.get("slide_timepoint_source"),
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
    timeline_rows = build_pathology_timeline_rows(association_rows, study_id)

    _write_timeline_meta(study_dir, study_id)
    _write_timeline_data(study_dir, timeline_rows)

    print(f"Study dir: {study_dir}")
    print(f"Study id: {study_id}")
    print(f"Pathology timeline rows: {len(timeline_rows)}")
    print(f"Written: {_TIMELINE_META_FILENAME}")
    print(f"Written: {_TIMELINE_DATA_FILENAME}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
