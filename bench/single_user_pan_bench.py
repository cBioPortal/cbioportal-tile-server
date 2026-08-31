#!/usr/bin/env python3
"""Benchmark one authenticated WSI viewer session.

The access JSON is the response from cBioPortal's slide access endpoint. It
must contain ``accessToken``, ``sourceUrl``, and ``tileMetadata``. The token is
read only in memory and is never included in the benchmark output.

Example::

    python bench/single_user_pan_bench.py \
        --base-url https://beta.cbioportal.mskcc.org/wsi \
        --access-json /secure/path/access.json \
        --center-x 33864 --center-y 23365
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import math
import statistics
import time
from pathlib import Path
from typing import Any

import httpx


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--access-json", required=True, type=Path)
    parser.add_argument("--center-x", type=int, required=True)
    parser.add_argument("--center-y", type=int, required=True)
    parser.add_argument("--viewport-width", type=int, default=1400)
    parser.add_argument("--viewport-height", type=int, default=900)
    parser.add_argument("--viewport-cols", type=int, default=8)
    parser.add_argument("--viewport-rows", type=int, default=5)
    parser.add_argument("--pan-steps", type=int, default=6)
    parser.add_argument("--pan-columns", type=int, default=2)
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--timeout", type=float, default=45.0)
    return parser.parse_args()


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))
    return ordered[index]


def read_access(path: Path) -> tuple[str, str, dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    token = payload.get("accessToken")
    source = payload.get("sourceUrl")
    metadata = payload.get("tileMetadata")
    if not isinstance(token, str) or not token:
        raise ValueError("access JSON is missing accessToken")
    if not isinstance(source, str) or not source:
        raise ValueError("access JSON is missing sourceUrl")
    if not isinstance(metadata, dict):
        raise ValueError("access JSON is missing tileMetadata")
    return token, source, metadata


def viewport_tiles(
    metadata: dict[str, Any],
    z: int,
    center_x: int,
    center_y: int,
    columns: int,
    rows: int,
) -> list[tuple[int, int, int]]:
    width = int(metadata["dimensions"]["width"])
    height = int(metadata["dimensions"]["height"])
    tile_size = int(metadata["tile_size"])
    max_zoom = int(metadata["max_zoom"])
    downsample = 2 ** (max_zoom - z)
    grid_x = max(1, math.ceil(width / (tile_size * downsample)))
    grid_y = max(1, math.ceil(height / (tile_size * downsample)))
    tile_x = max(0, min(grid_x - 1, center_x // (tile_size * downsample)))
    tile_y = max(0, min(grid_y - 1, center_y // (tile_size * downsample)))
    first_x = max(0, min(max(0, grid_x - columns), tile_x - columns // 2))
    first_y = max(0, min(max(0, grid_y - rows), tile_y - rows // 2))
    return [
        (z, x, y)
        for y in range(first_y, min(grid_y, first_y + rows))
        for x in range(first_x, min(grid_x, first_x + columns))
    ]


def build_workload(args: argparse.Namespace, metadata: dict[str, Any]):
    width = int(metadata["dimensions"]["width"])
    height = int(metadata["dimensions"]["height"])
    max_zoom = int(metadata["max_zoom"])
    fit_zoom = max(
        0,
        min(
            max_zoom,
            max_zoom
            - math.floor(
                math.log2(max(width / args.viewport_width, height / args.viewport_height))
            ),
        ),
    )
    fit_tiles = viewport_tiles(
        metadata,
        fit_zoom,
        args.center_x,
        args.center_y,
        args.viewport_cols,
        args.viewport_rows,
    )
    maximum_tiles = viewport_tiles(
        metadata,
        max_zoom,
        args.center_x,
        args.center_y,
        args.viewport_cols,
        args.viewport_rows,
    )
    rows = sorted(y for _, _, y in maximum_tiles)
    last_x = max(x for _, x, _ in maximum_tiles)
    grid_x = math.ceil(width / int(metadata["tile_size"]))
    pan_bands = []
    for step in range(1, args.pan_steps + 1):
        columns = [
            x
            for x in (
                last_x + args.pan_columns * step - 1,
                last_x + args.pan_columns * step,
            )
            if x < grid_x
        ]
        pan_bands.append([(max_zoom, x, y) for y in rows for x in columns])
    return fit_zoom, fit_tiles, maximum_tiles, pan_bands


async def run_phase(
    client: httpx.AsyncClient,
    base_url: str,
    tiles: list[tuple[int, int, int]],
    concurrency: int,
) -> dict[str, Any]:
    semaphore = asyncio.Semaphore(concurrency)

    async def request(tile: tuple[int, int, int]) -> dict[str, Any]:
        z, x, y = tile
        async with semaphore:
            started = time.perf_counter()
            try:
                response = await client.get(f"{base_url}/tiles/zxy/{z}/{x}/{y}")
                return {
                    "status": response.status_code,
                    "latency_ms": (time.perf_counter() - started) * 1000,
                    "bytes": len(response.content),
                    "http_version": response.http_version,
                }
            except Exception as exc:  # noqa: BLE001 - benchmark records failures
                return {
                    "status": 0,
                    "latency_ms": (time.perf_counter() - started) * 1000,
                    "bytes": 0,
                    "http_version": type(exc).__name__,
                }

    started = time.perf_counter()
    results = await asyncio.gather(*(request(tile) for tile in tiles))
    elapsed = time.perf_counter() - started
    successful = [
        result["latency_ms"] for result in results if result["status"] == 200
    ]
    return {
        "requests": len(results),
        "statuses": {
            str(status): sum(result["status"] == status for result in results)
            for status in sorted({result["status"] for result in results})
        },
        "elapsed_ms": round(elapsed * 1000, 1),
        "rps": round(len(results) / elapsed, 1) if elapsed else 0,
        "latency_ms": {
            "p50": round(statistics.median(successful), 1) if successful else None,
            "p95": round(percentile(successful, 0.95), 1) if successful else None,
            "max": round(max(successful), 1) if successful else None,
        },
        "http_versions": sorted({result["http_version"] for result in results}),
    }


async def main() -> None:
    args = parse_args()
    if min(
        args.viewport_width,
        args.viewport_height,
        args.viewport_cols,
        args.viewport_rows,
        args.concurrency,
    ) < 1:
        raise SystemExit("viewport dimensions, tile counts, and concurrency must be positive")
    token, source, metadata = read_access(args.access_json)
    fit_zoom, fit_tiles, maximum_tiles, pan_bands = build_workload(args, metadata)
    base_url = args.base_url.rstrip("/")
    headers = {
        "Authorization": f"Bearer {token}",
        "X-WSI-Source": source,
        "Origin": "https://beta.cbioportal.mskcc.org",
    }
    print(
        json.dumps(
            {
                "dimensions": metadata["dimensions"],
                "max_zoom": metadata["max_zoom"],
                "fit_zoom": fit_zoom,
                "fit_tiles": len(fit_tiles),
                "max_viewport_tiles": len(maximum_tiles),
                "pan_band_sizes": [len(band) for band in pan_bands],
                "concurrency": args.concurrency,
            }
        )
    )
    async with httpx.AsyncClient(
        # HTTP/2 is useful when the optional h2 package is present, but the
        # benchmark must also run in the minimal dev dependency environment.
        http2=importlib.util.find_spec("h2") is not None,
        headers=headers,
        timeout=args.timeout,
        limits=httpx.Limits(
            max_connections=args.concurrency,
            max_keepalive_connections=args.concurrency,
        ),
    ) as client:
        print(json.dumps({"phase": "cold_fit_view", **await run_phase(client, base_url, fit_tiles, args.concurrency)}))
        print(json.dumps({"phase": "cold_max_zoom_viewport", **await run_phase(client, base_url, maximum_tiles, args.concurrency)}))
        for index, band in enumerate(pan_bands, start=1):
            print(json.dumps({"phase": f"cold_pan_{index}", **await run_phase(client, base_url, band, args.concurrency)}))
            await asyncio.sleep(0.08)
        replay = maximum_tiles + [tile for band in pan_bands for tile in band]
        print(json.dumps({"phase": "warm_replay", **await run_phase(client, base_url, replay, args.concurrency)}))


if __name__ == "__main__":
    asyncio.run(main())
