#!/usr/bin/env python3
"""Create small navigation thumbnails from the existing master JPEG artifacts."""

from __future__ import annotations

import argparse
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from typing import Any

import fsspec
from PIL import Image

from app.config import settings
from app.constants import INVENTORY_TABLE, THUMBNAIL_REGISTRY_TABLE
from app.meta_store import run_query_external, run_statement
from app.slide_store import s3_opts
from tools.generate_slide_thumbnails import _sql_string

logger = logging.getLogger("thumbnail-variants")

NAV_WIDTH = 128
NAV_HEIGHT = 96
DEFAULT_VARIANT_ROOT = "s3://mskmind-bkt/wsi-thumbnails/variants/nav-128x96"
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
INVENTORY_PATH_COLUMN = os.environ.get("WSI_INVENTORY_PATH_COLUMN", "path").strip()
if not _IDENTIFIER.fullmatch(INVENTORY_PATH_COLUMN):
    raise ValueError("WSI_INVENTORY_PATH_COLUMN must be a simple SQL identifier")
_TABLE_NAME = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){2}$"
)


def _table_name(value: str, label: str) -> str:
    value = value.strip()
    if not _TABLE_NAME.fullmatch(value):
        raise ValueError(f"{label} must be a three-part table name")
    return value


def _serving_columns_migration_sql(registry_table: str) -> str:
    return f"""
ALTER TABLE {registry_table}
ADD COLUMNS (
    serving_artifact_uri STRING,
    serving_width INT,
    serving_height INT
)
"""


def _registry_query(
    registry_table: str = THUMBNAIL_REGISTRY_TABLE,
    inventory_table: str = INVENTORY_TABLE,
    inventory_path_column: str = INVENTORY_PATH_COLUMN,
) -> str:
    inventory_table = _table_name(inventory_table, "inventory table")
    registry_table = _table_name(registry_table, "registry table")
    if not _IDENTIFIER.fullmatch(inventory_path_column):
        raise ValueError("inventory path column must be a simple SQL identifier")
    inventory_path = f"inventory.{inventory_path_column}"
    return f"""
WITH current_inventory AS (
    SELECT image_id, path
    FROM (
        SELECT
            CAST(inventory.image_id AS STRING) AS image_id,
            {inventory_path} AS path,
            ROW_NUMBER() OVER (
                PARTITION BY CAST(inventory.image_id AS STRING)
                ORDER BY
                    CASE
                        WHEN {inventory_path} LIKE 's3://mskmind-bkt/reef-slides/%' THEN 0
                        WHEN {inventory_path} LIKE 's3://%' THEN 1
                        ELSE 2
                    END,
                    {inventory_path}
            ) AS row_num
        FROM {inventory_table} inventory
        WHERE inventory.image_id IS NOT NULL
          AND {inventory_path} IS NOT NULL
    ) ranked_inventory
    WHERE row_num = 1
), current_registry AS (
    SELECT
        CAST(registry.image_id AS STRING) AS image_id,
        registry.source_path,
        registry.artifact_uri,
        registry.manifest_version,
        registry.serving_artifact_uri,
        registry.serving_width,
        registry.serving_height,
        ROW_NUMBER() OVER (
            PARTITION BY CAST(registry.image_id AS STRING)
            ORDER BY registry.rendered_at DESC, registry.manifest_version DESC
        ) AS row_num
    FROM {registry_table} registry
    INNER JOIN current_inventory inventory
        ON CAST(registry.image_id AS STRING) = inventory.image_id
       AND registry.source_path = inventory.path
    WHERE registry.status = 'success'
      AND registry.artifact_uri IS NOT NULL
)
SELECT image_id, source_path, artifact_uri, manifest_version,
       serving_artifact_uri, serving_width, serving_height
FROM current_registry
WHERE row_num = 1
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
            if str(row.get("serving_artifact_uri") or "") == variant_uri:
                width = int(row.get("serving_width") or 0)
                height = int(row.get("serving_height") or 0)
            else:
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
    return f"""(
    {_sql_string(row['image_id'])},
    {_sql_string(row['source_path'])},
    {_sql_string(row['manifest_version'])},
    {_sql_string(row['serving_artifact_uri'])},
    {int(row['serving_width'])},
    {int(row['serving_height'])}
)"""


def _publish(
    warehouse_id: str,
    rows: list[dict[str, Any]],
    registry_table: str = THUMBNAIL_REGISTRY_TABLE,
) -> None:
    registry_table = _table_name(registry_table, "registry table")
    for start in range(0, len(rows), 1000):
        batch = rows[start : start + 1000]
        source = ",\n".join(_upsert_sql(row) for row in batch)
        run_statement(
            f"""
MERGE INTO {registry_table} AS target
USING (
    SELECT * FROM VALUES {source}
    AS rows(image_id, source_path, manifest_version, serving_artifact_uri, serving_width, serving_height)
) AS source
ON target.image_id = source.image_id
AND COALESCE(target.source_path, '') = COALESCE(source.source_path, '')
AND COALESCE(target.manifest_version, '') = COALESCE(source.manifest_version, '')
WHEN MATCHED THEN UPDATE SET
    target.serving_artifact_uri = source.serving_artifact_uri,
    target.serving_width = source.serving_width,
    target.serving_height = source.serving_height
""",
            warehouse_id,
        )


def _ensure_serving_columns(
    warehouse_id: str,
    registry_table: str = THUMBNAIL_REGISTRY_TABLE,
) -> None:
    """Add the serving-pointer columns once, while keeping reruns idempotent."""
    registry_table = _table_name(registry_table, "registry table")
    try:
        run_statement(_serving_columns_migration_sql(registry_table), warehouse_id)
    except Exception as exc:
        if "already exists" not in str(exc).lower():
            raise


def _registry_batch_query(
    after_image_id: str | None,
    batch_size: int,
    after_source_path: str | None = None,
    registry_table: str = THUMBNAIL_REGISTRY_TABLE,
    inventory_table: str = INVENTORY_TABLE,
    inventory_path_column: str = INVENTORY_PATH_COLUMN,
) -> str:
    if after_image_id is None:
        after_clause = ""
    else:
        after_image = _sql_string(after_image_id)
        after_source = _sql_string(after_source_path or "")
        after_clause = (
            "AND (CAST(image_id AS STRING) > "
            f"{after_image} OR (CAST(image_id AS STRING) = {after_image} "
            f"AND COALESCE(source_path, '') > {after_source}))"
        )
    return (
        f"{_registry_query(registry_table, inventory_table, inventory_path_column)} {after_clause} "
        f"ORDER BY CAST(image_id AS STRING), COALESCE(source_path, '') LIMIT {batch_size}"
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


def run_rows(
    *,
    warehouse_id: str,
    rows: list[dict[str, Any]],
    root_uri: str,
    workers: int,
    batch_size: int,
    force: bool,
    registry_table: str = THUMBNAIL_REGISTRY_TABLE,
) -> dict[str, int]:
    """Publish variants for a bounded set of newly published registry rows."""
    _ensure_serving_columns(warehouse_id, registry_table)
    candidate_count = 0
    published_count = 0
    failed_count = 0
    skipped_count = 0
    batch_size = max(1, batch_size)
    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        candidate_count += len(batch)
        published, batch_failures = _render_batch(batch, root_uri, workers, force)
        _publish(warehouse_id, published, registry_table)
        published_count += len(published)
        failed_count += batch_failures
        skipped_count += sum(1 for row in published if row.get("skipped"))
    return {
        "candidate_count": candidate_count,
        "published_count": published_count,
        "failed_count": failed_count,
        "skipped_count": skipped_count,
    }


def run(
    *,
    warehouse_id: str,
    root_uri: str,
    workers: int,
    batch_size: int,
    limit: int | None,
    force: bool,
    registry_table: str = THUMBNAIL_REGISTRY_TABLE,
    inventory_table: str = INVENTORY_TABLE,
    inventory_path_column: str = INVENTORY_PATH_COLUMN,
) -> dict[str, int]:
    _ensure_serving_columns(warehouse_id, registry_table)
    batch_size = max(1, batch_size)
    candidate_count = 0
    published_count = 0
    failed_count = 0
    skipped_count = 0
    after_image_id: str | None = None
    after_source_path: str | None = None
    while limit is None or candidate_count < limit:
        query_size = batch_size if limit is None else min(batch_size, limit - candidate_count)
        rows = list(
            run_query_external(
                _registry_batch_query(
                    after_image_id,
                    query_size,
                    after_source_path,
                    registry_table,
                    inventory_table,
                    inventory_path_column,
                ),
                warehouse_id,
            )
        )
        if not rows:
            break
        candidate_count += len(rows)
        published, batch_failures = _render_batch(rows, root_uri, workers, force)
        _publish(warehouse_id, published, registry_table)
        published_count += len(published)
        failed_count += batch_failures
        skipped_count += sum(1 for row in published if row.get("skipped"))
        after_image_id = str(rows[-1]["image_id"])
        after_source_path = str(rows[-1].get("source_path") or "")
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
    parser.add_argument("--registry-table", default=THUMBNAIL_REGISTRY_TABLE)
    parser.add_argument("--inventory-table", default=INVENTORY_TABLE)
    parser.add_argument("--inventory-path-column", default=INVENTORY_PATH_COLUMN)
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
        registry_table=_table_name(args.registry_table, "registry table"),
        inventory_table=_table_name(args.inventory_table, "inventory table"),
        inventory_path_column=args.inventory_path_column,
    )
    logger.info("thumbnail variant summary=%s", summary)
    return 0 if summary["failed_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
