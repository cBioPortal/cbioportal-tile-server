"""
BlockCache vs direct-ECS latency comparison.

Opens a slide via fsspec BlockCache, reads tiles twice (cold then warm),
and reports the speedup.  Run this to verify the cache is working before
doing a full locust load test.

Usage:
    pip install tiffslide s3fs fsspec
    python bench/blockcache_bench.py \
        --endpoint http://pmindecs.mskcc.org:9020 \
        --bucket pathology-slides \
        --slide-id 5805757 \
        --cache-dir /tmp/slide-cache \
        --profile ecs
"""

import argparse
import os
import shutil
import statistics
import time

import boto3


def get_creds(profile):
    import botocore.session
    s = botocore.session.Session(profile=profile)
    creds = s.get_credentials()
    return creds.access_key, creds.secret_key


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--endpoint", default=os.environ.get("ECS_ENDPOINT_URL", ""))
    p.add_argument("--bucket", default="pathology-slides")
    p.add_argument("--slide-id", required=True)
    p.add_argument("--cache-dir", default="/tmp/slide-cache")
    p.add_argument("--profile", default=None)
    p.add_argument("--block-size", type=int, default=8 * 1024 * 1024)
    p.add_argument("--reads", type=int, default=20,
                   help="Number of tile reads per pass")
    return p.parse_args()


def open_slide_direct(slide_id, bucket, s3_opts):
    from tiffslide import TiffSlide
    url = f"s3://{bucket}/{slide_id}.svs"
    return TiffSlide(url, storage_options=s3_opts), None


def open_slide_cached(slide_id, bucket, s3_opts, cache_dir, block_size):
    import fsspec
    from tiffslide import TiffSlide
    per_slide_cache = os.path.join(cache_dir, slide_id)
    os.makedirs(per_slide_cache, exist_ok=True)
    fs = fsspec.filesystem(
        "blockcache",
        target_protocol="s3",
        target_options=s3_opts,
        cache_storage=per_slide_cache,
        block_size=block_size,
    )
    fobj = fs.open(f"{bucket}/{slide_id}.svs", "rb")
    return TiffSlide(fobj), fobj


def time_reads(slide, n):
    """Read n tiles spread across the slide and return latencies in ms."""
    w, h = slide.dimensions
    levels = slide.level_count
    # Use the second-highest resolution level for tile reads
    lvl = min(1, levels - 1)
    lw, lh = slide.level_dimensions[lvl]
    ds = slide.level_downsamples[lvl]
    tile = 512

    latencies = []
    for i in range(n):
        # Spread reads across the slide
        x = int((lw / n * i) / tile) * tile
        y = int((lh / n * i) / tile) * tile
        x0 = int(x * ds)
        y0 = int(y * ds)
        t = time.perf_counter()
        slide.read_region((x0, y0), lvl, (tile, tile))
        latencies.append((time.perf_counter() - t) * 1000)
    return latencies


def report(label, latencies):
    s = sorted(latencies)
    n = len(s)
    print(f"\n  {label} ({n} reads):")
    print(f"    p50 : {statistics.median(s):.1f} ms")
    print(f"    p95 : {s[int(n * 0.95)]:.1f} ms")
    print(f"    max : {s[-1]:.1f} ms")


def main():
    args = parse_args()

    if args.profile:
        ak, sk = get_creds(args.profile)
    else:
        ak = os.environ.get("AWS_ACCESS_KEY_ID", "")
        sk = os.environ.get("AWS_SECRET_ACCESS_KEY", "")

    s3_opts = {
        "endpoint_url": args.endpoint or None,
        "key": ak,
        "secret": sk,
    }
    # Remove None values
    s3_opts = {k: v for k, v in s3_opts.items() if v}

    print(f"Slide   : {args.slide_id}.svs")
    print(f"Endpoint: {args.endpoint or 'AWS default'}")
    print(f"Cache   : {args.cache_dir}  (block_size={args.block_size // 1024 // 1024} MB)")

    # ---- Direct ECS (no cache) ----
    print("\n--- Direct ECS ---")
    slide_d, _ = open_slide_direct(args.slide_id, args.bucket, s3_opts)
    cold_direct = time_reads(slide_d, args.reads)
    report("cold", cold_direct)
    warm_direct = time_reads(slide_d, args.reads)
    report("warm (same process, tifffile in-memory)", warm_direct)
    slide_d.close()

    # ---- BlockCache — cold (first open, cache empty) ----
    print("\n--- BlockCache ---")
    cache_slide_dir = os.path.join(args.cache_dir, args.slide_id)
    if os.path.exists(cache_slide_dir):
        shutil.rmtree(cache_slide_dir)

    slide_c, fobj = open_slide_cached(
        args.slide_id, args.bucket, s3_opts, args.cache_dir, args.block_size
    )
    cold_cached = time_reads(slide_c, args.reads)
    report("cold (blocks fetched from ECS → disk)", cold_cached)

    # ---- BlockCache — warm (same handle, blocks on disk) ----
    warm_cached = time_reads(slide_c, args.reads)
    report("warm (blocks served from disk)", warm_cached)
    slide_c.close()
    if fobj:
        fobj.close()

    # ---- BlockCache — second open (blocks already on disk) ----
    slide_c2, fobj2 = open_slide_cached(
        args.slide_id, args.bucket, s3_opts, args.cache_dir, args.block_size
    )
    second_open = time_reads(slide_c2, args.reads)
    report("second open (cache warm across process restarts)", second_open)
    slide_c2.close()
    if fobj2:
        fobj2.close()

    # ---- Summary ----
    direct_p95 = sorted(cold_direct)[int(args.reads * 0.95)]
    warm_p95 = sorted(second_open)[int(args.reads * 0.95)]
    speedup = direct_p95 / warm_p95 if warm_p95 > 0 else float("inf")
    print(f"\n{'='*50}")
    print(f"Speedup (warm cache vs direct ECS): {speedup:.1f}×  "
          f"({direct_p95:.0f} ms → {warm_p95:.0f} ms at p95)")
    if warm_p95 < 20:
        print("✅ BlockCache delivers sub-20ms p95 — ECS latency is not the bottleneck")
    elif warm_p95 < 50:
        print("✅ BlockCache sufficient for interactive tile serving")
    else:
        print("⚠️  Cache warm reads still slow — check NVMe speed or block size")


if __name__ == "__main__":
    main()
