"""Read-only smoke test for the source-bound WSI pixel service.

Example:
    python tests/smoke.py --host http://localhost:8081 \
        --source-url s3://bucket/slide.svs \
        --thumbnail-source-url s3://bucket/thumb.jpg \
        --bearer-token "$WSI_TOKEN"
"""

import argparse
import sys

import requests


def check(label: str, response: requests.Response, expected_status: int = 200) -> bool:
    ok = response.status_code == expected_status
    print(f"  {'✓' if ok else '✗'} {label:42s} HTTP {response.status_code}")
    if not ok:
        print(f"    → {response.text[:200]}")
    return ok


def run_smoke(
    host: str,
    source_url: str,
    bearer_token: str,
    thumbnail_source_url: str | None = None,
) -> bool:
    host = host.rstrip("/")
    headers = {"Authorization": f"Bearer {bearer_token}"} if bearer_token else {}
    source_headers = {**headers, "X-WSI-Source": source_url}
    thumbnail_headers = {**headers, "X-WSI-Source": thumbnail_source_url or source_url}
    session = requests.Session()
    session.headers.update(headers)
    passed = failed = 0

    print(f"\nSmoke test: {host}")
    print("  source   : [redacted]")

    response = session.get(f"{host}/health", timeout=30)
    ok = check("/health", response)
    passed += int(ok); failed += int(not ok)

    response = session.get(f"{host}/ready", timeout=30)
    ok = check("/ready", response)
    passed += int(ok); failed += int(not ok)

    response = session.get(f"{host}/tiles/zxy/0/0/0", headers=source_headers, timeout=30)
    ok = check("/tiles/zxy/0/0/0", response) and response.headers.get("content-type", "").startswith("image/")
    passed += int(ok); failed += int(not ok)

    response = session.get(
        f"{host}/thumbnails?width=256&height=256",
        headers=thumbnail_headers,
        timeout=30,
    )
    ok = check("/thumbnails", response) and response.headers.get("content-type", "").startswith("image/")
    passed += int(ok); failed += int(not ok)

    unauthenticated = requests.get(
        f"{host}/tiles/zxy/0/0/0",
        headers={"X-WSI-Source": source_url},
        timeout=30,
    )
    ok = check("unauthenticated tile", unauthenticated, 401)
    passed += int(ok); failed += int(not ok)

    print(f"\n  Passed: {passed}   Failed: {failed}")
    return failed == 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="http://localhost:8081")
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--thumbnail-source-url", default="")
    parser.add_argument("--bearer-token", default="")
    args = parser.parse_args()
    thumbnail_source_url = args.thumbnail_source_url or args.source_url
    sys.exit(0 if run_smoke(args.host, args.source_url, args.bearer_token, thumbnail_source_url) else 1)


if __name__ == "__main__":
    main()
