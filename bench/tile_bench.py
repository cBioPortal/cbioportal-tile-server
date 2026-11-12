#!/usr/bin/env python3
"""Repeatable HTTP benchmark for the WSI tile-loading workload.

Each ``--access-json`` file describes one slide's resource row, as for
``single_user_pan_bench.py``: ``studyId``, ``key``, ``enc`` (the row's sealed
source) and ``tileMetadata``. Behind the ingress, set ``WSI_BENCH_COOKIE`` to
the browser's session cookie (for example ``SESSION=...``) so the ingress
authorizes the study. Sealed sources and the cookie are held only in memory
and never included in the output.

Example:
  WSI_BENCH_COOKIE='SESSION=...' python bench/tile_bench.py \
    --base-url https://cbioportal.example.org/wsi \
    --access-json /secure/path/slide-a.json --access-json /secure/path/slide-b.json \
    --requests 1000 --concurrency 20 --warmup 100 \
    --cache-mode warm --json-out results/nginx-warm.json

The generated request sequence is deterministic for a given seed. Warmup
requests are excluded from reported measurements. The server cache is not
mutated by this script; run against a restarted/empty-cache server for a cold
measurement, then repeat against the same server for a warm measurement.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
import random
import statistics
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


@dataclass(frozen=True)
class Result:
    status: int
    latency_ms: float
    error: str | None = None


@dataclass(frozen=True)
class Slide:
    params: dict[str, str]
    max_zoom: int
    tile_size: int
    width: int
    height: int


@dataclass(frozen=True)
class Request:
    path: str
    params: dict[str, str]


def percentile(values: list[float], fraction: float) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    index = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument("--access-json", action="append", required=True, type=Path)
    parser.add_argument("--requests", type=int, default=1000)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--warmup", type=int, default=100)
    parser.add_argument("--seed", type=int, default=20260721)
    parser.add_argument("--cache-mode", choices=("cold", "warm"), required=True)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--json-out")
    return parser.parse_args()


def read_slide(path: Path) -> Slide:
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    params = {name: payload.get(name) for name in ("studyId", "key", "enc")}
    for name, value in params.items():
        if not isinstance(value, str) or not value:
            raise SystemExit(f"{path}: access JSON is missing {name}")
    metadata = payload.get("tileMetadata")
    try:
        return Slide(
            params=params,
            max_zoom=int(metadata["max_zoom"]),
            tile_size=int(metadata["tile_size"]),
            width=int(metadata["dimensions"]["width"]),
            height=int(metadata["dimensions"]["height"]),
        )
    except (KeyError, TypeError, ValueError):
        raise SystemExit(f"{path}: access JSON is missing tileMetadata") from None


def build_workload(count: int, rng: random.Random, slides: list[Slide]) -> list[Request]:
    """About 95% tiles biased toward high zoom, 5% 256x256 thumbnails."""
    requests: list[Request] = []
    for _ in range(count):
        roll = rng.random()
        slide = slides[rng.randrange(len(slides))]
        if roll < 0.05:
            requests.append(
                Request("/thumbnails", {**slide.params, "width": "256", "height": "256"})
            )
            continue
        z_offset = rng.choices((0, 1, 2, 3, 4, 5), weights=(1, 1, 2, 3, 5, 8))[0]
        z = max(0, slide.max_zoom - z_offset)
        downsample = 2 ** (slide.max_zoom - z)
        grid_x = max(1, math.ceil(slide.width / (slide.tile_size * downsample)))
        grid_y = max(1, math.ceil(slide.height / (slide.tile_size * downsample)))
        x = rng.randrange(grid_x)
        y = rng.randrange(grid_y)
        requests.append(Request(f"/tiles/zxy/{z}/{x}/{y}", slide.params))
    return requests


_thread_state = threading.local()


def send(base_url: str, item: Request, headers: dict[str, str], timeout: float) -> Result:
    started = time.perf_counter()
    try:
        client = getattr(_thread_state, "client", None)
        if client is None or client.timeout.read != timeout:
            client = httpx.Client(timeout=timeout, headers=headers)
            _thread_state.client = client
        response = client.get(f"{base_url}{item.path}", params=item.params)
        return Result(response.status_code, (time.perf_counter() - started) * 1000)
    except (httpx.HTTPError, TimeoutError, OSError) as exc:
        # The exception text can carry the URL (and so `enc`); keep only its type.
        return Result(0, (time.perf_counter() - started) * 1000, type(exc).__name__)


def run(base_url: str, requests: list[Request], args: argparse.Namespace) -> list[Result]:
    headers = {}
    cookie = os.environ.get("WSI_BENCH_COOKIE", "")
    if cookie:
        headers["Cookie"] = cookie
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [
            pool.submit(send, base_url, item, headers, args.timeout) for item in requests
        ]
        return [future.result() for future in futures]


def main() -> None:
    args = parse_args()
    if args.requests < 1 or args.concurrency < 1 or args.warmup < 0:
        raise SystemExit("requests and concurrency must be positive; warmup cannot be negative")

    slides = [read_slide(path) for path in args.access_json]
    base_url = args.base_url.rstrip("/")
    rng = random.Random(args.seed)
    warmup = build_workload(args.warmup, rng, slides)
    measured = build_workload(args.requests, rng, slides)
    if warmup:
        run(base_url, warmup, args)

    started = time.perf_counter()
    results = run(base_url, measured, args)
    elapsed_s = time.perf_counter() - started
    successful = [result.latency_ms for result in results if 200 <= result.status < 300]
    failures = [result for result in results if not 200 <= result.status < 300]
    latency_by_status = {}
    for status in sorted({result.status for result in results}):
        values = [result.latency_ms for result in results if result.status == status]
        latency_by_status[str(status)] = {
            "count": len(values),
            "p50": round(percentile(values, 0.50) or 0, 3),
            "p95": round(percentile(values, 0.95) or 0, 3),
        }
    summary = {
        "base_url": base_url,
        "study_ids": sorted({slide.params["studyId"] for slide in slides}),
        "slides": len(slides),
        "cache_mode": args.cache_mode,
        "seed": args.seed,
        "warmup_requests": args.warmup,
        "measured_requests": len(results),
        "concurrency": args.concurrency,
        "elapsed_s": round(elapsed_s, 3),
        "throughput_rps": round(len(results) / elapsed_s, 3) if elapsed_s else 0,
        "successful_throughput_rps": round(len(successful) / elapsed_s, 3) if elapsed_s else 0,
        "failure_rate": round(len(failures) / len(results), 4) if results else 0,
        "successes": len(successful),
        "failures": len(failures),
        "latency_ms": {
            "p50": round(value, 3) if (value := percentile(successful, 0.50)) is not None else None,
            "p95": round(value, 3) if (value := percentile(successful, 0.95)) is not None else None,
            "p99": round(value, 3) if (value := percentile(successful, 0.99)) is not None else None,
            "max": round(max(successful), 3) if successful else None,
            "mean": round(statistics.mean(successful), 3) if successful else None,
        },
        "status_counts": {
            str(status): sum(result.status == status for result in results)
            for status in sorted({result.status for result in results})
        },
        "latency_ms_by_status": latency_by_status,
    }
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as output:
            json.dump(summary, output, indent=2)
            output.write("\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
