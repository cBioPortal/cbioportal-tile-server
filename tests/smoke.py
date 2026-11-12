"""Read-only smoke test for the WSI pixel service.

A slide is named by its resource row's study, key, and sealed source (`enc`),
taken from cBioPortal's resource data; no source URL is passed to the
service. The sealed source and cookie are read from the environment so they
never appear in shell history.

`WSI_SMOKE_COOKIE` selects the target:

- Set (the browser's portal session cookie): the host is the ingress, for
  example `https://cbioportal.example.org/wsi`. A tile request without slide
  parameters must be refused (any non-200), and the slide request without
  the cookie must be refused with 401 or 403.
- Unset: the host is the service itself. A tile request without slide
  parameters must return 400.

Example:
    export WSI_SMOKE_ENC=...               # the row's sealed source
    export WSI_SMOKE_COOKIE='SESSION=...'  # only when targeting the ingress
    python tests/smoke.py --host https://cbioportal.example.org/wsi \
        --study-id study_a --key 0123456789ab
"""

import argparse
import os
import sys

import requests


# Any status but 200: the ingress may refuse a request without a study with
# 400, 401, 403 or 5xx depending on its configuration.
REFUSED = frozenset(range(100, 600)) - {200}

def check(label: str, response: requests.Response, expected_status=200) -> bool:
    """Pass when the status equals `expected_status`, or is in it if a set."""
    if isinstance(expected_status, (set, frozenset)):
        ok = response.status_code in expected_status
    else:
        ok = response.status_code == expected_status
    print(f"  {'✓' if ok else '✗'} {label:42s} HTTP {response.status_code}")
    if not ok:
        print(f"    → {response.text[:200]}")
    return ok


def run_smoke(
    host: str,
    study_id: str,
    key: str,
    enc: str,
    cookie: str = "",
) -> bool:
    host = host.rstrip("/")
    session = requests.Session()
    if cookie:
        session.headers["Cookie"] = cookie
    slide = {"studyId": study_id, "key": key, "enc": enc}
    passed = failed = 0

    print(f"\nSmoke test: {host}")

    response = session.get(f"{host}/health", timeout=30)
    ok = check("/health", response)
    passed += int(ok); failed += int(not ok)

    response = session.get(f"{host}/ready", timeout=30)
    ok = check("/ready", response)
    passed += int(ok); failed += int(not ok)

    response = session.get(f"{host}/tiles/zxy/0/0/0", params=slide, timeout=30)
    ok = check("/tiles/zxy/0/0/0", response) and response.headers.get("content-type", "").startswith("image/")
    passed += int(ok); failed += int(not ok)

    response = session.get(
        f"{host}/thumbnails",
        params={**slide, "width": 256, "height": 256},
        timeout=30,
    )
    ok = check("/thumbnails", response) and response.headers.get("content-type", "").startswith("image/")
    passed += int(ok); failed += int(not ok)

    response = session.get(
        f"{host}/tiles/zxy/0/0/0",
        params={**slide, "source": "s3://bucket/slide.svs"},
        timeout=30,
    )
    ok = check("client-supplied source rejected", response, 400)
    passed += int(ok); failed += int(not ok)

    response = session.get(f"{host}/tiles/zxy/0/0/0", timeout=30)
    if cookie:
        ok = check("tile without slide parameters (refused)", response, REFUSED)
    else:
        ok = check("tile without slide parameters", response, 400)
    passed += int(ok); failed += int(not ok)

    if cookie:
        response = requests.get(f"{host}/tiles/zxy/0/0/0", params=slide, timeout=30)
        ok = check("tile without the session cookie", response, {401, 403})
        passed += int(ok); failed += int(not ok)

    rebound = "f" * 12 if key != "f" * 12 else "e" * 12
    response = session.get(f"{host}/tiles/zxy/0/0/0", params={**slide, "key": rebound}, timeout=30)
    ok = check("sealed source under another key", response, 404)
    passed += int(ok); failed += int(not ok)

    print(f"\n  Passed: {passed}   Failed: {failed}")
    return failed == 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="http://localhost:8081")
    parser.add_argument("--study-id", required=True)
    parser.add_argument("--key", required=True)
    args = parser.parse_args()
    enc = os.environ.get("WSI_SMOKE_ENC", "")
    if not enc:
        parser.error("set WSI_SMOKE_ENC to the slide's sealed source")
    ok = run_smoke(
        args.host,
        args.study_id,
        args.key,
        enc,
        os.environ.get("WSI_SMOKE_COOKIE", ""),
    )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
