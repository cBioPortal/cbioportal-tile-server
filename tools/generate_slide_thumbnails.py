#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable

import fsspec
from PIL import Image

from app.config import settings
from app.constants import INVENTORY_TABLE, THUMBNAIL_REGISTRY_TABLE
from app.meta_store import run_query_external, run_statement
from app.slide_store import open_slide, s3_opts
from app.tiles import NoSafeThumbnailOverview, get_thumbnail_bytes_with_plan, slide_metadata

logger = logging.getLogger("thumbnail-generator")

REGISTRY_UPSERT_BATCH_SIZE = 1_000
DEFAULT_SLIDES_PER_TASK = 2_000
MAX_ARRAY_TASKS = 480

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
    tile_metadata_json STRING,
    status STRING,
    rendered_at TIMESTAMP,
    error_message STRING,
    manifest_version STRING
)
USING DELTA
""".format(registry_table=THUMBNAIL_REGISTRY_TABLE)

REGISTRY_MIGRATE_SQL = """
ALTER TABLE {registry_table}
ADD COLUMNS (tile_metadata_json STRING)
""".format(registry_table=THUMBNAIL_REGISTRY_TABLE)

REGISTRY_SELECT_SQL = """
SELECT
    image_id,
    source_path,
    artifact_uri,
    width,
    height,
    content_type,
    tile_metadata_json,
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
    tile_metadata_json: str = ""


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
    return "NULL" if value is None else _sql_string(value)


def _sql_nullable_int(value: int | None) -> str:
    return "NULL" if value is None else str(value)


@contextmanager
def _without_blockcache():
    original = settings.blockcache_path
    settings.blockcache_path = ""
    try:
        yield
    finally:
        settings.blockcache_path = original


def _build_thumbnail_record(image_id: str, slide_uri: str, master_size: int) -> dict[str, Any]:
    with _without_blockcache():
        slide, fileobj = open_slide(slide_uri, logger)
    try:
        # A real OpenSlide object always exposes the intrinsic pyramid.  Keep
        # the renderer's artifact path usable for legacy/test doubles that do
        # not implement those fields; the core importer/backend will fail closed when
        # the resulting metadata JSON is not a valid tile contract.
        try:
            metadata = slide_metadata(slide)
        except (AttributeError, TypeError, ValueError):
            metadata = {}
        try:
            thumb_bytes, plan = get_thumbnail_bytes_with_plan(slide, master_size, master_size)
        except NoSafeThumbnailOverview:
            # Offline generation may use the heavier fallback, but only in the
            # isolated renderer process and never in the API process.
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
        "tile_metadata_json": json.dumps(metadata, separators=(",", ":")),
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
        "tile_metadata_json": artifact["tile_metadata_json"],
    }


def _thumbnail_temp_dir() -> str | None:
    configured = os.environ.get("THUMBNAIL_TMPDIR")
    if not configured:
        return None
    Path(configured).mkdir(parents=True, exist_ok=True)
    return configured


def _render_candidate_artifact_subprocess(
    *,
    image_id: str,
    slide_uri: str,
    artifact_uri: str,
    master_size: int,
    timeout_sec: int | None = None,
) -> dict[str, Any]:
    with tempfile.NamedTemporaryFile(
        prefix=f"slide-thumbnail-{image_id}-",
        suffix=".json",
        delete=False,
        dir=_thumbnail_temp_dir(),
    ) as handle:
        output_path = handle.name

    command = [
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
    ]
    process = subprocess.Popen(
        command,
        env=os.environ.copy(),
        start_new_session=True,
    )
    limit = max(1, timeout_sec or settings.thumbnail_batch_timeout_sec)
    try:
        try:
            return_code = process.wait(timeout=limit)
        except subprocess.TimeoutExpired as exc:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            raise TimeoutError(f"thumbnail render timed out after {limit}s for {image_id}") from exc
        if return_code != 0:
            raise subprocess.CalledProcessError(return_code, command)
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
    return [
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
            tile_metadata_json=str(row.get("tile_metadata_json") or ""),
        )
        for row in rows
    ]


def _fetch_inventory_rows(warehouse_id: str) -> list[InventoryRow]:
    return _normalize_inventory_rows(run_query_external(SERVABLE_SLIDES_SQL, warehouse_id))


def _fetch_registry_rows(warehouse_id: str) -> list[RegistryRow]:
    return _normalize_registry_rows(run_query_external(REGISTRY_SELECT_SQL, warehouse_id))


def _ensure_registry_table(warehouse_id: str) -> None:
    run_statement(REGISTRY_CREATE_SQL, warehouse_id)
    # Existing registries predate the source-bound contract.  Delta accepts
    # this idempotent schema extension, allowing an in-place rollout without
    # rewriting the generated thumbnail artifacts.
    try:
        run_statement(REGISTRY_MIGRATE_SQL, warehouse_id)
    except Exception as exc:
        # CREATE TABLE already includes the column for new installations; an
        # "already exists" response is therefore harmless.
        if "already exists" not in str(exc).lower():
            raise


def _dedupe_inventory_rows(rows: list[InventoryRow]) -> list[InventoryRow]:
    deduped: list[InventoryRow] = []
    seen: set[str] = set()
    for row in rows:
        if row.image_id in seen:
            continue
        seen.add(row.image_id)
        deduped.append(row)
    return deduped


def _registry_by_image_id(rows: list[RegistryRow]) -> dict[str, RegistryRow]:
    by_image_id: dict[str, RegistryRow] = {}
    for row in rows:
        existing = by_image_id.get(row.image_id)
        if existing is None:
            by_image_id[row.image_id] = row
            continue
        existing_priority = 0 if existing.status == "success" else 1
        row_priority = 0 if row.status == "success" else 1
        if row_priority < existing_priority or (
            row_priority == existing_priority and row.rendered_at > existing.rendered_at
        ):
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
        # A successful row written before tile_metadata_json was introduced
        # is not a complete source-bound record. Regenerate it so migration
        # does not make an otherwise servable slide unavailable.
        metadata_missing = not (registry_row.tile_metadata_json or "").strip()
        if retry_failures_only:
            if registry_row.status != "success" or metadata_missing:
                candidates.append(row)
            continue
        if registry_row.status == "success" and (
            registry_row.source_path != row.path or metadata_missing
        ):
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
    return candidates if limit is None else candidates[:limit]


def write_candidate_rows(path: str, rows: Iterable[InventoryRow]) -> None:
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
            rows.append(InventoryRow(image_id=str(payload["image_id"]), path=str(payload["path"])))
    return rows


def iter_candidate_rows(path: str) -> Iterable[InventoryRow]:
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            payload = json.loads(line)
            yield InventoryRow(image_id=str(payload["image_id"]), path=str(payload["path"]))


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


def write_candidate_shards(
    directory: str,
    rows: list[InventoryRow],
    *,
    slides_per_task: int = DEFAULT_SLIDES_PER_TASK,
    max_tasks: int = MAX_ARRAY_TASKS,
) -> int:
    if slides_per_task <= 0 or max_tasks <= 0:
        raise ValueError("slides_per_task and max_tasks must be positive")
    destination = Path(directory)
    destination.mkdir(parents=True, exist_ok=True)
    if not rows:
        return 0

    task_count = min(max_tasks, (len(rows) + slides_per_task - 1) // slides_per_task)
    rows_per_task = (len(rows) + task_count - 1) // task_count
    handles = [
        (destination / f"task-{task_index:04d}.jsonl").open("w", encoding="utf-8")
        for task_index in range(task_count)
    ]
    try:
        for index, row in enumerate(rows):
            task_index = min(index // rows_per_task, task_count - 1)
            handles[task_index].write(
                json.dumps({"image_id": row.image_id, "path": row.path}, sort_keys=True) + "\n"
            )
    finally:
        for handle in handles:
            handle.close()
    return task_count


def _rendered_at() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _append_result(handle, record: dict[str, Any]) -> None:
    if handle is None:
        return
    handle.write(json.dumps(record, sort_keys=True) + "\n")
    handle.flush()


def process_candidate_rows(
    *,
    warehouse_id: str,
    root_uri: str,
    master_size: int,
    rows: Iterable[InventoryRow],
    manifest_version: str,
    result_path: str | None = None,
    timeout_sec: int | None = None,
) -> list[dict[str, Any]]:
    del warehouse_id  # Registry writes are intentionally reserved for the finalizer.
    failures: list[dict[str, Any]] = []
    result_handle = None
    if result_path:
        result_file = Path(result_path)
        result_file.parent.mkdir(parents=True, exist_ok=True)
        result_handle = result_file.open("a", encoding="utf-8")

    try:
        for index, row in enumerate(rows, start=1):
            logger.info("processing thumbnail candidate index=%d", index)
            artifact_uri = _join_uri(root_uri, f"{row.image_id}.jpg")
            try:
                artifact = _render_candidate_artifact_subprocess(
                    image_id=row.image_id,
                    slide_uri=row.path,
                    artifact_uri=artifact_uri,
                    master_size=master_size,
                    timeout_sec=timeout_sec,
                )
                _append_result(
                    result_handle,
                    {
                        "image_id": artifact["image_id"],
                        "source_path": artifact["source_path"],
                        "artifact_uri": artifact["artifact_uri"],
                        "width": int(artifact["width"]),
                        "height": int(artifact["height"]),
                        "content_type": artifact["content_type"],
                        "status": "success",
                        "rendered_at": _rendered_at(),
                        "error_message": None,
                        "manifest_version": manifest_version,
                        "tile_metadata_json": artifact.get("tile_metadata_json") or "",
                    },
                )
            except Exception as exc:
                logger.error(
                    "thumbnail generation failed; error_type=%s",
                    type(exc).__name__,
                )
                failure = {"image_id": row.image_id, "path": row.path, "error": str(exc)}
                failures.append(failure)
                _append_result(
                    result_handle,
                    {
                        "image_id": row.image_id,
                        "source_path": row.path,
                        "artifact_uri": artifact_uri,
                        "width": None,
                        "height": None,
                        "content_type": "image/jpeg",
                        "status": "failed",
                        "rendered_at": _rendered_at(),
                        "error_message": str(exc),
                        "manifest_version": manifest_version,
                        "tile_metadata_json": "",
                    },
                )
    finally:
        if result_handle is not None:
            result_handle.close()
    return failures


def _iter_result_records(paths: Iterable[str]) -> Iterable[dict[str, Any]]:
    for path in sorted(paths):
        with Path(path).open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("ignoring incomplete result line=%d", line_number)
                    continue
                if not isinstance(payload, dict) or not payload.get("image_id"):
                    logger.warning("ignoring invalid result record line=%d", line_number)
                    continue
                yield payload


def _batched(values: list[dict[str, Any]], batch_size: int) -> Iterable[list[dict[str, Any]]]:
    for index in range(0, len(values), batch_size):
        yield values[index : index + batch_size]


def _registry_select_row_sql(row: dict[str, Any]) -> str:
    return f"""
SELECT
    {_sql_string(str(row["image_id"]))} AS image_id,
    {_sql_string(str(row["source_path"]))} AS source_path,
    {_sql_string(str(row["artifact_uri"]))} AS artifact_uri,
    {_sql_nullable_int(row.get("width"))} AS width,
    {_sql_nullable_int(row.get("height"))} AS height,
    {_sql_string(str(row.get("content_type") or "image/jpeg"))} AS content_type,
    {_sql_string(str(row.get("tile_metadata_json") or ""))} AS tile_metadata_json,
    {_sql_string(str(row["status"]))} AS status,
    CAST({_sql_string(str(row["rendered_at"]))} AS TIMESTAMP) AS rendered_at,
    {_sql_nullable_string(row.get("error_message"))} AS error_message,
    {_sql_string(str(row["manifest_version"]))} AS manifest_version
"""


def _upsert_registry_rows(warehouse_id: str, rows: list[dict[str, Any]]) -> None:
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
    target.tile_metadata_json = source.tile_metadata_json,
    target.status = source.status,
    target.rendered_at = source.rendered_at,
    target.error_message = source.error_message,
    target.manifest_version = source.manifest_version
WHEN NOT MATCHED THEN INSERT (
    image_id, source_path, artifact_uri, width, height, content_type, tile_metadata_json,
    status, rendered_at, error_message, manifest_version
) VALUES (
    source.image_id, source.source_path, source.artifact_uri, source.width,
    source.height, source.content_type, source.tile_metadata_json, source.status, source.rendered_at,
    source.error_message, source.manifest_version
)
"""
        run_statement(merge_sql, warehouse_id)


def publish_registry_results(warehouse_id: str, result_paths: Iterable[str]) -> dict[str, int]:
    batch: list[dict[str, Any]] = []
    stats = {"success_count": 0, "failure_count": 0, "record_count": 0}
    for record in _iter_result_records(result_paths):
        batch.append(record)
        stats["record_count"] += 1
        if record.get("status") == "success":
            stats["success_count"] += 1
        else:
            stats["failure_count"] += 1
        if len(batch) >= REGISTRY_UPSERT_BATCH_SIZE:
            _upsert_registry_batch_with_retries(warehouse_id, batch)
            batch.clear()
    if batch:
        _upsert_registry_batch_with_retries(warehouse_id, batch)
    return stats


def cleanup_run_artifacts(run_dir: str) -> None:
    run_path = Path(run_dir).resolve()
    if run_path == Path("/") or run_path.name == "":
        raise ValueError("refusing to clean an unsafe run directory")
    for name in ("candidates", "results", "tmp", "blockcache"):
        shutil.rmtree(run_path / name, ignore_errors=True)


def _upsert_registry_batch_with_retries(
    warehouse_id: str,
    batch: list[dict[str, Any]],
    *,
    attempts: int = 3,
) -> None:
    for attempt in range(1, attempts + 1):
        try:
            _upsert_registry_rows(warehouse_id, batch)
            return
        except Exception as exc:
            if attempt == attempts:
                raise
            delay = 2 ** (attempt - 1)
            logger.warning(
                "registry update failed; retrying in %ss error_type=%s",
                delay,
                type(exc).__name__,
            )
            time.sleep(delay)


def _successful_registry_for_inventory(
    inventory_rows: list[InventoryRow],
    registry_rows: list[RegistryRow],
) -> list[RegistryRow]:
    inventory_by_image_id = {row.image_id: row for row in inventory_rows}
    return [
        row
        for row in registry_rows
        if row.status == "success"
        and row.image_id in inventory_by_image_id
        and row.source_path == inventory_by_image_id[row.image_id].path
    ]


def _build_manifest_from_registry(
    registry_rows: list[RegistryRow],
    *,
    master_size: int,
    manifest_version: str,
) -> dict[str, Any]:
    slides = {
        row.image_id: {
            "uri": row.artifact_uri,
            "width": row.width,
            "height": row.height,
            "content_type": row.content_type or "image/jpeg",
            "source_uri": row.source_path,
        }
        for row in registry_rows
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
        limit=limit,
    )
    manifest_version = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    with tempfile.TemporaryDirectory(
        prefix="thumbnail-results-",
        dir=_thumbnail_temp_dir(),
    ) as result_dir:
        result_path = str(Path(result_dir) / "results.jsonl")
        failures = process_candidate_rows(
            warehouse_id=warehouse_id,
            root_uri=root_uri,
            master_size=master_size,
            rows=candidates,
            manifest_version=manifest_version,
            result_path=result_path,
        )
        publish_registry_results(warehouse_id, [result_path])
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
    parser.add_argument("--manifest-uri", default=settings.thumbnail_manifest_uri)
    parser.add_argument("--root-uri", default="")
    parser.add_argument("--warehouse-id", default=settings.databricks_warehouse_id)
    parser.add_argument("--master-size", type=int, default=settings.thumbnail_master_size)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--failures-path", default="")
    parser.add_argument("--retry-failures-only", action="store_true")
    parser.add_argument("--summary-path", default="")
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
    summary = _summary_payload(manifest=manifest, failures=failures, candidates=candidates)
    if args.failures_path:
        with open(args.failures_path, "w", encoding="utf-8") as handle:
            json.dump(failures, handle, indent=2, sort_keys=True)
    if args.summary_path:
        with open(args.summary_path, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, indent=2, sort_keys=True)
    logger.info(
        "published thumbnail manifest slides=%d candidates=%d failures=%d",
        len(manifest["slides"]),
        len(candidates),
        len(failures),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
