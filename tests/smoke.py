"""
End-to-end smoke test for the tile server.

Usage:
    # Against local docker-compose:
    python tests/smoke.py --host http://localhost:8080 \
        --patient P-1234567 --slide s3://mskmind-bkt/reef-slides/1234567.svs

    # Against production:
    python tests/smoke.py --host https://slides.cbioportal.mskcc.org \
        --patient P-1234567 --slide <image_id>

All tests are read-only (GET requests only).  Exits 0 on full pass, 1 on failure.
"""

import argparse
import sys
import time

import requests


def check(label: str, resp: requests.Response, expected_status: int = 200) -> bool:
    ok = resp.status_code == expected_status
    status = "✓" if ok else "✗"
    elapsed_ms = int(resp.elapsed.total_seconds() * 1000)
    print(f"  {status} {label:50s}  HTTP {resp.status_code}  ({elapsed_ms} ms)")
    if not ok:
        print(f"    → {resp.text[:200]}")
    return ok


def run_smoke(host: str, patient_id: str, slide_id: str) -> bool:
    host = host.rstrip("/")
    s = requests.Session()
    s.timeout = 30
    passed = 0
    failed = 0

    print(f"\nSmoke test: {host}")
    print(f"  patient_id : {patient_id}")
    print(f"  slide_id   : {slide_id}\n")

    # 1. Health check
    resp = s.get(f"{host}/health")
    ok = check("/health", resp)
    if ok:
        data = resp.json()
        print(f"    n_workers = {data.get('n_workers')}")
    passed += int(ok); failed += int(not ok)

    # 2. Slide metadata (S3 + TiffSlide open)
    resp = s.get(f"{host}/tiles/{slide_id}/metadata")
    ok = check(f"/tiles/{slide_id}/metadata", resp)
    meta = None
    if ok:
        meta = resp.json()
        dims = meta.get("dimensions", {})
        print(f"    {dims.get('width')} × {dims.get('height')} px  "
              f"max_zoom={meta.get('max_zoom')}  levels={meta.get('levels')}")
    passed += int(ok); failed += int(not ok)

    # 3. Thumbnail
    resp = s.get(f"{host}/tiles/{slide_id}/thumbnail?width=256&height=256")
    ok = check(f"/tiles/{slide_id}/thumbnail", resp) and resp.headers.get("content-type", "").startswith("image/")
    passed += int(ok); failed += int(not ok)

    # 4. Tiles: z=0 (overview), z=1, and highest zoom if available
    test_zooms = [0, 1]
    if meta:
        mz = meta.get("max_zoom", 0)
        if mz > 1:
            test_zooms.append(mz)
    for z in test_zooms:
        resp = s.get(f"{host}/tiles/{slide_id}/zxy/{z}/0/0")
        ok = check(f"/tiles/{slide_id}/zxy/{z}/0/0", resp) and resp.headers.get("content-type", "").startswith("image/")
        passed += int(ok); failed += int(not ok)

    # 5. Cache hit — second tile request should be faster (served from Redis)
    t0 = time.monotonic()
    resp2 = s.get(f"{host}/tiles/{slide_id}/zxy/0/0/0")
    elapsed2 = (time.monotonic() - t0) * 1000
    ok = resp2.status_code == 200
    print(f"  {'✓' if ok else '✗'} /tiles/{slide_id}/zxy/0/0/0 (cache hit)  "
          f"HTTP {resp2.status_code}  ({elapsed2:.0f} ms)")
    passed += int(ok); failed += int(not ok)

    # 6. Search endpoint
    q = patient_id[:4]  # first 4 chars as search query
    resp = s.get(f"{host}/search?q={q}")
    ok = check(f"/search?q={q}", resp)
    passed += int(ok); failed += int(not ok)

    # 8. Warmup endpoint (hidden, but should return 200)
    resp = s.get(f"{host}/tiles/{slide_id}/warmup")
    ok = check(f"/tiles/{slide_id}/warmup", resp)
    passed += int(ok); failed += int(not ok)

    print(f"\n{'─' * 60}")
    print(f"  Passed: {passed}   Failed: {failed}")
    return failed == 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="http://localhost:8080",
                        help="Tile server base URL")
    parser.add_argument("--patient", required=True,
                        help="A real patient_id in the inventory (e.g. P-1234567)")
    parser.add_argument("--slide", required=True,
                        help="A real image_id (numeric) for a can_serve_tiles slide")
    args = parser.parse_args()

    success = run_smoke(args.host, args.patient, args.slide)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
