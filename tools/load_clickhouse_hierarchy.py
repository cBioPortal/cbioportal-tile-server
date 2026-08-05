#!/usr/bin/env python3
"""Load canonical normalized WSI associations into cBioPortal ClickHouse.

Each JSONL row contains a study, patient, and flat canonical slide rows. Its
portal identifiers are resolved to cBioPortal internal IDs and only
pathology-specific rows are written. Every table insert uses one release ID;
the release insert is the visibility boundary.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


TABLE_DDL = """
CREATE TABLE IF NOT EXISTS wsi_release (
    cancer_study_id Int64,
    release_id String,
    release_version UInt64,
    released_at DateTime64(6)
) ENGINE = MergeTree() ORDER BY (cancer_study_id, released_at, release_id);
CREATE TABLE IF NOT EXISTS wsi_release_patient (
    cancer_study_id Int64, release_id String, patient_id Int64,
    reference_sample_id Nullable(Int64)
) ENGINE = MergeTree() ORDER BY (cancer_study_id, release_id, patient_id);
CREATE TABLE IF NOT EXISTS wsi_part (
    cancer_study_id Int64, release_id String, patient_id Int64,
    part_key String, part_number Nullable(String),
    part_designator Nullable(String), part_type Nullable(String),
    part_description Nullable(String), subspecialty Nullable(String),
    path_dx_title Nullable(String)
) ENGINE = MergeTree() ORDER BY (cancer_study_id, release_id, patient_id, part_key);
CREATE TABLE IF NOT EXISTS wsi_block (
    cancer_study_id Int64, release_id String, patient_id Int64,
    part_key String, block_key String,
    block_number Nullable(String), block_label Nullable(String)
) ENGINE = MergeTree() ORDER BY (cancer_study_id, release_id, patient_id, part_key, block_key);
CREATE TABLE IF NOT EXISTS wsi_slide (
    cancer_study_id Int64, release_id String, patient_id Int64,
    image_id String, stain_name Nullable(String),
    stain_group Nullable(String), is_hne Bool, is_ihc Bool,
    magnification Nullable(String), file_size_bytes Nullable(UInt64),
    can_serve_tiles Bool, barcode Nullable(String), slide_type Nullable(String)
) ENGINE = MergeTree() ORDER BY (cancer_study_id, release_id, patient_id, image_id);
CREATE TABLE IF NOT EXISTS wsi_slide_placement (
    cancer_study_id Int64, release_id String, patient_id Int64,
    image_id String, part_key String, block_key String,
    sample_id Nullable(Int64), match_level String, specimen_key String,
    procedure_date_days Nullable(Int32), timepoint_source Nullable(String)
) ENGINE = MergeTree() ORDER BY (cancer_study_id, release_id, patient_id, image_id, part_key, block_key);
"""

INSERT_TABLES = (
    "wsi_release_patient",
    "wsi_part",
    "wsi_block",
    "wsi_slide",
    "wsi_slide_placement",
)


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path, help="Canonical WSI association JSONL snapshot")
    parser.add_argument("--version", type=int, required=True)
    parser.add_argument("--url", default=os.getenv("CLICKHOUSE_URL", "http://localhost:8123"))
    parser.add_argument("--database", default=os.getenv("CLICKHOUSE_DATABASE", "cbioportal"))
    parser.add_argument("--user", default=os.getenv("CLICKHOUSE_USER", "default"))
    parser.add_argument("--password", default=os.getenv("CLICKHOUSE_PASSWORD", ""))
    parser.add_argument("--resource-index", type=Path, default=(
        Path(os.environ["WSI_RESOURCE_INDEX_FILE"])
        if os.getenv("WSI_RESOURCE_INDEX_FILE") else None
    ))
    return parser.parse_args()


class ClickHouse:
    def __init__(self, url: str, database: str, user: str, password: str):
        self.url = url.rstrip("/")
        self.database = database
        self.auth = base64.b64encode(f"{user}:{password}".encode()).decode()

    def _request(self, query: str, body: bytes | None = None) -> bytes:
        params = urlencode({"database": self.database, "query": query})
        request = Request(f"{self.url}/?{params}", data=body, method="POST")
        request.add_header("Authorization", f"Basic {self.auth}")
        request.add_header("Content-Type", "application/json" if body else "text/plain")
        with urlopen(request, timeout=120) as response:
            return response.read()

    def execute(self, query: str, body: bytes | None = None) -> None:
        self._request(query, body)

    def query_json(self, query: str) -> list[dict]:
        output = self._request(query + " FORMAT JSONEachRow").decode()
        return [json.loads(line) for line in output.splitlines() if line.strip()]


def _release_id() -> str:
    return f"{time.time_ns():020d}-{uuid.uuid4().hex}"


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _coerce_bool(value: object, *, default: bool | None = None) -> bool | None:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1"}:
            return True
        if normalized in {"false", "0"}:
            return False
    raise ValueError(f"invalid boolean value: {value!r}")


def _read_snapshot(snapshot: Path) -> list[tuple[str, str, dict]]:
    parsed: list[tuple[str, str, dict]] = []
    seen: set[tuple[str, str]] = set()
    with snapshot.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
                study_id, patient_id = value.get("study_id"), value.get("patient_id")
                slides = value.get("slides")
                if not isinstance(study_id, str) or not study_id.strip() or not isinstance(patient_id, str) or not patient_id.strip() or not isinstance(slides, list):
                    raise ValueError("snapshot row needs non-empty string study_id, patient_id, and slides")
                hierarchy = _hierarchy_from_canonical_rows(slides)
                if (study_id, patient_id) in seen:
                    raise ValueError(f"duplicate study_id/patient_id in snapshot: {study_id}/{patient_id}")
                seen.add((study_id, patient_id))
                parsed.append((study_id, patient_id, hierarchy))
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise ValueError(f"Invalid snapshot line {line_number}: {error}") from error
    if not parsed:
        raise ValueError("snapshot must contain at least one hierarchy row")
    return parsed


def _hierarchy_from_canonical_rows(rows: list[dict]) -> dict:
    """Build a transient loader shape from flat canonical pathology rows."""
    samples: dict[str | None, dict] = {}
    associations: list[dict] = []
    reference_samples: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("image_id") is None:
            raise ValueError("canonical slide rows need an image_id")
        part_key = row.get("part_key")
        block_key = row.get("block_key")
        if not isinstance(part_key, str) or not part_key.strip() or "?" in part_key:
            raise ValueError("canonical slide rows need a non-empty part_key")
        if not isinstance(block_key, str) or not block_key.strip() or "?" in block_key:
            raise ValueError("canonical slide rows need a non-empty block_key")
        match_level = row.get("match_level")
        if not isinstance(match_level, str) or match_level.upper() not in {"BLOCK", "PART", "UNMATCHED"}:
            raise ValueError("canonical slide rows need a valid match_level")
        if not isinstance(row.get("specimen_key"), str) or not row["specimen_key"].strip():
            raise ValueError("canonical slide rows need a non-empty specimen_key")
        can_serve_tiles = _coerce_bool(row.get("can_serve_tiles"))
        if can_serve_tiles is None:
            raise ValueError("canonical slide rows need a boolean can_serve_tiles")
        slide_path = row.get("slide_path")
        if can_serve_tiles and (
            not isinstance(slide_path, str) or not slide_path.startswith("s3://")
        ):
            raise ValueError("tile-servable canonical slide rows need an s3:// slide_path")
        reference_sample = row.get("reference_sample_id")
        if reference_sample not in (None, "", "UNMATCHED"):
            reference_samples.add(str(reference_sample))
        sample_id = row.get("sample_id") or None
        if match_level.upper() == "UNMATCHED" and sample_id is not None:
            raise ValueError("unmatched canonical slide rows need a null sample_id")
        if match_level.upper() != "UNMATCHED" and sample_id is None:
            raise ValueError("matched canonical slide rows need a sample_id")
        sample = samples.setdefault(sample_id, {"sample_id": sample_id, "parts": {}})
        part_number = row.get("part_number")
        part_fields = {
            "part_key": part_key,
            "part_number": part_number,
            "part_designator": row.get("part_designator"),
            "part_type": row.get("part_type"),
            "part_description": row.get("part_description"),
            "subspecialty": row.get("subspecialty"),
            "path_dx_title": row.get("path_dx_title"),
        }
        part = sample["parts"].setdefault(part_key, {**part_fields, "blocks": {}})
        block_fields = {
            "block_key": block_key,
            "block_number": row.get("block_number"),
            "block_label": row.get("block_label"),
        }
        block = part["blocks"].setdefault(block_key, {**block_fields, "slides": []})
        slide = {
            key: row.get(key)
            for key in (
                "image_id", "stain_name", "stain_group", "is_hne", "is_ihc",
                "magnification", "file_size_bytes", "can_serve_tiles", "barcode", "slide_type",
                "slide_path",
            )
        }
        slide["is_hne"] = _coerce_bool(slide.get("is_hne"), default=False)
        slide["is_ihc"] = _coerce_bool(slide.get("is_ihc"), default=False)
        slide["can_serve_tiles"] = can_serve_tiles
        block["slides"].append(slide)
        associations.append({
            key: row.get(key)
            for key in (
                "image_id", "sample_id", "match_level", "specimen_key",
                "procedure_date_days", "timepoint_source",
            )
        })
    if len(reference_samples) > 1:
        raise ValueError("canonical rows contain conflicting reference samples")
    return {
        "reference_sample_id": next(iter(reference_samples), None),
        "samples": [
            {
                **sample,
                "parts": [
                    {**part, "blocks": list(part["blocks"].values())}
                    for part in sample["parts"].values()
                ],
            }
            for sample in samples.values()
        ],
        "slide_associations": associations,
    }


def _resolve_identities(clickhouse: ClickHouse, parsed: list[tuple[str, str, dict]]) -> dict[tuple[str, str], dict]:
    keys = sorted({(study, patient) for study, patient, _ in parsed})
    values = ", ".join(f"({_sql_literal(study)}, {_sql_literal(patient)})" for study, patient in keys)
    rows = clickhouse.query_json(
        "SELECT cs.cancer_study_identifier AS study_id, cs.cancer_study_id AS study_internal_id, "
        "p.stable_id AS patient_id, p.internal_id AS patient_internal_id "
        "FROM cancer_study cs INNER JOIN patient p ON p.cancer_study_id = cs.cancer_study_id "
        f"WHERE (cs.cancer_study_identifier, p.stable_id) IN ({values})"
    )
    identities = {
        (str(row["study_id"]), str(row["patient_id"])): {
            "study_internal_id": int(row["study_internal_id"]),
            "patient_internal_id": int(row["patient_internal_id"]),
        }
        for row in rows
    }
    missing = sorted(set(keys) - set(identities))
    if missing:
        raise ValueError(f"unknown cBioPortal study/patient references: {missing}")

    sample_keys: set[tuple[str, str, str]] = set()
    for study_id, patient_id, hierarchy in parsed:
        sample_ids = {
            str(sample["sample_id"])
            for sample in hierarchy.get("samples", [])
            if isinstance(sample, dict)
            and sample.get("sample_id") not in (None, "", "UNMATCHED")
        }
        reference_sample = hierarchy.get(
            "reference_sample_id", hierarchy.get("referenceSampleId")
        )
        if reference_sample not in (None, "", "UNMATCHED"):
            sample_ids.add(str(reference_sample))
        sample_keys.update((study_id, patient_id, sample_id) for sample_id in sample_ids)
    if not sample_keys:
        return identities
    sample_values = ", ".join(
        f"({_sql_literal(study_id)}, {_sql_literal(patient_id)}, {_sql_literal(sample_id)})"
        for study_id, patient_id, sample_id in sorted(sample_keys)
    )
    sample_rows = clickhouse.query_json(
        "SELECT cs.cancer_study_identifier AS study_id, p.stable_id AS patient_id, "
        "s.stable_id AS sample_id, s.internal_id AS sample_internal_id "
        "FROM sample s INNER JOIN patient p ON p.internal_id = s.patient_id "
        "INNER JOIN cancer_study cs ON cs.cancer_study_id = p.cancer_study_id "
        f"WHERE (cs.cancer_study_identifier, p.stable_id, s.stable_id) IN ({sample_values})"
    )
    resolved_keys: set[tuple[str, str, str]] = set()
    for row in sample_rows:
        key = (
            str(row["study_id"]),
            str(row["patient_id"]),
            str(row["sample_id"]),
        )
        if key not in sample_keys:
            continue
        identity_key = key[:2]
        identity = identities[identity_key]
        if key in resolved_keys:
            raise ValueError(
                f"ambiguous sample reference: {key[0]}/{key[1]}/{key[2]}"
            )
        resolved_keys.add(key)
        identity.setdefault("samples", {})[key[2]] = int(row["sample_internal_id"])
    missing_samples = sorted(sample_keys - resolved_keys)
    if missing_samples:
        raise ValueError(f"unknown sample references: {missing_samples}")
    for study_id, patient_id, hierarchy in parsed:
        known = identities[(study_id, patient_id)].setdefault("samples", {})
        for sample in hierarchy.get("samples", []):
            sample_id = sample.get("sample_id") if isinstance(sample, dict) else None
            if sample_id not in (None, "", "UNMATCHED") and sample_id not in known:
                raise ValueError(f"unknown sample reference: {study_id}/{patient_id}/{sample_id}")
    return identities


def _normalize(parsed: list[tuple[str, str, dict]], identities: dict[tuple[str, str], dict], version: int, release_id: str) -> tuple[dict[str, list[dict]], dict[str, dict[str, dict[str, set[str]]]], set[int]]:
    tables = {table: [] for table in INSERT_TABLES}
    resource_rows: list[tuple[str, str, dict]] = []
    studies: set[int] = set()
    for study_id, patient_id, hierarchy in parsed:
        identity = identities[(study_id, patient_id)]
        study_internal = identity["study_internal_id"]
        patient_internal = identity["patient_internal_id"]
        studies.add(study_internal)
        sample_map = identity.get("samples", {})
        reference_sample = hierarchy.get(
            "reference_sample_id", hierarchy.get("referenceSampleId")
        )
        if reference_sample in ("", "UNMATCHED"):
            reference_sample = None
        if reference_sample is not None and reference_sample not in sample_map:
            raise ValueError(f"unknown reference sample: {study_id}/{patient_id}/{reference_sample}")
        tables["wsi_release_patient"].append({
            "cancer_study_id": study_internal, "release_id": release_id,
            "patient_id": patient_internal,
            "reference_sample_id": sample_map.get(reference_sample) if reference_sample else None,
        })
        raw_associations = hierarchy.get("slide_associations", [])
        if not isinstance(raw_associations, list) or any(
            not isinstance(row, dict) or row.get("image_id") is None
            for row in raw_associations
        ):
            raise ValueError(f"invalid slide association in {study_id}/{patient_id}")
        associations = {str(row["image_id"]): row for row in raw_associations}
        if len(associations) != len(raw_associations):
            raise ValueError(f"duplicate slide association in {study_id}/{patient_id}")
        parts: dict[str, dict] = {}
        blocks: dict[tuple[str, str], dict] = {}
        slides: dict[str, dict] = {}
        placements: dict[str, dict] = {}
        for sample in hierarchy.get("samples", []):
            if not isinstance(sample, dict):
                raise ValueError(f"invalid sample group in {study_id}/{patient_id}")
            raw_sample_id = sample.get("sample_id")
            sample_internal = None if raw_sample_id in (None, "", "UNMATCHED") else sample_map.get(raw_sample_id)
            for part in sample.get("parts", []):
                part_number = part.get("part_number")
                part_key = str(part["part_key"])
                part_row = {
                    "cancer_study_id": study_internal, "release_id": release_id,
                    "patient_id": patient_internal,
                    "part_key": part_key, "part_number": str(part_number) if part_number is not None else None,
                    "part_designator": part.get("part_designator"), "part_type": part.get("part_type"),
                    "part_description": part.get("part_description"), "subspecialty": part.get("subspecialty"),
                    "path_dx_title": part.get("path_dx_title"),
                }
                # Source rows may repeat a key with variant descriptive metadata; placements remain lossless.
                parts.setdefault(part_key, part_row)
                for block in part.get("blocks", []):
                    block_number = block.get("block_number")
                    block_key = str(block["block_key"])
                    block_row = {
                        "cancer_study_id": study_internal, "release_id": release_id,
                        "patient_id": patient_internal,
                        "part_key": part_key, "block_key": block_key,
                        "block_number": str(block_number) if block_number is not None else None,
                        "block_label": block.get("block_label"),
                    }
                    block_identity = (part_key, block_key)
                    blocks.setdefault(block_identity, block_row)
                    for slide in block.get("slides", []):
                        image_id = str(slide.get("image_id")) if slide.get("image_id") is not None else ""
                        if not image_id:
                            raise ValueError(f"slide is missing image_id in {study_id}/{patient_id}")
                        association = associations.get(image_id, {})
                        association_sample_id = association.get("sample_id")
                        if association_sample_id in ("", "UNMATCHED"):
                            association_sample_id = None
                        if association_sample_id is not None and association_sample_id not in sample_map:
                            raise ValueError(
                                f"unknown association sample reference: {study_id}/{patient_id}/{association_sample_id}"
                            )
                        if association_sample_id is not None and sample_internal != sample_map[association_sample_id]:
                            raise ValueError(
                                f"association sample does not match slide placement: {study_id}/{patient_id}/{image_id}"
                            )
                        slide_row = {
                            "cancer_study_id": study_internal, "release_id": release_id,
                            "patient_id": patient_internal,
                            "image_id": image_id, "stain_name": slide.get("stain_name"),
                            "stain_group": slide.get("stain_group"), "is_hne": bool(slide.get("is_hne", False)),
                            "is_ihc": bool(slide.get("is_ihc", False)), "magnification": slide.get("magnification"),
                            "file_size_bytes": int(slide["file_size_bytes"]) if slide.get("file_size_bytes") not in (None, "") else None,
                            "can_serve_tiles": bool(slide.get("can_serve_tiles", False)),
                            "barcode": slide.get("barcode"), "slide_type": slide.get("slide_type"),
                        }
                        if image_id in slides and slides[image_id] != slide_row:
                            raise ValueError(f"duplicate/conflicting slide: {study_id}/{patient_id}/{image_id}")
                        slides[image_id] = slide_row
                        match_level = str(association["match_level"]).upper()
                        placement = {
                            "cancer_study_id": study_internal, "release_id": release_id,
                            "patient_id": patient_internal,
                            "image_id": image_id, "part_key": part_key, "block_key": block_key,
                            "sample_id": sample_internal, "match_level": match_level,
                            "specimen_key": association["specimen_key"],
                            "procedure_date_days": (
                                int(association["procedure_date_days"])
                                if association.get("procedure_date_days") not in (None, "")
                                else None
                            ),
                            "timepoint_source": association.get("timepoint_source"),
                        }
                        if image_id in placements and placements[image_id] != placement:
                            raise ValueError(f"duplicate/conflicting slide placement: {study_id}/{patient_id}/{image_id}")
                        placements[image_id] = placement
        tables["wsi_part"].extend(parts.values())
        tables["wsi_block"].extend(blocks.values())
        tables["wsi_slide"].extend(slides.values())
        tables["wsi_slide_placement"].extend(placements.values())
        unknown_associations = set(associations) - set(slides)
        if unknown_associations:
            raise ValueError(
                f"slide association references unknown slide: {study_id}/{patient_id}/{sorted(unknown_associations)[0]}"
            )
        resource_rows.append((study_id, patient_id, hierarchy))
    return tables, _resource_index_rows(resource_rows), studies


def _resource_index_rows(parsed: list[tuple[str, str, dict]]) -> dict[str, dict[str, object]]:
    studies: dict[str, dict[str, object]] = {}
    for study_id, patient_id, hierarchy in parsed:
        resources = studies.setdefault(
            study_id,
            {"patients": set(), "samples": set(), "slides": {}},
        )
        resources["patients"].add(patient_id)  # type: ignore[union-attr]
        reference_sample = hierarchy.get(
            "reference_sample_id", hierarchy.get("referenceSampleId")
        )
        if reference_sample not in (None, "", "UNMATCHED"):
            resources["samples"].add(str(reference_sample))  # type: ignore[union-attr]
        for sample in hierarchy.get("samples", []):
            if isinstance(sample, dict) and sample.get("sample_id") not in (None, "", "UNMATCHED"):
                resources["samples"].add(str(sample["sample_id"]))  # type: ignore[union-attr]
            for part in sample.get("parts", []) if isinstance(sample, dict) else []:
                for block in part.get("blocks", []) if isinstance(part, dict) else []:
                    for slide in block.get("slides", []) if isinstance(block, dict) else []:
                        if slide.get("image_id") is not None:
                            image_id = str(slide["image_id"])
                            binding = {
                                "patient_id": patient_id,
                                "source_path": slide.get("slide_path"),
                            }
                            slides = resources["slides"]  # type: ignore[assignment]
                            previous = slides.get(image_id)
                            if previous is not None and previous != binding:
                                raise ValueError(
                                    f"conflicting slide binding: {study_id}/{image_id}"
                                )
                            slides[image_id] = binding
    return studies


def _read_resource_index(path: Path | None) -> dict | None:
    if path is None or not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("version") != 2:
        raise ValueError("resource index must be a version 2 JSON object")
    return value


def _merge_resource_index(rows: dict[str, dict[str, object]], release_id: str, existing: dict | None) -> dict:
    studies = {
        str(study_id): {
            resource_type: {str(value) for value in (resources.get(resource_type) or [])}
            for resource_type in ("patients", "samples")
        } | {
            "slides": {
                str(image_id): dict(binding)
                for image_id, binding in (resources.get("slides") or {}).items()
                if isinstance(binding, dict)
            }
        }
        for study_id, resources in ((existing or {}).get("studies") or {}).items()
        if isinstance(resources, dict)
    }
    studies.update(rows)
    return {
        "version": 2, "release_id": release_id,
        "generated_at": datetime.now(UTC).isoformat(),
        "studies": {
            study_id: {
                resource_type: (
                    sorted(values)
                    if resource_type != "slides"
                    else {key: values[key] for key in sorted(values)}
                )
                for resource_type, values in resources.items()
            }
            for study_id, resources in sorted(studies.items())
        },
    }


def _publish_resource_index(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _insert_rows(clickhouse: ClickHouse, table: str, rows: list[dict]) -> None:
    if not rows:
        return
    clickhouse.execute(
        f"INSERT INTO {table} FORMAT JSONEachRow",
        ("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n").encode(),
    )


def load(snapshot: Path, version: int, clickhouse: ClickHouse, resource_index_path: Path | None = None) -> tuple[int, set[str]]:
    if version < 1:
        raise ValueError("version must be positive")
    parsed = _read_snapshot(snapshot)
    release_id = _release_id()
    identities = _resolve_identities(clickhouse, parsed)
    tables, resource_rows, study_internal_ids = _normalize(parsed, identities, version, release_id)
    previous_index = _read_resource_index(resource_index_path)
    index_payload = _merge_resource_index(resource_rows, release_id, previous_index) if resource_index_path else None
    for statement in TABLE_DDL.split(";"):
        if statement.strip():
            clickhouse.execute(statement)
    for table in INSERT_TABLES:
        _insert_rows(clickhouse, table, tables[table])
    old_index_bytes = resource_index_path.read_bytes() if resource_index_path and resource_index_path.exists() else None
    if resource_index_path and index_payload:
        _publish_resource_index(resource_index_path, index_payload)
    try:
        _insert_rows(clickhouse, "wsi_release", [
            {"cancer_study_id": study_id, "release_id": release_id, "release_version": version, "released_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")}
            for study_id in sorted(study_internal_ids)
        ])
    except Exception:
        if resource_index_path:
            if old_index_bytes is None:
                resource_index_path.unlink(missing_ok=True)
            else:
                resource_index_path.write_bytes(old_index_bytes)
        raise
    return len(parsed), set(study for study, _, _ in parsed)


def main() -> None:
    args = _args()
    count, studies = load(args.input, args.version, ClickHouse(args.url, args.database, args.user, args.password), args.resource_index)
    print(f"Published normalized ClickHouse WSI snapshot {args.version}: {count} patients across {len(studies)} studies")


if __name__ == "__main__":
    main()
