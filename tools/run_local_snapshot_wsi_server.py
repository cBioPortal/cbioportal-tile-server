#!/usr/bin/env python3
"""Run the local WSI tile server with a snapshot-backed patient hierarchy route."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import uvicorn
from fastapi import HTTPException, Query
from fastapi.responses import Response

from app.main import PHI_CACHE_HEADERS, app
from app.meta import get_patient_hierarchy
from app.config import settings


def _load_snapshot(path: Path) -> dict[str, dict[str, dict]]:
    by_study: dict[str, dict[str, dict]] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            by_study.setdefault(row["study_id"], {})[row["patient_id"]] = row["hierarchy"]
    return by_study


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", help="Optional path to a hierarchy JSONL snapshot.")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host.")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("SERVER_PORT", "8081")),
        help="Bind port.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    hierarchy_by_study: dict[str, dict[str, dict]] = {}
    if args.snapshot:
        snapshot = Path(args.snapshot).expanduser().resolve()
        hierarchy_by_study = _load_snapshot(snapshot)

    @app.get("/patient/{patient_id}", include_in_schema=False)
    async def patient_hierarchy(patient_id: str, studyId: str = Query(...)) -> Response:  # noqa: N803
        hierarchy = hierarchy_by_study.get(studyId, {}).get(patient_id)
        if hierarchy is None:
            hierarchy = get_patient_hierarchy(patient_id, settings.databricks_warehouse_id)
        if hierarchy is None:
            raise HTTPException(status_code=404, detail="Patient hierarchy not found")
        return Response(
            content=json.dumps(hierarchy, separators=(",", ":")),
            media_type="application/json",
            headers=PHI_CACHE_HEADERS,
        )

    @app.get("/patient/{patient_id}/bootstrap", include_in_schema=False)
    async def patient_hierarchy_bootstrap(patient_id: str, studyId: str = Query(...)) -> Response:  # noqa: N803
        hierarchy = hierarchy_by_study.get(studyId, {}).get(patient_id)
        if hierarchy is None:
            hierarchy = get_patient_hierarchy(patient_id, settings.databricks_warehouse_id)
        if hierarchy is None:
            raise HTTPException(status_code=404, detail="Patient hierarchy not found")
        return Response(
            content=json.dumps({"hierarchy": hierarchy, "initial": None}, separators=(",", ":")),
            media_type="application/json",
            headers=PHI_CACHE_HEADERS,
        )

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
