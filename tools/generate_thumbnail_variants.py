#!/usr/bin/env python3
"""Create small navigation thumbnails from the existing master JPEG artifacts."""

from __future__ import annotations

import argparse
import logging
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from typing import Any

import fsspec
from PIL import Image

from app.config import settings
from app.constants import THUMBNAIL_REGISTRY_TABLE
from app.meta_store import run_query_external, run_statement
from app.slide_store import s3_opts
from tools.generate_slide_thumbnails import _sql_string

logger = logging.getLogger("thumbnail-variants")

NAV_WIDTH = 128
NAV_HEIGHT = 96
DEFAULT_VARIANT_ROOT = "s3://mskmind-bkt/wsi-thumbnails/variants/nav-128x96"

SERVING_COLUMNS_MIGRATION_SQL = f"""
ALTER TABLE {THUMBNAIL_REGISTRY_TABLE}
ADD COLUMNS (
    serving_artifact_uri STRING,
    serving_width INT,
    serving_height INT
)
"""

REGISTRY_QUERY = f"""
SELECT
    CAST(image_id AS STRING) AS image_id,
    source_path,
    artifact_uri,
    manifest_version
FROM {THUMBNAIL_REGISTRY_TABLE}
WHERE status = 'success'
  AND artifact_uri IS NOT NULL
"""


def _filesystem(uri: str):
    if uri.startswith("s3://"):
        return fsspec.filesystem("s3", **s3_opts())
    return fsspec.filesystem("file")


def _path(uri: str) -> str:
    if uri.startswith("s3://"):
        return uri[5:]
    if uri.startswith("file://"):
        return uri[7:]
    return uri


def variant_root_for_master(master_root_uri: str) -> str:
    root = master_root_uri.rstrip('/')
    if root.endswith('/masters'):
        return root[: -len('/masters')] + '/variants/nav-128x96'
    return root + '/variants/nav-128x96'


def _join_uri(root_uri: str, image_id: str, manifest_version: str) -> str:
    return f"{root_uri.rstrip('/')}/{manifest_version}/{image_id}.jpg"


def _fit_thumbnail(payload: bytes) -> tuple[bytes, int, int]:
    with Image.open(BytesIO(payload)) as image:
        working = image.convert("RGB")
        working.thumbnail((NAV_WIDTH, NAV_HEIGHT), Image.Resampling.LANCZOS)
        out = BytesIO()
        working.save(out, format="JPEG", quality=settings.jpeg_quality)
        width, height = working.size
    return out.getvalue(), width, height


def _read(uri: str) -> bytes:
    fs = _filesystem(uri)
    with fs.open(_path(uri), "rb") as handle:
        return handle.read()


def _dimensions(uri: str) -> tuple[int, int]:
    with Image.open(BytesIO(_read(uri))) as image:
        return image.size


def _write(uri: str, payload: bytes) -> None:
    fs = _filesystem(uri)
    path = _path(uri)
    if not uri.startswith("s3://"):
        fs.makedirs(str(Path(path).parent), exist_ok=True)
    with fs.open(path, "wb") as handle:
        handle.write(payload)


def _render(row: dict[str, Any], root_uri: str, force: bool) -> dict[str, Any]:
    image_id = str(row["image_id"])
    manifest_version = str(row.get("manifest_version") or "legacy")
    variant_uri = _join_uri(root_uri, image_id, manifest_version)
    if not force:
        fs = _filesystem(variant_uri)
        if fs.exists(_path(variant_uri)):
            width, height = _dimensions(variant_uri)
            return {
                "image_id": image_id,
                "source_path": str(row.get("source_path") or ""),
                "manifest_version": manifest_version,
                "serving_artifact_uri": variant_uri,
                "serving_width": width,
                "serving_height": height,
                "skipped": True,
            }
    payload, width, height = _fit_thumbnail(_read(str(row["artifact_uri"])))
    _write(variant_uri, payload)
    return {
        "image_id": image_id,
        "source_path": str(row.get("source_path") or ""),
        "manifest_version": manifest_version,
        "serving_artifact_uri": variant_uri,
        "serving_width": width,
        "serving_height": height,
        "skipped": False,
    }


def _upsert_sql(row: dict[str, Any]) -> str:
    return f"""
SELECT
    {_sql_string(row['image_id'])} AS image_id,
    {_sql_string(row['source_path'])} AS source_path,
    {_sql_string(row['serving_artifact_uri'])} AS serving_artifact_uri,
    {int(row['serving_width'])} AS serving_width,
    {int(row['serving_height'])} AS serving_height
"""


def _publish(warehouse_id: str, rows: list[dict[str, Any]]) -> None:
    for start in range(0, len(rows), 100):
        batch = rows[start : start + 100]
        source = "\nUNION ALL\n".join(_upsert_sql(row) for row in batch)
        run_statement(
            f"""
MERGE INTO {THUMBNAIL_REGISTRY_TABLE} AS target
USING ({source}) AS source
ON target.image_id = source.image_id
AND COALESCE(target.source_path, '') = COALESCE(source.source_path, '')
WHEN MATCHED THEN UPDATE SET
    target.serving_artifact_uri = source.serving_artifact_uri,
    target.serving_width = source.serving_width,
    target.serving_height = source.serving_height
""",
            warehouse_id,
        )


def _ensure_serving_columns(warehouse_id: str) -> None:
    """Add the serving-pointer columns once, while keeping reruns idempotent."""
    try:
        run_statement(SERVING_COLUMNS_MIGRATION_SQL, warehouse_id)
    except Exception as exc:
        if "already exists" not in str(exc).lower():
            raise


def _registry_batch_query(after_image_id: str | None, batch_size: int) -> str:
    after_clause = "" if after_image_id is None else f"AND CAST(image_id AS STRING) > {_sql_string(after_image_id)}"
    return (
        f"{REGISTRY_QUERY} {after_clause} "
        f"ORDER BY CAST(image_id AS STRING) LIMIT {batch_size}"
    )


def _render_batch(
    rows: list[dict[str, Any]], root_uri: str, workers: int, force: bool
) -> tuple[list[dict[str, Any]], int]:
    published: list[dict[str, Any]] = []
    failures = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(_render, row, root_uri, force) for row in rows]
        for row, future in zip(rows, futures):
            try:
                published.append(future.result())
            except Exception as exc:
                failures += 1
                logger.error(
                    "variant generation failed; image_id=%s error_type=%s",
                    row.get("image_id"),
                    type(exc).__name__,
                )
    return published, failures


def run(
    *,
    warehouse_id: str,
    root_uri: str,
    workers: int,
    batch_size: int,
    limit: int | None,
    force: bool,
) -> dict[str, int]:
    _ensure_serving_columns(warehouse_id)
    batch_size = max(1, batch_size)
    candidate_count = 0
    published_count = 0
    failed_count = 0
    skipped_count = 0
    after_image_id: str | None = None
    while limit is None or candidate_count < limit:
        query_size = batch_size if limit is None else min(batch_size, limit - candidate_count)
        rows = list(
            run_query_external(
                _registry_batch_query(after_image_id, query_size), warehouse_id
            )
        )
        if not rows:
            break
        candidate_count += len(rows)
        published, batch_failures = _render_batch(rows, root_uri, workers, force)
        _publish(warehouse_id, published)
        published_count += len(published)
        failed_count += batch_failures
        skipped_count += sum(1 for row in published if row.get("skipped"))
        after_image_id = str(rows[-1]["image_id"])
        logger.info(
            "thumbnail variant progress candidates=%d published=%d failed=%d",
            candidate_count,
            published_count,
            failed_count,
        )
        if len(rows) < query_size:
            break
    return {
        "candidate_count": candidate_count,
        "published_count": published_count,
        "failed_count": failed_count,
        "skipped_count": skipped_count,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--warehouse-id", default=settings.databricks_warehouse_id)
    parser.add_argument("--variant-root-uri", default=DEFAULT_VARIANT_ROOT)
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument("--batch-size", type=int, default=5000)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    summary = run(
        warehouse_id=args.warehouse_id,
        root_uri=args.variant_root_uri,
        workers=args.workers,
        batch_size=args.batch_size,
        limit=args.limit,
        force=args.force,
    )
    logger.info("thumbnail variant summary=%s", summary)
    return 0 if summary["failed_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
