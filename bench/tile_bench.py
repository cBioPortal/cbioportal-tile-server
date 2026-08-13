#!/usr/bin/env python3
"""Repeatable HTTP benchmark for the WSI tile-loading workload.

Example:
  python bench/tile_bench.py \
    --base-url http://localhost:3001/wsi \
    --study-id msk_spectrum_tme_2022 \
    --slide-id 2908638 --slide-id 4186363 \
    --bearer-token "$WSI_BENCH_TOKEN" \
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
import random
import statistics
import threading
import time
from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class Result:
    status: int
    latency_ms: float
    error: str | None = None


@dataclass(frozen=True)
class SlideSpec:
    max_zoom: int
    tile_size: int
    width: int
    height: int


def percentile(values: list[float], fraction: float) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    index = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument("--study-id", required=True)
    parser.add_argument("--slide-id", action="append", required=True)
    parser.add_argument("--bearer-token", default="")
    parser.add_argument("--requests", type=int, default=1000)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--warmup", type=int, default=100)
    parser.add_argument("--seed", type=int, default=20260721)
    parser.add_argument("--max-zoom", type=int, default=None)
    parser.add_argument("--tile-grid", type=int, default=None,
                        help="Fallback grid when metadata discovery is unavailable")
    parser.add_argument("--cache-mode", choices=("cold", "warm"), required=True)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--json-out")
    return parser.parse_args()


def discover_slide_specs(args: argparse.Namespace) -> dict[str, SlideSpec]:
    specs: dict[str, SlideSpec] = {}
    fallback_zoom = args.max_zoom if args.max_zoom is not None else 15
    fallback_grid = args.tile_grid if args.tile_grid is not None else 8
    with httpx.Client(timeout=args.timeout) as client:
        for slide_id in args.slide_id:
            spec = SlideSpec(
                max_zoom=fallback_zoom,
                tile_size=256,
                width=fallback_grid * 256,
                height=fallback_grid * 256,
            )
            if args.max_zoom is None or args.tile_grid is None:
                url = f"{args.base_url.rstrip('/')}/tiles/{slide_id}/metadata?studyId={args.study_id}"
                try:
                    response = client.get(url, headers={"Authorization": f"Bearer {args.bearer_token}"} if args.bearer_token else {})
                    if 200 <= response.status_code < 300:
                        data = response.json()
                        dimensions = data["dimensions"]
                        spec = SlideSpec(
                            max_zoom=int(data["max_zoom"]),
                            tile_size=int(data["tile_size"]),
                            width=int(dimensions["width"]),
                            height=int(dimensions["height"]),
                        )
                except (httpx.HTTPError, ValueError, KeyError, TypeError):
                    pass
            specs[slide_id] = spec
    return specs


def build_workload(args: argparse.Namespace, count: int, rng: random.Random, specs: dict[str, SlideSpec]) -> list[str]:
    base = args.base_url.rstrip("/")
    urls: list[str] = []
    for _ in range(count):
        roll = rng.random()
        slide_id = args.slide_id[rng.randrange(len(args.slide_id))]
        spec = specs[slide_id]
        scope = f"studyId={args.study_id}"
        if roll < 0.05:
            urls.append(f"{base}/tiles/{slide_id}/metadata?{scope}")
        elif roll < 0.10:
            urls.append(f"{base}/tiles/{slide_id}/thumbnail?width=256&height=256&{scope}")
        else:
            z_offset = rng.choices((0, 1, 2, 3, 4, 5), weights=(1, 1, 2, 3, 5, 8))[0]
            z = max(0, spec.max_zoom - z_offset)
            downsample = 2 ** (spec.max_zoom - z)
            grid_x = max(1, math.ceil(spec.width / (spec.tile_size * downsample)))
            grid_y = max(1, math.ceil(spec.height / (spec.tile_size * downsample)))
            x = rng.randrange(grid_x)
            y = rng.randrange(grid_y)
            urls.append(f"{base}/tiles/{slide_id}/zxy/{z}/{x}/{y}?{scope}")
    return urls


_thread_state = threading.local()


def request(url: str, token: str, timeout: float) -> Result:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    started = time.perf_counter()
    try:
        client = getattr(_thread_state, "client", None)
        if client is None or client.timeout.read != timeout:
            client = httpx.Client(timeout=timeout)
            _thread_state.client = client
        response = client.get(url, headers=headers)
        return Result(response.status_code, (time.perf_counter() - started) * 1000)
    except (httpx.HTTPError, TimeoutError, OSError) as exc:
        return Result(0, (time.perf_counter() - started) * 1000, str(exc))


def run(urls: list[str], args: argparse.Namespace) -> list[Result]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(request, url, args.bearer_token, args.timeout) for url in urls]
        return [future.result() for future in futures]


def main() -> None:
    args = parse_args()
    if args.requests < 1 or args.concurrency < 1 or args.warmup < 0:
        raise SystemExit("requests and concurrency must be positive; warmup cannot be negative")

    specs = discover_slide_specs(args)
    rng = random.Random(args.seed)
    warmup_urls = build_workload(args, args.warmup, rng, specs)
    measured_urls = build_workload(args, args.requests, rng, specs)
    if warmup_urls:
        run(warmup_urls, args)

    started = time.perf_counter()
    results = run(measured_urls, args)
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
        "base_url": args.base_url.rstrip("/"),
        "study_id": args.study_id,
        "slide_ids": args.slide_id,
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
