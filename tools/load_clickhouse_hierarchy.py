#!/usr/bin/env python3
"""Load a validated, study-scoped WSI hierarchy snapshot into ClickHouse.

The upstream Databricks job produces one JSON object per line with the shape:
{"study_id": "...", "patient_id": "...", "hierarchy": {...}}

Every load gets a unique publication ID. The complete input is parsed and
validated before any database write. Rows are inserted under that ID and the
single manifest insert is the publication point. A failed row insert therefore
leaves the previous active manifest untouched; orphaned rows are invisible.
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
CREATE TABLE IF NOT EXISTS wsi_patient_hierarchy
(
    study_id String,
    patient_id String,
    snapshot_version UInt64,
    publication_id String DEFAULT '',
    hierarchy_json String,
    updated_at DateTime64(6)
) ENGINE = MergeTree()
ORDER BY (study_id, patient_id, snapshot_version, publication_id)
"""

MANIFEST_DDL = """
CREATE TABLE IF NOT EXISTS wsi_patient_hierarchy_manifest
(
    study_id String,
    active_version UInt64,
    publication_id String DEFAULT '',
    updated_at DateTime64(6)
) ENGINE = MergeTree()
ORDER BY study_id
"""

# These ALTERs make an existing pre-publication-ID installation able to accept
# new rows. Changing an existing MergeTree's ORDER BY/engine is a deployment
# migration and is documented in docs/runbook.md; fresh installs use the DDL
# above.
COMPATIBILITY_DDL = (
    "ALTER TABLE wsi_patient_hierarchy ADD COLUMN IF NOT EXISTS publication_id String DEFAULT ''",
    "ALTER TABLE wsi_patient_hierarchy_manifest ADD COLUMN IF NOT EXISTS publication_id String DEFAULT ''",
)


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path, help="Upstream hierarchy JSONL snapshot")
    parser.add_argument("--version", type=int, required=True)
    parser.add_argument("--url", default=os.getenv("CLICKHOUSE_URL", "http://localhost:8123"))
    parser.add_argument("--database", default=os.getenv("CLICKHOUSE_DATABASE", "cbioportal"))
    parser.add_argument("--user", default=os.getenv("CLICKHOUSE_USER", "default"))
    parser.add_argument("--password", default=os.getenv("CLICKHOUSE_PASSWORD", ""))
    parser.add_argument(
        "--resource-index",
        type=Path,
        default=(
            Path(os.environ["WSI_RESOURCE_INDEX_FILE"])
            if os.getenv("WSI_RESOURCE_INDEX_FILE")
            else None
        ),
        help="Atomically publish the trusted study-to-patient/sample/slide index here",
    )
    return parser.parse_args()


class ClickHouse:
    def __init__(self, url: str, database: str, user: str, password: str):
        self.url = url.rstrip("/")
        self.database = database
        self.auth = base64.b64encode(f"{user}:{password}".encode()).decode()

    def execute(self, query: str, body: bytes | None = None) -> None:
        params = urlencode({"database": self.database, "query": query})
        request = Request(f"{self.url}/?{params}", data=body, method="POST")
        request.add_header("Authorization", f"Basic {self.auth}")
        request.add_header("Content-Type", "application/json" if body else "text/plain")
        with urlopen(request, timeout=120) as response:
            response.read()


def _publication_id() -> str:
    # The nanosecond prefix gives publication IDs a total time order for the
    # manifest argMax query; the UUID keeps retries unique even on coarse clocks.
    return f"{time.time_ns():020d}-{uuid.uuid4().hex}"


def _row(
    line: str,
    version: int,
    updated_at: str,
    publication_id: str = "",
) -> tuple[str, str]:
    value = json.loads(line)
    if not isinstance(value, dict):
        raise ValueError("Each snapshot line must be a JSON object")
    study_id = value.get("study_id")
    patient_id = value.get("patient_id")
    hierarchy = value.get("hierarchy")
    if (
        not isinstance(study_id, str)
        or not study_id.strip()
        or not isinstance(patient_id, str)
        or not patient_id.strip()
        or not isinstance(hierarchy, dict)
    ):
        raise ValueError("Each snapshot row needs study_id, patient_id, and hierarchy")
    if hierarchy.get("patient_id") != patient_id:
        raise ValueError(f"Hierarchy patient_id does not match row for {patient_id}")
    row = {
        "study_id": study_id,
        "patient_id": patient_id,
        "snapshot_version": version,
        "publication_id": publication_id,
        "hierarchy_json": json.dumps(hierarchy, separators=(",", ":"), ensure_ascii=False),
        "updated_at": updated_at,
    }
    return study_id, json.dumps(row, ensure_ascii=False)


def _resource_ids(hierarchy: dict, patient_id: str) -> dict[str, set[str]]:
    ids = {"patients": {patient_id}, "samples": set(), "slides": set()}

    def visit(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "sample_id" and child is not None:
                    ids["samples"].add(str(child))
                elif key in {"image_id", "slide_id"} and child is not None:
                    ids["slides"].add(str(child))
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(hierarchy)
    return ids


def _merge_resource_index(
    parsed_rows: list[tuple[str, str, dict]], publication_id: str, existing: dict | None
) -> dict:
    studies: dict[str, dict[str, set[str]]] = {}
    if isinstance(existing, dict):
        for study_id, resources in (existing.get("studies") or {}).items():
            if not isinstance(resources, dict):
                continue
            studies[str(study_id)] = {
                resource_type: {str(value) for value in (resources.get(resource_type) or [])}
                for resource_type in ("patients", "samples", "slides")
            }

    replaced_studies: set[str] = set()
    for study_id, patient_id, hierarchy in parsed_rows:
        if study_id not in replaced_studies:
            resources = {"patients": set(), "samples": set(), "slides": set()}
            studies[study_id] = resources
            replaced_studies.add(study_id)
        else:
            resources = studies[study_id]
        row_resources = _resource_ids(hierarchy, patient_id)
        for resource_type, values in row_resources.items():
            resources[resource_type].update(values)

    owners: dict[tuple[str, str], str] = {}
    for study_id, resources in studies.items():
        for resource_type in ("patients", "samples", "slides"):
            for resource_id in resources[resource_type]:
                owner_key = (resource_type, resource_id)
                owner = owners.get(owner_key)
                if owner is not None and owner != study_id:
                    raise ValueError(
                        "resource identifier is ambiguous across studies: "
                        f"{resource_type}/{resource_id} ({owner}, {study_id})"
                    )
                owners[owner_key] = study_id

    return {
        "version": 1,
        "publication_id": publication_id,
        "generated_at": datetime.now(UTC).isoformat(),
        "studies": {
            study_id: {
                resource_type: sorted(values)
                for resource_type, values in resources.items()
            }
            for study_id, resources in sorted(studies.items())
        },
    }


def _read_resource_index(path: Path | None) -> dict | None:
    if path is None or not path.exists():
        return None
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("resource index must be a version 1 JSON object")
    return value


def _publish_resource_index(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return path


def load(
    snapshot: Path,
    version: int,
    clickhouse: ClickHouse,
    resource_index_path: Path | None = None,
) -> tuple[int, set[str]]:
    if version < 1:
        raise ValueError("version must be positive")

    publication_id = _publication_id()
    updated_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")
    count = 0
    studies: set[str] = set()
    parsed_rows: list[tuple[str, str, dict]] = []
    seen_patients: set[tuple[str, str]] = set()
    with snapshot.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("snapshot line must be a JSON object")
                study_id = value.get("study_id")
                patient_id = value.get("patient_id")
                hierarchy = value.get("hierarchy")
                if (
                    not isinstance(study_id, str)
                    or not study_id.strip()
                    or not isinstance(patient_id, str)
                    or not patient_id.strip()
                    or not isinstance(hierarchy, dict)
                ):
                    raise ValueError(
                        "snapshot row needs non-empty string study_id, patient_id, and hierarchy"
                    )
                if (study_id, patient_id) in seen_patients:
                    raise ValueError(
                        f"duplicate study_id/patient_id in snapshot: {study_id}/{patient_id}"
                    )
                seen_patients.add((study_id, patient_id))
                study_id, row = _row(line, version, updated_at, publication_id)
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise ValueError(f"Invalid snapshot line {line_number}: {error}") from error
            parsed_rows.append((study_id, patient_id, hierarchy))
            studies.add(study_id)
            count += 1
    if not parsed_rows:
        raise ValueError("snapshot must contain at least one hierarchy row")

    previous_index = _read_resource_index(resource_index_path)
    index_payload = (
        _merge_resource_index(parsed_rows, publication_id, previous_index)
        if resource_index_path is not None
        else None
    )

    # No database or index mutation occurs before the full input and duplicate
    # key check above have succeeded.
    clickhouse.execute(TABLE_DDL)
    clickhouse.execute(MANIFEST_DDL)
    for statement in COMPATIBILITY_DDL:
        clickhouse.execute(statement)

    rows = [
        _row(
            json.dumps(
                {"study_id": study_id, "patient_id": patient_id, "hierarchy": hierarchy}
            ),
            version,
            updated_at,
            publication_id,
        )[1]
        for study_id, patient_id, hierarchy in parsed_rows
    ]
    for offset in range(0, len(rows), 10_000):
        clickhouse.execute(
            "INSERT INTO wsi_patient_hierarchy FORMAT JSONEachRow",
            ("\n".join(rows[offset : offset + 10_000]) + "\n").encode(),
        )

    old_index_bytes = None
    if resource_index_path is not None:
        if resource_index_path.exists():
            old_index_bytes = resource_index_path.read_bytes()
        if index_payload is not None:
            _publish_resource_index(resource_index_path, index_payload)

    try:
        # One JSONEachRow statement is the publication boundary. ClickHouse
        # commits this insert as one query, so a failed publication does not
        # replace only a subset of study manifests.
        clickhouse.execute(
            "INSERT INTO wsi_patient_hierarchy_manifest FORMAT JSONEachRow",
            (
                "\n".join(
                    json.dumps(
                        {
                            "study_id": study_id,
                            "active_version": version,
                            "publication_id": publication_id,
                            "updated_at": updated_at,
                        }
                    )
                    for study_id in sorted(studies)
                )
                + "\n"
            ).encode(),
        )
    except Exception:
        # Keep the trusted index aligned with the previous manifest if the
        # publication query fails before ClickHouse accepts it.
        if resource_index_path is not None:
            if old_index_bytes is None:
                resource_index_path.unlink(missing_ok=True)
            else:
                resource_index_path.write_bytes(old_index_bytes)
        raise
    return count, studies


def main() -> None:
    args = _args()
    count, studies = load(
        args.input,
        args.version,
        ClickHouse(args.url, args.database, args.user, args.password),
        args.resource_index,
    )
    print(f"Published ClickHouse WSI snapshot {args.version}: {count} patients across {len(studies)} studies")


if __name__ == "__main__":
    main()
