#!/usr/bin/env python3
"""Repeatable HTTP benchmark for the WSI tile-loading workload.

Example:
  python bench/tile_bench.py \
    --base-url http://localhost:3001/wsi \
    --study-id coad_msk_2025 \
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
import time
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class Result:
    status: int
    latency_ms: float
    error: str | None = None


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
    parser.add_argument("--max-zoom", type=int, default=15)
    parser.add_argument("--tile-grid", type=int, default=8)
    parser.add_argument("--cache-mode", choices=("cold", "warm"), required=True)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--json-out")
    return parser.parse_args()


def build_workload(args: argparse.Namespace, count: int) -> list[str]:
    rng = random.Random(args.seed)
    base = args.base_url.rstrip("/")
    urls: list[str] = []
    for _ in range(count):
        roll = rng.random()
        slide_id = args.slide_id[rng.randrange(len(args.slide_id))]
        scope = f"studyId={args.study_id}"
        if roll < 0.05:
            urls.append(f"{base}/tiles/{slide_id}/metadata?{scope}")
        elif roll < 0.10:
            urls.append(f"{base}/tiles/{slide_id}/thumbnail?width=256&height=256&{scope}")
        else:
            z_offset = rng.choices((0, 1, 2, 3, 4, 5), weights=(1, 1, 2, 3, 5, 8))[0]
            z = max(0, args.max_zoom - z_offset)
            grid = max(1, args.tile_grid >> z_offset)
            x = rng.randrange(grid)
            y = rng.randrange(grid)
            urls.append(f"{base}/tiles/{slide_id}/zxy/{z}/{x}/{y}?{scope}")
    return urls


def request(url: str, token: str, timeout: float) -> Result:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    started = time.perf_counter()
    try:
        with urlopen(Request(url, headers=headers), timeout=timeout) as response:
            response.read()
            return Result(response.status, (time.perf_counter() - started) * 1000)
    except HTTPError as exc:
        return Result(exc.code, (time.perf_counter() - started) * 1000, str(exc))
    except (URLError, TimeoutError, OSError) as exc:
        return Result(0, (time.perf_counter() - started) * 1000, str(exc))


def run(urls: list[str], args: argparse.Namespace) -> list[Result]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(request, url, args.bearer_token, args.timeout) for url in urls]
        return [future.result() for future in futures]


def main() -> None:
    args = parse_args()
    if args.requests < 1 or args.concurrency < 1 or args.warmup < 0:
        raise SystemExit("requests and concurrency must be positive; warmup cannot be negative")

    warmup_urls = build_workload(args, args.warmup)
    measured_urls = build_workload(args, args.requests)
    if warmup_urls:
        run(warmup_urls, args)

    started = time.perf_counter()
    results = run(measured_urls, args)
    elapsed_s = time.perf_counter() - started
    successful = [result.latency_ms for result in results if 200 <= result.status < 300]
    failures = [result for result in results if not 200 <= result.status < 300]
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
    }
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as output:
            json.dump(summary, output, indent=2)
            output.write("\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
