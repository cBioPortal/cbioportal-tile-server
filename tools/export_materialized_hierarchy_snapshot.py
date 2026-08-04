#!/usr/bin/env python3
"""Export canonical pathology rows as a study-scoped JSONL snapshot."""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from app.config import settings
from app import meta_store


def _read_patient_ids(study_dir: Path) -> list[str]:
    data_file = study_dir / "data_clinical_patient.txt"
    lines = [
        line.rstrip("\n")
        for line in data_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return [line.split("\t", 1)[0] for line in lines[4:]]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study-dir", required=True, help="Path to the cBioPortal study directory.")
    parser.add_argument("--study-id", required=True, help="Study identifier to embed in each JSONL row.")
    parser.add_argument("--output", required=True, help="Output JSONL path.")
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
    study_dir = Path(args.study_dir).expanduser().resolve()
    output = Path(args.output).expanduser().resolve()
    patient_ids = _read_patient_ids(study_dir)

    rows: dict[str, dict] = {}
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
            rows[patient_id] = {
                "study_id": args.study_id,
                "patient_id": patient_id,
                "slides": slides,
            }

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as handle:
        for patient_id in sorted(rows):
            handle.write(json.dumps(rows[patient_id], separators=(",", ":")))
            handle.write("\n")

    print(
        f"Wrote {len(rows)} canonical association rows for {args.study_id} to {output}"
        f" (missing {len(missing)} patients)"
    )
    if missing:
        print("Missing patients:")
        for patient_id in missing[:50]:
            print(patient_id)
        if len(missing) > 50:
            print(f"... and {len(missing) - 50} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
