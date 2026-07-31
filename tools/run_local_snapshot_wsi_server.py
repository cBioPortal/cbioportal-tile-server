#!/usr/bin/env python3
"""Run the local WSI tile server with a snapshot-backed patient hierarchy route."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import uvicorn

from app.main import app


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
    app.state.wsi_snapshot_hierarchies = hierarchy_by_study

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
