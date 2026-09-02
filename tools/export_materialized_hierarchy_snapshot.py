#!/usr/bin/env python3
"""Export canonical pathology rows as cBioPortal WSI study files."""

from __future__ import annotations

import argparse
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from app import meta_store
from app.config import settings

try:
    from tools.wsi_study_format import write_wsi_study_files
    from tools.generate_pathology_timeline_files import write_pathology_timeline_files
except ModuleNotFoundError:  # Direct execution from the tools directory.
    from wsi_study_format import write_wsi_study_files
    from generate_pathology_timeline_files import write_pathology_timeline_files


def _read_patient_ids(study_dir: Path) -> list[str]:
    data_file = study_dir / "data_clinical_patient.txt"
    with data_file.open(encoding="utf-8", newline="") as handle:
        rows = [
            row
            for row in csv.reader(handle, delimiter="\t")
            if row and not row[0].startswith("#")
        ]
    if not rows:
        raise ValueError(f"No column header found in {data_file}")
    header = rows[0]
    try:
        patient_index = header.index("PATIENT_ID")
    except ValueError as error:
        raise ValueError(f"PATIENT_ID column not found in {data_file}") from error
    return sorted(
        {
            row[patient_index].strip()
            for row in rows[1:]
            if len(row) > patient_index and row[patient_index].strip()
        }
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study-dir", required=True, help="Path to the cBioPortal study directory.")
    parser.add_argument("--study-id", required=True, help="cBioPortal study identifier.")
    parser.add_argument(
        "--output-dir",
        help="Directory for meta_wsi.txt and data_wsi.txt; defaults to --study-dir.",
    )
    parser.add_argument(
        "--warehouse-id",
        default=settings.databricks_warehouse_id,
        help="Databricks SQL warehouse id.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Concurrent Databricks fetch workers.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if not settings.wsi_allowed_source_prefixes or not settings.wsi_allowed_thumbnail_prefixes:
        raise ValueError(
            "WSI_ALLOWED_SOURCE_PREFIXES and WSI_ALLOWED_THUMBNAIL_PREFIXES "
            "must be configured before publishing WSI files"
        )
    study_dir = Path(args.study_dir).expanduser().resolve()
    output_dir = (
        Path(args.output_dir).expanduser().resolve()
        if args.output_dir
        else study_dir
    )
    patient_ids = _read_patient_ids(study_dir)

    rows: list[dict] = []
    missing: list[str] = []

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(
                meta_store.get_patient_association_rows,
                patient_id,
                args.warehouse_id,
            ): patient_id
            for patient_id in patient_ids
        }
        for future in as_completed(futures):
            patient_id = futures[future]
            slides = future.result()
            if not slides:
                missing.append(patient_id)
                continue
            for slide in slides:
                rows.append({**slide, "patient_id": patient_id})

    if not rows:
        raise ValueError(f"No WSI association rows found for {args.study_id}")
    meta_path, data_path = write_wsi_study_files(output_dir, args.study_id, rows)
    timeline_meta_path, timeline_data_path, timeline_row_count = (
        write_pathology_timeline_files(output_dir, args.study_id, rows)
    )

    print(
        f"Wrote {len(rows)} canonical WSI rows for {args.study_id} to "
        f"{meta_path} and {data_path}"
        f" (missing {len(missing)} patients)"
    )
    print(
        f"Wrote {timeline_row_count} pathology timeline rows to "
        f"{timeline_meta_path} and {timeline_data_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
