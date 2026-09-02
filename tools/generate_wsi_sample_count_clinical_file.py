#!/usr/bin/env python3
"""Generate sample-level WSI count clinical files from WSI hierarchy data."""

from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path

from app.config import settings
from app.constants import CANONICAL_ASSOCIATION_TABLE
from app.meta_store import run_query

try:
    from tools.wsi_study_format import read_wsi_study
except ModuleNotFoundError:  # Direct execution from the tools directory.
    from wsi_study_format import read_wsi_study


ATTRIBUTE_COLUMNS = [
    (
        "WSI_SAMPLE_SLIDE_COUNT",
        "WSI Slides",
        "Number of associated WSI slides for the sample.",
    ),
    (
        "WSI_SAMPLE_PART_MATCHED_SLIDE_COUNT",
        "WSI Slides, Part-matched",
        "Number of part-matched WSI slides for the sample.",
    ),
    (
        "WSI_SAMPLE_BLOCK_MATCHED_SLIDE_COUNT",
        "WSI Slides, Block-matched",
        "Number of block-matched WSI slides for the sample.",
    ),
]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study-id", required=True, help="Cancer study identifier.")
    parser.add_argument(
        "--wsi-meta",
        help="Input meta_wsi.txt; its data_wsi.txt supplies the WSI associations.",
    )
    parser.add_argument("--study-dir", required=True, help="Target study directory.")
    parser.add_argument(
        "--warehouse-id",
        default=settings.databricks_warehouse_id,
        help="Databricks SQL warehouse id for live canonical aggregation.",
    )
    parser.add_argument(
        "--sample-file",
        default="data_clinical_sample.txt",
        help="Study sample file used to define the loaded cohort for live canonical aggregation.",
    )
    parser.add_argument(
        "--merge-into-sample-file",
        action="store_true",
        help="Merge WSI count columns into the primary study sample clinical file instead of writing a separate sample attribute file.",
    )
    parser.add_argument(
        "--meta-filename",
        default="meta_clinical_sample_wsi_counts.txt",
        help="Output meta filename within the study directory.",
    )
    parser.add_argument(
        "--data-filename",
        default="data_clinical_sample_wsi_counts.txt",
        help="Output data filename within the study directory.",
    )
    return parser.parse_args()


def _empty_counts() -> dict[str, int]:
    return {
        "WSI_SAMPLE_SLIDE_COUNT": 0,
        "WSI_SAMPLE_PART_MATCHED_SLIDE_COUNT": 0,
        "WSI_SAMPLE_BLOCK_MATCHED_SLIDE_COUNT": 0,
    }


def _load_counts(meta_path: Path, study_id: str) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = defaultdict(_empty_counts)
    file_study_id, slides = read_wsi_study(meta_path)
    if file_study_id != study_id:
        raise ValueError(
            f"WSI study identifier {file_study_id!r} does not match {study_id!r}"
        )
    for slide in slides:
        sample_id = slide.get("sample_id")
        if not sample_id:
            continue
        counts[str(sample_id)]["WSI_SAMPLE_SLIDE_COUNT"] += 1
        match_level = str(slide.get("match_level") or "").upper()
        if match_level == "PART":
            counts[str(sample_id)]["WSI_SAMPLE_PART_MATCHED_SLIDE_COUNT"] += 1
        elif match_level == "BLOCK":
            counts[str(sample_id)]["WSI_SAMPLE_BLOCK_MATCHED_SLIDE_COUNT"] += 1
    return counts


def _read_study_sample_ids(path: Path) -> list[str]:
    lines = [
        line.rstrip("\n")
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return [line.split("\t", 1)[0] for line in lines[5:]]


def _quoted_sample_ids(sample_ids: list[str]) -> str:
    return ", ".join("'" + sample_id.replace("'", "''") + "'" for sample_id in sample_ids)


def _load_counts_from_live_canonical(
    sample_ids: list[str], warehouse_id: str
) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}

    for start in range(0, len(sample_ids), 500):
        batch = sample_ids[start : start + 500]
        sql = f"""
SELECT
    sample_id,
    COUNT(*) AS WSI_SAMPLE_SLIDE_COUNT,
    SUM(CASE WHEN UPPER(match_level) = 'PART' THEN 1 ELSE 0 END) AS WSI_SAMPLE_PART_MATCHED_SLIDE_COUNT,
    SUM(CASE WHEN UPPER(match_level) = 'BLOCK' THEN 1 ELSE 0 END) AS WSI_SAMPLE_BLOCK_MATCHED_SLIDE_COUNT
FROM {CANONICAL_ASSOCIATION_TABLE}
WHERE sample_id IN ({_quoted_sample_ids(batch)})
GROUP BY sample_id
"""
        for row in run_query(sql, warehouse_id):
            counts[str(row["sample_id"])] = {
                "WSI_SAMPLE_SLIDE_COUNT": int(row["WSI_SAMPLE_SLIDE_COUNT"] or 0),
                "WSI_SAMPLE_PART_MATCHED_SLIDE_COUNT": int(
                    row["WSI_SAMPLE_PART_MATCHED_SLIDE_COUNT"] or 0
                ),
                "WSI_SAMPLE_BLOCK_MATCHED_SLIDE_COUNT": int(
                    row["WSI_SAMPLE_BLOCK_MATCHED_SLIDE_COUNT"] or 0
                ),
            }

    return counts


def _write_meta_file(path: Path, study_id: str, data_filename: str) -> None:
    path.write_text(
        "\n".join(
            [
                f"cancer_study_identifier: {study_id}",
                "genetic_alteration_type: CLINICAL",
                "datatype: SAMPLE_ATTRIBUTES",
                f"data_filename: {data_filename}",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _write_data_file(path: Path, counts: dict[str, dict[str, int]]) -> None:
    headers = ["SAMPLE_ID", *[attribute_id for attribute_id, _, _ in ATTRIBUTE_COLUMNS]]
    display_names = ["#Sample Identifier", *[display_name for _, display_name, _ in ATTRIBUTE_COLUMNS]]
    descriptions = [
        "#A unique sample identifier.",
        *[description for _, _, description in ATTRIBUTE_COLUMNS],
    ]
    datatypes = ["#STRING", "#NUMBER", "#NUMBER", "#NUMBER"]
    priorities = ["#1", "#1", "#1", "#1"]

    lines = [
        "\t".join(display_names),
        "\t".join(descriptions),
        "\t".join(datatypes),
        "\t".join(priorities),
        "\t".join(headers),
    ]

    for sample_id in sorted(counts):
        row = [sample_id] + [str(counts[sample_id][attribute_id]) for attribute_id, _, _ in ATTRIBUTE_COLUMNS]
        lines.append("\t".join(row))

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _merge_into_sample_file(path: Path, counts: dict[str, dict[str, int]]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) < 5:
        raise ValueError(f"Unexpected sample clinical file format: {path}")

    headers = [attribute_id for attribute_id, _, _ in ATTRIBUTE_COLUMNS]
    display_names = [display_name for _, display_name, _ in ATTRIBUTE_COLUMNS]
    descriptions = [description for _, _, description in ATTRIBUTE_COLUMNS]
    datatypes = ["NUMBER"] * len(headers)
    priorities = ["1"] * len(headers)

    parsed = [line.split("\t") for line in lines]
    existing_header_set = set(parsed[4])
    if any(header in existing_header_set for header in headers):
        raise ValueError(f"WSI count columns already exist in {path}")

    parsed[0].extend(display_names)
    parsed[1].extend(descriptions)
    parsed[2].extend(datatypes)
    parsed[3].extend(priorities)
    parsed[4].extend(headers)

    sample_id_index = parsed[4].index("SAMPLE_ID")

    for row in parsed[5:]:
        if len(row) <= sample_id_index:
            continue
        sample_id = row[sample_id_index]
        sample_counts = counts.get(sample_id, _empty_counts())
        row.extend(str(sample_counts[attribute_id]) for attribute_id in headers)

    path.write_text("\n".join("\t".join(row) for row in parsed) + "\n", encoding="utf-8")


def main() -> int:
    args = _parse_args()
    study_dir = Path(args.study_dir).expanduser().resolve()
    output_meta_path = study_dir / args.meta_filename
    data_path = study_dir / args.data_filename
    if args.wsi_meta:
        input_meta_path = Path(args.wsi_meta).expanduser().resolve()
        counts = _load_counts(input_meta_path, args.study_id)
    else:
        sample_ids = _read_study_sample_ids(study_dir / args.sample_file)
        counts = _load_counts_from_live_canonical(sample_ids, args.warehouse_id)
    if args.merge_into_sample_file:
        sample_file_path = study_dir / args.sample_file
        _merge_into_sample_file(sample_file_path, counts)
        print(f"Merged WSI count columns into {sample_file_path}")
    else:
        _write_meta_file(output_meta_path, args.study_id, data_path.name)
        _write_data_file(data_path, counts)
        print(f"Wrote {len(counts)} sample WSI count rows to {data_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
