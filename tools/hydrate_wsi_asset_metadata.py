#!/usr/bin/env python3
"""Hydrate a WSI study file from the published thumbnail registry.

The pathology association export and the thumbnail renderer are independent
jobs.  During a partial export it is therefore possible to retain the slide
hierarchy while losing the source/metadata/thumbnail fields that make a row
servable.  This command joins those fields back by ``IMAGE_ID`` and source
URI, preserving rows without a complete registry record as non-servable.

The input registry is JSONL in the same shape emitted by
``generate_slide_thumbnails.py``.  It is intentionally a separate step from
the importer so an incomplete registry can never make a slide appear
servable by accident.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

# Support both ``python -m tools...`` from the repository root and the
# documented ``python tools/...`` invocation from the tile-server directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.metadata_contract import validate_tile_metadata
from tools.wsi_study_format import read_wsi_study, write_wsi_study_files


def _read_registry(path: Path) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict) or not str(value.get("image_id") or "").strip():
            raise ValueError(f"registry line {line_number} must contain image_id")
        image_id = str(value["image_id"]).strip()
        if image_id in records:
            raise ValueError(f"registry contains duplicate image_id {image_id}")
        records[image_id] = value
    return records


def _complete_record(record: dict[str, Any]) -> bool:
    if str(record.get("status") or "").lower() != "success":
        return False
    if not str(record.get("source_path") or "").strip():
        return False
    if not str(record.get("artifact_uri") or "").strip():
        return False
    if not str(record.get("tile_metadata_json") or "").strip():
        return False
    try:
        width = int(record.get("width") or 0)
        height = int(record.get("height") or 0)
        metadata = json.loads(str(record["tile_metadata_json"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if width <= 0 or height <= 0:
        return False
    valid, _ = validate_tile_metadata(metadata, allow_legacy=True)
    return valid


def hydrate_rows(
    rows: list[dict[str, Any]], registry: dict[str, dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    stats = {
        "rows": len(rows),
        "hydrated": 0,
        "unchanged": 0,
        "incomplete": 0,
        "source_mismatch": 0,
    }
    hydrated: list[dict[str, Any]] = []
    for row in rows:
        image_id = str(row.get("image_id") or "").strip()
        record = registry.get(image_id)
        if not record or not _complete_record(record):
            stats["incomplete"] += 1
            hydrated.append(row)
            continue

        registry_source = str(record.get("source_path") or "").strip()
        current_source = str(row.get("slide_path") or "").strip()
        if current_source and current_source != registry_source:
            stats["source_mismatch"] += 1
            hydrated.append(row)
            continue

        updated = dict(row)
        updated["slide_path"] = registry_source
        updated["can_serve_tiles"] = True
        updated["tile_metadata_json"] = str(record["tile_metadata_json"])
        updated["thumbnail_url"] = str(record["artifact_uri"])
        updated["thumbnail_width"] = int(record["width"])
        updated["thumbnail_height"] = int(record["height"])
        updated["thumbnail_content_type"] = str(record.get("content_type") or "image/jpeg")
        if all(
            updated.get(key) == row.get(key)
            for key in (
                "slide_path",
                "can_serve_tiles",
                "tile_metadata_json",
                "thumbnail_url",
                "thumbnail_width",
                "thumbnail_height",
                "thumbnail_content_type",
            )
        ):
            stats["unchanged"] += 1
        else:
            stats["hydrated"] += 1
        hydrated.append(updated)
    return hydrated, stats


def _write_atomically(
    output_dir: Path,
    study_id: str,
    rows: list[dict[str, Any]],
    destination: Path,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="wsi-hydrate-", dir=str(output_dir)) as temp_dir:
        _, temp_data = write_wsi_study_files(Path(temp_dir), study_id, rows)
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temp_data, destination)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--meta-wsi", type=Path, required=True)
    parser.add_argument("--registry-jsonl", type=Path, required=True)
    parser.add_argument("--output-data-wsi", type=Path, required=True)
    parser.add_argument(
        "--report-json",
        type=Path,
        help="optional JSON report listing incomplete/source-mismatched image IDs",
    )
    parser.add_argument(
        "--fail-on-incomplete",
        action="store_true",
        help="return non-zero after writing when any row lacks a complete registry record",
    )
    args = parser.parse_args(argv)

    study_id, rows = read_wsi_study(args.meta_wsi)
    registry = _read_registry(args.registry_jsonl)
    hydrated, stats = hydrate_rows(rows, registry)
    _write_atomically(
        args.output_data_wsi.parent,
        study_id,
        hydrated,
        args.output_data_wsi,
    )
    if args.report_json:
        incomplete_image_ids = [
            str(row.get("image_id") or "")
            for row in rows
            if not _complete_record(registry.get(str(row.get("image_id") or ""), {}))
        ]
        source_mismatch_image_ids = [
            str(row.get("image_id") or "")
            for row in rows
            if _complete_record(registry.get(str(row.get("image_id") or ""), {}))
            and str(row.get("slide_path") or "").strip()
            and str(row.get("slide_path") or "").strip()
            != str(registry[str(row.get("image_id") or "")].get("source_path") or "").strip()
        ]
        report = {
            "study_id": study_id,
            "meta_wsi": str(args.meta_wsi),
            "output_data_wsi": str(args.output_data_wsi),
            "stats": stats,
            "incomplete_image_ids": incomplete_image_ids,
            "source_mismatch_image_ids": source_mismatch_image_ids,
        }
        args.report_json.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.report_json.with_suffix(args.report_json.suffix + ".tmp")
        temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, args.report_json)
    print(json.dumps(stats, sort_keys=True))
    return 2 if args.fail_on_incomplete and (stats["incomplete"] or stats["source_mismatch"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
