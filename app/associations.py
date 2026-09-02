"""Shared helpers for canonical pathology slide associations."""

from __future__ import annotations

import re
from typing import Any


def derive_block_fields(
    block_id: str | None, block_label: str | None
) -> tuple[int | None, str, str | None]:
    part_number: int | None = None
    source = block_id or ""
    if source:
        match = re.search(r"/(\d+)-([^/]+)$", source)
        if match:
            part_number = int(match.group(1))
            raw_block = match.group(2).strip()
            label = (block_label or raw_block).strip()
            block_number_match = re.match(r"^(\d+)", raw_block)
            block_number = (
                block_number_match.group(1) if block_number_match else raw_block
            )
            return part_number, block_number, label

    label = (block_label or "").strip()
    block_number_match = re.match(r"^(\d+)", label)
    block_number = block_number_match.group(1) if block_number_match else label
    return part_number, block_number, label or None


def build_specimen_key(
    match_level: str, part_number: int | None, block_number: str
) -> str:
    part_token = str(part_number) if part_number is not None else "?"
    if match_level == "BLOCK":
        return f"block::{part_token}::{block_number or '?'}"
    if match_level == "PART":
        return f"part::{part_token}"
    return f"unmatched::{part_token}::{block_number or '?'}"


def association_path_rank(slide_path: str | None) -> int:
    path = slide_path or ""
    if path.startswith("s3://mskmind-bkt/reef-slides/"):
        return 0
    if path.startswith("s3://"):
        return 1
    return 2


def association_match_rank(match_level: str | None) -> int:
    normalized = (match_level or "UNMATCHED").upper()
    return {"BLOCK": 0, "PART": 1, "UNMATCHED": 2}.get(normalized, 3)


def canonical_association_preference(row: dict[str, Any]) -> tuple[object, ...]:
    # Procedure timing remains an upstream association tie-breaker for
    # timeline-file generation; it is intentionally not part of the WSI
    # study-file or hierarchy contracts.
    raw_part_number = row.get("part_number")
    part_number = (
        int(raw_part_number)
        if isinstance(raw_part_number, (int, str)) and str(raw_part_number).isdigit()
        else None
    )
    block_number = str(row.get("block_number") or "").strip()
    if part_number is None or not block_number:
        legacy_part_number, legacy_block_number, _ = derive_block_fields(
            row.get("block_id"), row.get("block_label")
        )
        part_number = part_number if part_number is not None else legacy_part_number
        block_number = block_number or legacy_block_number
    procedure_days = row.get("timeline_start_days")
    if procedure_days is None:
        procedure_days = row.get("procedure_date_days")
    if procedure_days is None:
        procedure_days = row.get("slide_timepoint_days")
    return (
        association_path_rank(row.get("slide_path")),
        association_match_rank(row.get("match_level")),
        0 if row.get("sample_id") else 1,
        str(row.get("sample_id") or "~~~~~~~~"),
        f"{part_number:08d}" if isinstance(part_number, int) else "~~~~~~~~",
        block_number or "~~~~~~~~",
        str(row.get("stain_group") or "~~~~~~~~"),
        str(row.get("stain_name") or "~~~~~~~~"),
        0 if row.get("part_description") else 1,
        str(row.get("part_description") or "~~~~~~~~"),
        0 if procedure_days is not None else 1,
        str(row.get("image_id") or ""),
    )


def canonicalize_association_rows(
    rows: list[dict[str, Any]], key_fields: tuple[str, ...]
) -> list[dict[str, Any]]:
    best_by_key: dict[tuple[str, ...], dict[str, Any]] = {}
    for row in rows:
        key = tuple(str(row.get(field) or "").strip() for field in key_fields)
        if not all(key):
            continue
        existing = best_by_key.get(key)
        if existing is None or canonical_association_preference(row) < canonical_association_preference(existing):
            best_by_key[key] = row
    return list(best_by_key.values())
