#!/usr/bin/env python3
"""Benchmark QuiltNet worker latency over a published slide manifest.

The first pass measures artifact/model warmup as observed by the worker; the
second pass measures a warm artifact cache.  The worker remains the source of
truth for scoring and its aggregate stage timings are printed at the end.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx


def _read_manifest(source: str) -> dict[str, Any]:
    parsed = urlparse(source)
    if parsed.scheme in {"", "file"}:
        return json.loads(Path(parsed.path if parsed.scheme else source).read_text())
    if parsed.scheme != "s3":
        raise ValueError("manifest must be a local path or s3 URI")
    import boto3

    response = boto3.client(
        "s3",
        endpoint_url=os.environ.get("AWS_ENDPOINT_URL") or None,
    ).get_object(Bucket=parsed.netloc, Key=parsed.path.lstrip("/"))
    return json.loads(response["Body"].read())


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, math.ceil(len(ordered) * percentile) - 1)
    return ordered[index]


async def _run(args: argparse.Namespace) -> None:
    manifest = _read_manifest(args.manifest)
    records = []
    for row in manifest.get("slides", []):
        if not isinstance(row, dict):
            continue
        model = next(
            (
                value
                for value in row.get("models", [])
                if isinstance(value, dict)
                and value.get("id") == "quiltnet_pmb"
                and (
                    value.get("features_uri")
                    or (
                        isinstance(value.get("serving_artifact"), dict)
                        and value["serving_artifact"].get("features_uri")
                    )
                )
            ),
            None,
        )
        if model:
            records.append((str(row.get("slide_id")), model))
    records = records[: args.limit]
    payload = {
        "model": "quiltnet_pmb",
        "query_plan": {"primary": args.query, "positive": [args.query], "negative": []},
        "top_k": args.top_k,
        "slide_width": args.slide_width,
        "slide_height": args.slide_height,
    }
    passes = []
    async with httpx.AsyncClient(base_url=args.worker_url.rstrip("/"), timeout=args.timeout) as client:
        ready = await client.get("/ready")
        ready.raise_for_status()
        for pass_number in range(1, args.runs + 1):
            timings = []
            failures = []
            for slide_id, model in records:
                started = time.perf_counter()
                response = await client.post(
                    "/v1/search", json={**payload, "model_record": model}
                )
                elapsed = (time.perf_counter() - started) * 1000
                if response.is_success:
                    timings.append(elapsed)
                else:
                    failures.append({"slide_id": slide_id, "status": response.status_code})
            passes.append(
                {
                    "pass": pass_number,
                    "successful": len(timings),
                    "failures": failures,
                    "p50_ms": round(statistics.median(timings), 1) if timings else None,
                    "p95_ms": round(_percentile(timings, 0.95), 1) if timings else None,
                    "max_ms": round(max(timings), 1) if timings else None,
                }
            )
        health = (await client.get("/health")).json()
    print(json.dumps({"slides": len(records), "passes": passes, "worker": health}, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, help="manifest path or s3 URI")
    parser.add_argument("--worker-url", default="http://localhost:8080")
    parser.add_argument("--query", default="tumor")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--limit", type=int, default=24)
    parser.add_argument("--runs", type=int, default=2)
    parser.add_argument("--slide-width", type=int, default=100000)
    parser.add_argument("--slide-height", type=int, default=100000)
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
