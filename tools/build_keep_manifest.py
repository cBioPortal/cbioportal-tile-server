#!/usr/bin/env python3
"""Build a server-side research manifest for KEEP-adapter region search.

Input rows (JSON Lines, one object per servable slide):

    {"study_id", "slide_key", "sealed_source", "slide_width", "slide_height"}

Each sealed source is opened with WSI_SOURCE_SEAL_KEY (AAD = slide key) and
the PDM image ID is taken from the tile source's file name
(``s3://<bucket>/.../<image_id>.<ext>``).  Rows whose file name is not a
numeric image ID, or whose file name disagrees with the sealed image ID, are
skipped.  With ``--wds-index`` only image IDs that have stored H-optimus-1
features are written.

The image ID is an internal identifier: the manifest is read only by the tile
server, which passes it to the retrieval worker.  This tool prints counts
only, never image IDs, sources or sealed values, and writes the manifest with
mode 640 in a directory with mode 750.

Usage:
    uv run python tools/build_keep_manifest.py --rows rows.jsonl \\
        --seal-key-env-file stack/.env --out research/manifest.json \\
        --wds-index s3://reef-v2-0/wds/hoptimus1/wds_index.json \\
        --s3-endpoint http://pmindecs.mskcc.org:9020 --aws-profile ecs
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.auth import InvalidWsiToken, decode_source_seal_key, decrypt_sealed_claims  # noqa: E402
from app.sealed_source import SLIDE_KEY_PATTERN  # noqa: E402

MODEL_ID = "reef_v2_hoptimus1_keep"
MODEL_PROVIDER = "keep_adapter"
MODEL_CAPABILITIES = ["text_search"]
_IMAGE_ID_FILE = re.compile(r"^(\d{1,20})\.[A-Za-z0-9]{2,8}$")


def image_id_from_source(source: str) -> str | None:
    """Return the numeric PDM image ID named by an s3 slide source, if any."""
    parsed = urlparse(source)
    if parsed.scheme != "s3" or not parsed.netloc:
        return None
    name = parsed.path.rsplit("/", 1)[-1]
    match = _IMAGE_ID_FILE.fullmatch(name)
    return match.group(1) if match else None


def read_seal_key(env_file: str | None) -> bytes:
    """Read WSI_SOURCE_SEAL_KEY from an env file or the environment."""
    value = ""
    if env_file:
        for line in Path(env_file).read_text(encoding="utf-8").splitlines():
            name, _, raw = line.partition("=")
            if name.strip() == "WSI_SOURCE_SEAL_KEY":
                value = raw.strip().strip("'\"")
    else:
        value = os.environ.get("WSI_SOURCE_SEAL_KEY", "")
    return decode_source_seal_key(value)


def load_wds_index(uri: str, endpoint: str | None, profile: str | None) -> set[str]:
    """Return the image IDs present in a WebDataset index (local path or s3)."""
    parsed = urlparse(uri)
    if parsed.scheme == "s3":
        import boto3

        session = boto3.Session(profile_name=profile or None)
        client = session.client("s3", endpoint_url=endpoint or None)
        body = client.get_object(Bucket=parsed.netloc, Key=parsed.path.lstrip("/"))["Body"].read()
        payload = json.loads(body)
    else:
        payload = json.loads(Path(parsed.path if parsed.scheme == "file" else uri).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("WDS index must be a JSON object keyed by image ID")
    return {str(key) for key in payload}


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def build_rows(
    rows: Iterable[dict[str, Any]],
    seal_key: bytes,
    wds_image_ids: set[str] | None,
    counts: Counter,
) -> Iterator[dict[str, Any]]:
    """Yield manifest slide rows; tally outcomes in ``counts``."""
    seen: set[tuple[str, str]] = set()
    for row in rows:
        counts["considered"] += 1
        study_id = str(row.get("study_id") or "").strip()
        slide_key = str(row.get("slide_key") or "").strip()
        width = _positive_int(row.get("slide_width"))
        height = _positive_int(row.get("slide_height"))
        if not study_id or not SLIDE_KEY_PATTERN.fullmatch(slide_key):
            counts["skipped_invalid_key"] += 1
            continue
        if (study_id, slide_key) in seen:
            counts["skipped_duplicate"] += 1
            continue
        seen.add((study_id, slide_key))
        if width is None or height is None:
            counts["skipped_no_dimensions"] += 1
            continue
        try:
            claims = decrypt_sealed_claims(row.get("sealed_source"), seal_key, slide_key)
        except InvalidWsiToken:
            counts["skipped_unsealable"] += 1
            continue
        image_id = image_id_from_source(claims["tile_source"])
        if image_id is None:
            counts["skipped_not_derivable"] += 1
            continue
        if image_id != claims["image_id"].strip():
            counts["skipped_image_id_mismatch"] += 1
            continue
        counts["image_id_derived"] += 1
        if wds_image_ids is not None:
            if image_id not in wds_image_ids:
                counts["skipped_no_features"] += 1
                continue
            counts["in_wds_index"] += 1
        counts["written"] += 1
        yield {
            "study_id": study_id,
            "slide_id": slide_key,
            "slide_width": width,
            "slide_height": height,
            "models": [
                {
                    "id": MODEL_ID,
                    "provider": MODEL_PROVIDER,
                    "image_id": image_id,
                    "slide_width": width,
                    "slide_height": height,
                    "capabilities": list(MODEL_CAPABILITIES),
                }
            ],
        }


def write_manifest(path: Path, slides: list[dict[str, Any]]) -> None:
    """Atomically write the manifest with mode 640 in a mode-750 directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o750)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o640)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "slides": slides}, handle, separators=(",", ":"))
        os.replace(temporary, path)
        os.chmod(path, 0o640)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _read_rows(source: str) -> Iterator[dict[str, Any]]:
    handle = sys.stdin if source == "-" else open(source, encoding="utf-8")
    try:
        for line in handle:
            line = line.strip()
            if line:
                value = json.loads(line)
                if isinstance(value, dict):
                    yield value
    finally:
        if handle is not sys.stdin:
            handle.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rows", required=True, help="JSON Lines input ('-' for stdin)")
    parser.add_argument("--out", required=True, help="manifest path to write")
    parser.add_argument("--seal-key-env-file", help="env file holding WSI_SOURCE_SEAL_KEY (default: environment)")
    parser.add_argument("--wds-index", help="WDS index (s3:// or path); keep only image IDs it lists")
    parser.add_argument("--s3-endpoint", help="S3 endpoint for --wds-index")
    parser.add_argument("--aws-profile", help="AWS profile for --wds-index")
    args = parser.parse_args(argv)

    try:
        seal_key = read_seal_key(args.seal_key_env_file)
    except InvalidWsiToken:
        print("WSI_SOURCE_SEAL_KEY is missing or invalid", file=sys.stderr)
        return 2
    wds_image_ids = (
        load_wds_index(args.wds_index, args.s3_endpoint, args.aws_profile)
        if args.wds_index
        else None
    )
    counts: Counter = Counter()
    slides = list(build_rows(_read_rows(args.rows), seal_key, wds_image_ids, counts))
    write_manifest(Path(args.out), slides)
    for name in (
        "considered",
        "image_id_derived",
        "in_wds_index",
        "written",
        "skipped_invalid_key",
        "skipped_duplicate",
        "skipped_no_dimensions",
        "skipped_unsealable",
        "skipped_not_derivable",
        "skipped_image_id_mismatch",
        "skipped_no_features",
    ):
        if name in counts or name in {"considered", "image_id_derived", "written"}:
            print(f"{name}: {counts[name]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
