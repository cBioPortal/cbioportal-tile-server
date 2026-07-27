#!/usr/bin/env python3
"""Load a validated WSI hierarchy JSONL snapshot into ClickHouse.

The upstream Databricks job produces one JSON object per line with the shape:
{"study_id": "...", "patient_id": "...", "hierarchy": {...}}
Rows are inserted under an inactive version first. The manifest is published
only after the complete input has been accepted by ClickHouse.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
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
    hierarchy_json String,
    updated_at DateTime
) ENGINE = MergeTree()
ORDER BY (study_id, patient_id, snapshot_version)
"""

MANIFEST_DDL = """
CREATE TABLE IF NOT EXISTS wsi_patient_hierarchy_manifest
(
    study_id String,
    active_version UInt64,
    updated_at DateTime
) ENGINE = ReplacingMergeTree(updated_at)
ORDER BY study_id
"""


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path, help="Upstream hierarchy JSONL snapshot")
    parser.add_argument("--version", type=int, required=True)
    parser.add_argument("--url", default=os.getenv("CLICKHOUSE_URL", "http://localhost:8123"))
    parser.add_argument("--database", default=os.getenv("CLICKHOUSE_DATABASE", "cbioportal"))
    parser.add_argument("--user", default=os.getenv("CLICKHOUSE_USER", "default"))
    parser.add_argument("--password", default=os.getenv("CLICKHOUSE_PASSWORD", ""))
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


def _row(line: str, version: int, updated_at: str) -> tuple[str, str]:
    value = json.loads(line)
    study_id = str(value.get("study_id") or "")
    patient_id = str(value.get("patient_id") or "")
    hierarchy = value.get("hierarchy")
    if not study_id or not patient_id or not isinstance(hierarchy, dict):
        raise ValueError("Each snapshot row needs study_id, patient_id, and hierarchy")
    if hierarchy.get("patient_id") != patient_id:
        raise ValueError(f"Hierarchy patient_id does not match row for {patient_id}")
    row = {
        "study_id": study_id,
        "patient_id": patient_id,
        "snapshot_version": version,
        "hierarchy_json": json.dumps(hierarchy, separators=(",", ":"), ensure_ascii=False),
        "updated_at": updated_at,
    }
    return study_id, json.dumps(row, ensure_ascii=False)


def load(snapshot: Path, version: int, clickhouse: ClickHouse) -> tuple[int, set[str]]:
    if version < 1:
        raise ValueError("version must be positive")
    clickhouse.execute(TABLE_DDL)
    clickhouse.execute(MANIFEST_DDL)
    updated_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    count = 0
    studies: set[str] = set()
    batch: list[str] = []
    with snapshot.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                study_id, row = _row(line, version, updated_at)
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise ValueError(f"Invalid snapshot line {line_number}: {error}") from error
            studies.add(study_id)
            batch.append(row)
            count += 1
            if len(batch) == 10_000:
                clickhouse.execute(
                    "INSERT INTO wsi_patient_hierarchy FORMAT JSONEachRow",
                    ("\n".join(batch) + "\n").encode(),
                )
                batch.clear()
    if batch:
        clickhouse.execute(
            "INSERT INTO wsi_patient_hierarchy FORMAT JSONEachRow",
            ("\n".join(batch) + "\n").encode(),
        )
    for study_id in studies:
        clickhouse.execute(
            "INSERT INTO wsi_patient_hierarchy_manifest FORMAT JSONEachRow",
            json.dumps(
                {"study_id": study_id, "active_version": version, "updated_at": updated_at}
            ).encode()
            + b"\n",
        )
    return count, studies


def main() -> None:
    args = _args()
    count, studies = load(
        args.input,
        args.version,
        ClickHouse(args.url, args.database, args.user, args.password),
    )
    print(f"Published ClickHouse WSI snapshot {args.version}: {count} patients across {len(studies)} studies")


if __name__ == "__main__":
    main()
