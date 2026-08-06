#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any

import fsspec
from PIL import Image

from app.config import settings
from app.constants import INVENTORY_TABLE
from app.constants import THUMBNAIL_REGISTRY_TABLE
from app.meta_store import run_query_external, run_statement
from app.slide_store import open_slide, s3_opts
from app.tiles import NoSafeThumbnailOverview
from app.tiles import get_thumbnail_bytes_with_plan

logger = logging.getLogger("thumbnail-generator")
REGISTRY_UPSERT_BATCH_SIZE = 250

SERVABLE_SLIDES_SQL = """
SELECT image_id, path
FROM (
    SELECT
        CAST(image_id AS STRING) AS image_id,
        path,
        ROW_NUMBER() OVER (
            PARTITION BY CAST(image_id AS STRING)
            ORDER BY
                CASE
                    WHEN path LIKE 's3://mskmind-bkt/reef-slides/%' THEN 0
                    ELSE 1
                END,
                path
        ) AS row_num
    FROM {inventory_table}
    WHERE image_id IS NOT NULL
      AND path LIKE 's3://%'
) ranked_inventory
WHERE row_num = 1
""".format(inventory_table=INVENTORY_TABLE)

REGISTRY_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS {registry_table} (
    image_id STRING,
    source_path STRING,
    artifact_uri STRING,
    width INT,
    height INT,
    content_type STRING,
    status STRING,
    rendered_at TIMESTAMP,
    error_message STRING,
    manifest_version STRING
)
USING DELTA
""".format(registry_table=THUMBNAIL_REGISTRY_TABLE)

REGISTRY_SELECT_SQL = """
SELECT
    image_id,
    source_path,
    artifact_uri,
    width,
    height,
    content_type,
    status,
    rendered_at,
    error_message,
    manifest_version
FROM {registry_table}
""".format(registry_table=THUMBNAIL_REGISTRY_TABLE)


@dataclass(frozen=True)
class InventoryRow:
    image_id: str
    path: str


@dataclass(frozen=True)
class RegistryRow:
    image_id: str
    source_path: str
    artifact_uri: str
    width: int
    height: int
    content_type: str
    status: str
    rendered_at: str
    error_message: str
    manifest_version: str


def _filesystem_for_uri(uri: str):
    if uri.startswith("s3://"):
        return fsspec.filesystem("s3", **s3_opts())
    return fsspec.filesystem("file")


def _filesystem_path(uri: str) -> str:
    if uri.startswith("s3://"):
        return uri[5:]
    if uri.startswith("file://"):
        return uri[7:]
    return uri


def _join_uri(root_uri: str, leaf: str) -> str:
    return root_uri.rstrip("/") + "/" + leaf.lstrip("/")


def _write_bytes(uri: str, data: bytes) -> None:
    fs = _filesystem_for_uri(uri)
    path = _filesystem_path(uri)
    parent = path.rsplit("/", 1)[0] if "/" in path else ""
    protocols = fs.protocol if isinstance(fs.protocol, (tuple, list)) else (fs.protocol,)
    if parent and "file" in protocols:
        fs.makedirs(parent, exist_ok=True)
    with fs.open(path, "wb") as handle:
        handle.write(data)


def _read_json(uri: str) -> dict[str, Any]:
    fs = _filesystem_for_uri(uri)
    with fs.open(_filesystem_path(uri), "r") as handle:
        return json.load(handle)


def _copy_uri(source_uri: str, destination_uri: str) -> None:
    source_fs = _filesystem_for_uri(source_uri)
    source_path = _filesystem_path(source_uri)
    destination_fs = _filesystem_for_uri(destination_uri)
    destination_path = _filesystem_path(destination_uri)

    if source_fs.protocol == destination_fs.protocol:
        destination_fs.copy(source_path, destination_path)
        return

    with source_fs.open(source_path, "rb") as src, destination_fs.open(destination_path, "wb") as dst:
        dst.write(src.read())


def _sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _sql_nullable_string(value: str | None) -> str:
    if value is None:
        return "NULL"
    return _sql_string(value)


def _sql_nullable_int(value: int | None) -> str:
    if value is None:
        return "NULL"
    return str(value)


@contextmanager
def _without_blockcache():
    original = settings.blockcache_path
    settings.blockcache_path = ""
    try:
        yield
    finally:
        settings.blockcache_path = original


def _build_thumbnail_record(image_id: str, slide_uri: str, master_size: int) -> dict:
    with _without_blockcache():
        slide, fileobj = open_slide(slide_uri, logger)
    try:
        try:
            thumb_bytes, plan = get_thumbnail_bytes_with_plan(slide, master_size, master_size)
        except NoSafeThumbnailOverview:
            # Offline generation is allowed to do heavier work so every servable
            # slide can still receive a published thumbnail artifact.
            image = slide.get_thumbnail((master_size, master_size)).convert("RGB")
            out = BytesIO()
            image.save(out, format="JPEG", quality=settings.jpeg_quality)
            thumb_bytes = out.getvalue()
            plan = None
    finally:
        try:
            slide.close()
        except Exception:
            pass
        if fileobj is not None:
            try:
                fileobj.close()
            except Exception:
                pass

    with Image.open(BytesIO(thumb_bytes)) as image:
        width, height = image.size

    return {
        "bytes": thumb_bytes,
        "width": width,
        "height": height,
        "level": None if plan is None else plan.level,
        "requested_pixels": None if plan is None else plan.requested_pixels,
    }


def _render_candidate_artifact(
    *,
    image_id: str,
    slide_uri: str,
    artifact_uri: str,
    master_size: int,
) -> dict[str, Any]:
    artifact = _build_thumbnail_record(image_id, slide_uri, master_size)
    _write_bytes(artifact_uri, artifact["bytes"])
    return {
        "image_id": image_id,
        "source_path": slide_uri,
        "artifact_uri": artifact_uri,
        "width": int(artifact["width"]),
        "height": int(artifact["height"]),
        "content_type": "image/jpeg",
        "level": artifact["level"],
        "requested_pixels": artifact["requested_pixels"],
    }


def _render_candidate_artifact_subprocess(
    *,
    image_id: str,
    slide_uri: str,
    artifact_uri: str,
    master_size: int,
) -> dict[str, Any]:
    with tempfile.NamedTemporaryFile(
        prefix=f"slide-thumbnail-{image_id}-",
        suffix=".json",
        delete=False,
    ) as handle:
        output_path = handle.name
    try:
        subprocess.run(
            [
                sys.executable,
                __file__,
                "--render-single-image-id",
                image_id,
                "--render-single-source-path",
                slide_uri,
                "--render-single-artifact-uri",
                artifact_uri,
                "--render-single-master-size",
                str(master_size),
                "--render-single-output-path",
                output_path,
            ],
            check=True,
            env=os.environ.copy(),
        )
        with open(output_path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    finally:
        try:
            os.unlink(output_path)
        except FileNotFoundError:
            pass


def _normalize_inventory_rows(rows: list[dict[str, Any]]) -> list[InventoryRow]:
    return [InventoryRow(image_id=str(row["image_id"]), path=str(row["path"])) for row in rows]


def _normalize_registry_rows(rows: list[dict[str, Any]]) -> list[RegistryRow]:
    normalized: list[RegistryRow] = []
    for row in rows:
        normalized.append(
            RegistryRow(
                image_id=str(row["image_id"]),
                source_path=str(row.get("source_path") or ""),
                artifact_uri=str(row.get("artifact_uri") or ""),
                width=int(row.get("width") or 0),
                height=int(row.get("height") or 0),
                content_type=str(row.get("content_type") or "image/jpeg"),
                status=str(row.get("status") or ""),
                rendered_at=str(row.get("rendered_at") or ""),
                error_message=str(row.get("error_message") or ""),
                manifest_version=str(row.get("manifest_version") or ""),
            )
        )
    return normalized


def _fetch_inventory_rows(warehouse_id: str) -> list[InventoryRow]:
    return _normalize_inventory_rows(run_query_external(SERVABLE_SLIDES_SQL, warehouse_id))


def _fetch_registry_rows(warehouse_id: str) -> list[RegistryRow]:
    return _normalize_registry_rows(run_query_external(REGISTRY_SELECT_SQL, warehouse_id))


def _ensure_registry_table(warehouse_id: str) -> None:
    run_statement(REGISTRY_CREATE_SQL, warehouse_id)


def _dedupe_inventory_rows(rows: list[InventoryRow]) -> list[InventoryRow]:
    deduped_rows: list[InventoryRow] = []
    seen_image_ids: set[str] = set()
    for row in rows:
        if row.image_id in seen_image_ids:
            continue
        seen_image_ids.add(row.image_id)
        deduped_rows.append(row)
    return deduped_rows


def _slice_candidate_rows(
    rows: list[InventoryRow],
    *,
    task_index: int,
    task_count: int,
) -> list[InventoryRow]:
    if task_count <= 0:
        raise ValueError("task_count must be positive")
    if task_index < 0 or task_index >= task_count:
        raise ValueError("task_index out of range")
    return [row for index, row in enumerate(rows) if index % task_count == task_index]


def write_candidate_rows(path: str, rows: list[InventoryRow]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps({"image_id": row.image_id, "path": row.path}, sort_keys=True))
            handle.write("\n")


def read_candidate_rows(path: str) -> list[InventoryRow]:
    rows: list[InventoryRow] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            payload = json.loads(line)
            rows.append(
                InventoryRow(
                    image_id=str(payload["image_id"]),
                    path=str(payload["path"]),
                )
            )
    return rows


def _registry_by_image_id(rows: list[RegistryRow]) -> dict[str, RegistryRow]:
    by_image_id: dict[str, RegistryRow] = {}
    for row in rows:
        existing = by_image_id.get(row.image_id)
        if existing is None:
            by_image_id[row.image_id] = row
            continue
        existing_priority = 0 if existing.status == "success" else 1
        row_priority = 0 if row.status == "success" else 1
        if row_priority < existing_priority:
            by_image_id[row.image_id] = row
            continue
        if row_priority == existing_priority and row.rendered_at > existing.rendered_at:
            by_image_id[row.image_id] = row
    return by_image_id


def _select_candidate_rows(
    inventory_rows: list[InventoryRow],
    registry_rows: list[RegistryRow],
    *,
    retry_failures_only: bool,
) -> list[InventoryRow]:
    registry_by_image_id = _registry_by_image_id(registry_rows)
    candidates: list[InventoryRow] = []
    for row in inventory_rows:
        registry_row = registry_by_image_id.get(row.image_id)
        if registry_row is None:
            if not retry_failures_only:
                candidates.append(row)
            continue
        if retry_failures_only:
            if registry_row.status != "success":
                candidates.append(row)
            continue
        if registry_row.status == "success" and registry_row.source_path != row.path:
            candidates.append(row)
    return candidates


def discover_candidate_rows(
    *,
    warehouse_id: str,
    retry_failures_only: bool,
    limit: int | None = None,
) -> list[InventoryRow]:
    inventory_rows = _dedupe_inventory_rows(_fetch_inventory_rows(warehouse_id))
    registry_rows = _fetch_registry_rows(warehouse_id)
    candidates = _select_candidate_rows(
        inventory_rows,
        registry_rows,
        retry_failures_only=retry_failures_only,
    )
    if limit is not None:
        candidates = candidates[:limit]
    return candidates


def _successful_registry_for_inventory(
    inventory_rows: list[InventoryRow],
    registry_rows: list[RegistryRow],
) -> list[RegistryRow]:
    inventory_by_image_id = {row.image_id: row for row in inventory_rows}
    successful_rows: list[RegistryRow] = []
    for row in registry_rows:
        inventory_row = inventory_by_image_id.get(row.image_id)
        if inventory_row is None:
            continue
        if row.status != "success":
            continue
        if row.source_path != inventory_row.path:
            continue
        successful_rows.append(row)
    return successful_rows


def _build_manifest_from_registry(
    registry_rows: list[RegistryRow],
    *,
    master_size: int,
    manifest_version: str,
) -> dict[str, Any]:
    slides: dict[str, Any] = {}
    for row in registry_rows:
        slides[row.image_id] = {
            "uri": row.artifact_uri,
            "width": row.width,
            "height": row.height,
            "content_type": row.content_type or "image/jpeg",
            "source_uri": row.source_path,
        }
    return {
        "version": 1,
        "manifest_version": manifest_version,
        "generated_at": datetime.now(UTC).isoformat(),
        "master_size": master_size,
        "slides": slides,
    }


def _staged_manifest_uri(manifest_uri: str, manifest_version: str) -> str:
    return f"{manifest_uri}.staged.{manifest_version}"


def _publish_manifest(manifest_uri: str, manifest: dict[str, Any], manifest_version: str) -> str:
    staged_uri = _staged_manifest_uri(manifest_uri, manifest_version)
    payload = json.dumps(manifest, indent=2, sort_keys=True).encode()
    _write_bytes(staged_uri, payload)
    _read_json(staged_uri)
    _copy_uri(staged_uri, manifest_uri)
    return staged_uri


def _batched(values: list[dict[str, Any]], batch_size: int) -> list[list[dict[str, Any]]]:
    return [values[index : index + batch_size] for index in range(0, len(values), batch_size)]


def _registry_select_row_sql(row: dict[str, Any]) -> str:
    rendered_at = str(row["rendered_at"])
    return f"""
SELECT
    {_sql_string(str(row["image_id"]))} AS image_id,
    {_sql_string(str(row["source_path"]))} AS source_path,
    {_sql_string(str(row["artifact_uri"]))} AS artifact_uri,
    {_sql_nullable_int(row.get("width"))} AS width,
    {_sql_nullable_int(row.get("height"))} AS height,
    {_sql_string(str(row.get("content_type") or "image/jpeg"))} AS content_type,
    {_sql_string(str(row["status"]))} AS status,
    CAST({_sql_string(rendered_at)} AS TIMESTAMP) AS rendered_at,
    {_sql_nullable_string(row.get("error_message"))} AS error_message,
    {_sql_string(str(row["manifest_version"]))} AS manifest_version
"""


def _upsert_registry_rows(warehouse_id: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    for batch in _batched(rows, REGISTRY_UPSERT_BATCH_SIZE):
        source_sql = "\nUNION ALL\n".join(_registry_select_row_sql(row) for row in batch)
        merge_sql = f"""
MERGE INTO {THUMBNAIL_REGISTRY_TABLE} AS target
USING (
{source_sql}
) AS source
ON target.image_id = source.image_id
WHEN MATCHED THEN UPDATE SET
    target.source_path = source.source_path,
    target.artifact_uri = source.artifact_uri,
    target.width = source.width,
    target.height = source.height,
    target.content_type = source.content_type,
    target.status = source.status,
    target.rendered_at = source.rendered_at,
    target.error_message = source.error_message,
    target.manifest_version = source.manifest_version
WHEN NOT MATCHED THEN INSERT (
    image_id,
    source_path,
    artifact_uri,
    width,
    height,
    content_type,
    status,
    rendered_at,
    error_message,
    manifest_version
) VALUES (
    source.image_id,
    source.source_path,
    source.artifact_uri,
    source.width,
    source.height,
    source.content_type,
    source.status,
    source.rendered_at,
    source.error_message,
    source.manifest_version
)
"""
        run_statement(merge_sql, warehouse_id)


def process_candidate_rows(
    *,
    warehouse_id: str,
    root_uri: str,
    master_size: int,
    rows: list[InventoryRow],
    manifest_version: str,
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    registry_updates: list[dict[str, Any]] = []

    for index, row in enumerate(rows, start=1):
        logger.info("processing %s (%d/%d)", row.image_id, index, len(rows))
        artifact_uri = _join_uri(root_uri, f"{row.image_id}.jpg")
        try:
            artifact = _render_candidate_artifact_subprocess(
                image_id=row.image_id,
                slide_uri=row.path,
                artifact_uri=artifact_uri,
                master_size=master_size,
            )
            registry_updates.append(
                {
                    "image_id": artifact["image_id"],
                    "source_path": artifact["source_path"],
                    "artifact_uri": artifact["artifact_uri"],
                    "width": int(artifact["width"]),
                    "height": int(artifact["height"]),
                    "content_type": artifact["content_type"],
                    "status": "success",
                    "rendered_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
                    "error_message": None,
                    "manifest_version": manifest_version,
                }
            )
        except Exception as exc:
            logger.exception("failed thumbnail generation for %s", row.image_id)
            failures.append(
                {
                    "image_id": row.image_id,
                    "path": row.path,
                    "error": str(exc),
                }
            )
            registry_updates.append(
                {
                    "image_id": row.image_id,
                    "source_path": row.path,
                    "artifact_uri": artifact_uri,
                    "width": None,
                    "height": None,
                    "content_type": "image/jpeg",
                    "status": "failed",
                    "rendered_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
                    "error_message": str(exc),
                    "manifest_version": manifest_version,
                }
            )
        if len(registry_updates) >= REGISTRY_UPSERT_BATCH_SIZE:
            _upsert_registry_rows(warehouse_id, registry_updates)
            registry_updates.clear()

    if registry_updates:
        _upsert_registry_rows(warehouse_id, registry_updates)
    return failures


def publish_manifest_for_current_inventory(
    *,
    warehouse_id: str,
    manifest_uri: str,
    master_size: int,
    manifest_version: str,
) -> dict[str, Any]:
    inventory_rows = _dedupe_inventory_rows(_fetch_inventory_rows(warehouse_id))
    published_registry_rows = _successful_registry_for_inventory(
        inventory_rows,
        _fetch_registry_rows(warehouse_id),
    )
    manifest = _build_manifest_from_registry(
        published_registry_rows,
        master_size=master_size,
        manifest_version=manifest_version,
    )
    _publish_manifest(manifest_uri, manifest, manifest_version)
    return manifest


def run_incremental_pipeline(
    *,
    warehouse_id: str,
    manifest_uri: str,
    root_uri: str,
    master_size: int,
    limit: int | None,
    retry_failures_only: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[InventoryRow]]:
    _ensure_registry_table(warehouse_id)
    candidates = discover_candidate_rows(
        warehouse_id=warehouse_id,
        retry_failures_only=retry_failures_only,
    )
    if limit is not None:
        candidates = candidates[:limit]

    manifest_version = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    failures = process_candidate_rows(
        warehouse_id=warehouse_id,
        root_uri=root_uri,
        master_size=master_size,
        rows=candidates,
        manifest_version=manifest_version,
    )
    manifest = publish_manifest_for_current_inventory(
        warehouse_id=warehouse_id,
        manifest_uri=manifest_uri,
        master_size=master_size,
        manifest_version=manifest_version,
    )
    return manifest, failures, candidates


def _summary_payload(
    *,
    manifest: dict[str, Any],
    failures: list[dict[str, Any]],
    candidates: list[InventoryRow],
) -> dict[str, Any]:
    return {
        "candidate_count": len(candidates),
        "failure_count": len(failures),
        "success_count": len(candidates) - len(failures),
        "manifest_slide_count": len(manifest["slides"]),
        "generated_at": manifest["generated_at"],
        "manifest_version": manifest.get("manifest_version"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--render-single-image-id", default="")
    parser.add_argument("--render-single-source-path", default="")
    parser.add_argument("--render-single-artifact-uri", default="")
    parser.add_argument("--render-single-master-size", type=int, default=0)
    parser.add_argument("--render-single-output-path", default="")
    parser.add_argument(
        "--manifest-uri",
        default=settings.thumbnail_manifest_uri,
        help="Where to publish the JSON manifest.",
    )
    parser.add_argument(
        "--root-uri",
        default="",
        help="Object-store prefix for generated JPEG masters.",
    )
    parser.add_argument(
        "--warehouse-id",
        default=settings.databricks_warehouse_id,
        help="Databricks SQL warehouse id.",
    )
    parser.add_argument(
        "--master-size",
        type=int,
        default=settings.thumbnail_master_size,
        help="Max edge length for generated JPEG masters.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only process the first N candidate slides.",
    )
    parser.add_argument(
        "--failures-path",
        default="",
        help="Optional local JSON file for generation failures.",
    )
    parser.add_argument(
        "--retry-failures-only",
        action="store_true",
        help="Only retry registry rows whose last known status is not success.",
    )
    parser.add_argument(
        "--summary-path",
        default="",
        help="Optional local JSON file for run summary metrics.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

    if args.render_single_output_path:
        payload = _render_candidate_artifact(
            image_id=args.render_single_image_id,
            slide_uri=args.render_single_source_path,
            artifact_uri=args.render_single_artifact_uri,
            master_size=max(1, args.render_single_master_size),
        )
        with open(args.render_single_output_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
        return 0

    if not args.manifest_uri:
        parser.error("--manifest-uri is required")
    if not args.root_uri:
        parser.error("--root-uri is required")

    manifest, failures, candidates = run_incremental_pipeline(
        warehouse_id=args.warehouse_id,
        manifest_uri=args.manifest_uri,
        root_uri=args.root_uri,
        master_size=max(1, args.master_size),
        limit=args.limit,
        retry_failures_only=args.retry_failures_only,
    )
    summary = _summary_payload(
        manifest=manifest,
        failures=failures,
        candidates=candidates,
    )

    if args.failures_path:
        with open(args.failures_path, "w", encoding="utf-8") as handle:
            json.dump(failures, handle, indent=2, sort_keys=True)
    if args.summary_path:
        with open(args.summary_path, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, indent=2, sort_keys=True)

    logger.info(
        "published manifest=%s slides=%d candidates=%d failures=%d",
        args.manifest_uri,
        len(manifest["slides"]),
        len(candidates),
        len(failures),
    )
    if failures:
        logger.warning("completed with failures=%d; failed rows remain in registry for explicit retry mode", len(failures))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
