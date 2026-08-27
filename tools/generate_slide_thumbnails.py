#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
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
from app.constants import SERVING_MANIFEST_TABLE, THUMBNAIL_REGISTRY_TABLE
from app.identity import (
    IDENTITY_VERSION,
    TILE_METADATA_SCHEMA_VERSION,
    decode_policy_version,
    source_fingerprint as canonical_source_fingerprint,
)
from app.meta_store import run_query_external, run_statement
from app.metadata_contract import validate_tile_metadata
from app.slide_store import open_slide, s3_opts
from app.tiles import NoSafeThumbnailOverview, get_thumbnail_bytes_with_plan, slide_metadata

logger = logging.getLogger("thumbnail-generator")

REGISTRY_UPSERT_BATCH_SIZE = 1_000
DEFAULT_SLIDES_PER_TASK = 2_000
MAX_ARRAY_TASKS = 480
TASK_MARKER_VERSION = 1


def source_fingerprint(row: Any) -> str | None:
    """Match the fingerprint contract produced by the audit and PDM SQL."""
    if isinstance(row, InventoryRow):
        path = row.path
        size = row.size
        last_modified = row.last_modified
    else:
        path = str(row.get("path") or row.get("slide_path") or "")
        size = row.get("size", row.get("file_size_bytes", ""))
        last_modified = row.get("last_modified", "")
    return canonical_source_fingerprint(path, size, last_modified)


def _require_source_fingerprint(row: InventoryRow) -> str:
    fingerprint = source_fingerprint(row)
    if fingerprint is None:
        raise ValueError("serving manifest row has incomplete source identity")
    return fingerprint


def _metadata_source_fingerprint(metadata_json: str) -> str | None:
    try:
        value = json.loads(metadata_json).get("source_fingerprint")
    except (TypeError, json.JSONDecodeError):
        return None
    return str(value) if value not in (None, "") else None


def _metadata_policy_version(metadata_json: str) -> str | None:
    try:
        value = json.loads(metadata_json).get("decode_policy_version")
    except (TypeError, json.JSONDecodeError):
        return None
    return str(value) if value not in (None, "") else None


def _metadata_schema_version(metadata_json: str) -> int | None:
    try:
        value = json.loads(metadata_json).get("tile_metadata_schema_version")
        return int(value) if value is not None else None
    except (TypeError, ValueError, json.JSONDecodeError):
        return None

SERVABLE_SLIDES_SQL = """
SELECT
    CAST(image_id AS STRING) AS image_id,
    slide_path AS path,
    serving_size AS size,
    serving_last_modified AS last_modified
FROM {serving_manifest_table}
WHERE image_id IS NOT NULL
  AND slide_path LIKE 's3://%'
  AND certification_status = 'valid'
""".format(serving_manifest_table=SERVING_MANIFEST_TABLE)

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
    size: int | None = None
    last_modified: str | None = None


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
    source_fingerprint: str | None = None


def _task_result_path(run_dir: str | Path, task_index: int) -> Path:
    return Path(run_dir) / "results" / f"task-{task_index:04d}.jsonl"


def _task_marker_path(run_dir: str | Path, task_index: int) -> Path:
    return Path(run_dir) / "results" / f"task-{task_index:04d}.done.json"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_result_records_strict(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid result JSON at {path}:{line_number}") from exc
            if not isinstance(payload, dict) or not payload.get("image_id"):
                raise ValueError(f"invalid result record at {path}:{line_number}")
            records.append(payload)
    return records


def _legacy_summary_path(run_dir: Path, task_index: int) -> Path | None:
    summaries = sorted(
        (run_dir / "logs").glob(f"slide-thumbnail-summary-*-{task_index}.json"),
        key=lambda path: path.stat().st_mtime,
    )
    return summaries[-1] if summaries else None


def _result_counts(records: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"record_count": len(records), "success_count": 0, "failure_count": 0}
    for record in records:
        if record.get("status") == "success":
            counts["success_count"] += 1
        elif record.get("status") == "failed":
            counts["failure_count"] += 1
        else:
            raise ValueError(f"unsupported thumbnail result status: {record.get('status')!r}")
    return counts


def write_task_completion_marker(
    *,
    run_dir: str | Path,
    task_index: int,
    candidate_count: int,
    manifest_version: str,
    result_path: str | Path | None = None,
) -> dict[str, Any]:
    result_file = Path(result_path) if result_path else _task_result_path(run_dir, task_index)
    records = _read_result_records_strict(result_file)
    counts = _result_counts(records)
    if counts["record_count"] != candidate_count:
        raise ValueError(
            f"task {task_index} has {counts['record_count']} results for "
            f"{candidate_count} candidates"
        )
    marker = {
        "marker_version": TASK_MARKER_VERSION,
        "task_index": task_index,
        "candidate_count": candidate_count,
        "manifest_version": manifest_version,
        "result_sha256": _sha256_file(result_file),
        **counts,
    }
    marker_path = _task_marker_path(run_dir, task_index)
    _atomic_write_json(marker_path, marker)
    return marker


def _validate_task(
    *,
    run_dir: Path,
    candidate_dir: Path,
    task_index: int,
    manifest_version: str,
    require_marker: bool,
) -> dict[str, Any]:
    candidate_path = candidate_dir / f"task-{task_index:04d}.jsonl"
    result_path = _task_result_path(run_dir, task_index)
    marker_path = _task_marker_path(run_dir, task_index)
    try:
        candidate_rows = read_candidate_rows(str(candidate_path))
        candidate_ids = [row.image_id for row in candidate_rows]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("duplicate candidate image_id")
        records = _read_result_records_strict(result_path)
        result_ids = [str(record["image_id"]) for record in records]
        if len(result_ids) != len(set(result_ids)):
            raise ValueError("duplicate result image_id")
        if set(candidate_ids) != set(result_ids):
            raise ValueError("candidate and result image_id sets differ")
        candidate_by_id = {row.image_id: row for row in candidate_rows}
        for record in records:
            image_id = str(record["image_id"])
            if str(record.get("source_path") or "") != candidate_by_id[image_id].path:
                raise ValueError(f"source path mismatch for {image_id}")
            if str(record.get("manifest_version") or "") != manifest_version:
                raise ValueError(f"manifest version mismatch for {image_id}")
        counts = _result_counts(records)
        marker = None
        if marker_path.exists():
            with marker_path.open("r", encoding="utf-8") as handle:
                marker = json.load(handle)
            expected_marker = {
                "marker_version": TASK_MARKER_VERSION,
                "task_index": task_index,
                "candidate_count": len(candidate_rows),
                "record_count": counts["record_count"],
                "success_count": counts["success_count"],
                "failure_count": counts["failure_count"],
                "manifest_version": manifest_version,
                "result_sha256": _sha256_file(result_path),
            }
            if marker != expected_marker:
                raise ValueError("completion marker does not match result file")
        elif require_marker:
            raise ValueError("missing completion marker")
        return {
            "task_index": task_index,
            "state": "complete" if marker else "legacy_complete",
            "candidate_count": len(candidate_rows),
            **counts,
            "result_path": str(result_path),
            "marker_path": str(marker_path),
        }
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {
            "task_index": task_index,
            "state": "incomplete",
            "candidate_count": 0,
            "record_count": 0,
            "success_count": 0,
            "failure_count": 0,
            "result_path": str(result_path),
            "marker_path": str(marker_path),
            "reason": str(exc),
        }


def _quarantine_task_files(run_dir: Path, task_index: int) -> str | None:
    paths = [
        _task_result_path(run_dir, task_index),
        _task_marker_path(run_dir, task_index),
        *_task_result_path(run_dir, task_index).parent.glob(
            f"task-{task_index:04d}.jsonl.partial.*"
        ),
    ]
    existing = [path for path in paths if path.exists()]
    if not existing:
        return None
    quarantine_dir = run_dir / "quarantine" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    for path in existing:
        shutil.move(str(path), str(quarantine_dir / path.name))
    return str(quarantine_dir)


def audit_thumbnail_run(
    run_dir: str,
    *,
    adopt_legacy: bool = False,
    quarantine_incomplete: bool = False,
) -> dict[str, Any]:
    run_path = Path(run_dir).resolve()
    with (run_path / "run-meta.json").open("r", encoding="utf-8") as handle:
        meta = json.load(handle)
    task_count = int(meta["task_count"])
    candidate_dir = Path(meta["candidate_dir"])
    if not candidate_dir.is_absolute():
        candidate_dir = run_path / candidate_dir
    manifest_version = str(meta["manifest_version"])
    tasks: list[dict[str, Any]] = []
    legacy_adopted: list[int] = []
    quarantined: dict[str, str] = {}
    for task_index in range(task_count):
        task = _validate_task(
            run_dir=run_path,
            candidate_dir=candidate_dir,
            task_index=task_index,
            manifest_version=manifest_version,
            require_marker=False,
        )
        if task["state"] == "legacy_complete":
            summary_path = _legacy_summary_path(run_path, task_index)
            summary_matches = False
            if summary_path is not None:
                try:
                    with summary_path.open("r", encoding="utf-8") as handle:
                        summary = json.load(handle)
                    summary_matches = (
                        int(summary.get("task_index", -1)) == task_index
                        and int(summary.get("candidate_count", -1)) == task["candidate_count"]
                        and int(summary.get("success_count", -1)) == task["success_count"]
                        and int(summary.get("failure_count", -1)) == task["failure_count"]
                        and str(summary.get("manifest_version")) == manifest_version
                    )
                except (OSError, TypeError, ValueError):
                    summary_matches = False
            if adopt_legacy and summary_matches:
                write_task_completion_marker(
                    run_dir=run_path,
                    task_index=task_index,
                    candidate_count=task["candidate_count"],
                    manifest_version=manifest_version,
                )
                task["state"] = "complete"
                legacy_adopted.append(task_index)
            elif not summary_matches:
                task["state"] = "incomplete"
                task["reason"] = "valid result has no matching legacy summary"
        if task["state"] == "incomplete" and quarantine_incomplete:
            quarantine_dir = _quarantine_task_files(run_path, task_index)
            if quarantine_dir:
                quarantined[str(task_index)] = quarantine_dir
        tasks.append(task)

    complete_tasks = [task for task in tasks if task["state"] in {"complete", "legacy_complete"}]
    incomplete_tasks = [task for task in tasks if task["state"] == "incomplete"]
    marker_complete_tasks = [task for task in tasks if task["state"] == "complete"]
    return {
        "run_dir": str(run_path),
        "manifest_version": manifest_version,
        "worker_job": meta.get("worker_job"),
        "publisher_job": meta.get("publisher_job"),
        "task_count": task_count,
        "completed_task_count": len(complete_tasks),
        "incomplete_task_count": len(incomplete_tasks),
        "completed_task_indexes": [task["task_index"] for task in complete_tasks],
        "incomplete_task_indexes": [task["task_index"] for task in incomplete_tasks],
        "legacy_complete_task_indexes": [
            task["task_index"] for task in complete_tasks if task["state"] == "legacy_complete"
        ],
        "legacy_adopted_task_indexes": legacy_adopted,
        "candidate_count": sum(task["candidate_count"] for task in complete_tasks),
        "record_count": sum(task["record_count"] for task in complete_tasks),
        "success_count": sum(task["success_count"] for task in complete_tasks),
        "failure_count": sum(task["failure_count"] for task in complete_tasks),
        "publishable": not incomplete_tasks and len(marker_complete_tasks) == task_count,
        "quarantined": quarantined,
        "tasks": tasks,
    }


def slurm_array_expression(task_indexes: Iterable[int]) -> str:
    indexes = sorted(set(int(index) for index in task_indexes))
    if not indexes:
        return ""
    ranges: list[str] = []
    start = previous = indexes[0]
    for index in indexes[1:]:
        if index == previous + 1:
            previous = index
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = index
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(ranges)


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
        try:
            metadata = slide_metadata(slide)
        except Exception as exc:
            raise RuntimeError(f"slide metadata extraction failed for {image_id}") from exc
        valid, reason = validate_tile_metadata(metadata, allow_legacy=False)
        if not valid:
            raise RuntimeError(f"invalid v2 slide metadata for {image_id}: {reason}")
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


def _artifact_exists(uri: str) -> bool:
    try:
        return bool(_filesystem_for_uri(uri).exists(_filesystem_path(uri)))
    except Exception:
        return False


def _build_metadata_only_record(
    *,
    image_id: str,
    slide_uri: str,
    existing: RegistryRow,
    source_fingerprint: str,
) -> dict[str, Any]:
    """Upgrade registry metadata while retaining an existing thumbnail object."""
    if (
        existing.status != "success"
        or existing.source_path != slide_uri
        or not existing.artifact_uri
        or not _artifact_exists(existing.artifact_uri)
        or existing.width <= 0
        or existing.height <= 0
    ):
        raise RuntimeError(f"existing thumbnail artifact is not reusable for {image_id}")
    with _without_blockcache():
        slide, fileobj = open_slide(slide_uri, logger)
    try:
        metadata = slide_metadata(slide)
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
    valid, reason = validate_tile_metadata(metadata, allow_legacy=False)
    if not valid:
        raise RuntimeError(f"invalid v2 slide metadata for {image_id}: {reason}")
    metadata["source_fingerprint"] = source_fingerprint
    return {
        "image_id": image_id,
        "source_path": slide_uri,
        "artifact_uri": existing.artifact_uri,
        "width": existing.width,
        "height": existing.height,
        "content_type": existing.content_type or "image/jpeg",
        "level": None,
        "requested_pixels": None,
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
    return [
        InventoryRow(
            image_id=str(row["image_id"]),
            path=str(row["path"]),
            size=int(row["size"]) if row.get("size") not in (None, "") else None,
            last_modified=(
                str(row["last_modified"])
                if row.get("last_modified") not in (None, "")
                else None
            ),
        )
        for row in rows
    ]
def _validate_inventory_identity(rows: Iterable[InventoryRow]) -> None:
    invalid_count = 0
    for row in rows:
        try:
            if source_fingerprint(row) is None:
                invalid_count += 1
        except (TypeError, ValueError, OverflowError):
            invalid_count += 1
    if invalid_count:
        raise ValueError(
            "serving manifest contains "
            f"{invalid_count} rows with incomplete or invalid source identity"
        )


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
            source_fingerprint=_metadata_source_fingerprint(str(row.get("tile_metadata_json") or "")),
        )
        for row in rows
    ]


def _fetch_inventory_rows(warehouse_id: str) -> list[InventoryRow]:
    rows = _normalize_inventory_rows(run_query_external(SERVABLE_SLIDES_SQL, warehouse_id))
    _validate_inventory_identity(rows)
    return rows


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


def _registry_by_image_id_and_source(rows: list[RegistryRow]) -> dict[tuple[str, str], RegistryRow]:
    by_image_and_source: dict[tuple[str, str], RegistryRow] = {}
    for row in rows:
        key = (row.image_id, row.source_path)
        existing = by_image_and_source.get(key)
        if existing is None or (row.rendered_at, row.manifest_version) > (
            existing.rendered_at,
            existing.manifest_version,
        ):
            by_image_and_source[key] = row
    return by_image_and_source


def _select_candidate_rows(
    inventory_rows: list[InventoryRow],
    registry_rows: list[RegistryRow],
    *,
    retry_failures_only: bool,
) -> list[InventoryRow]:
    _validate_inventory_identity(inventory_rows)
    registry_by_image_and_source = _registry_by_image_id_and_source(registry_rows)
    candidates: list[InventoryRow] = []
    for row in inventory_rows:
        registry_row = registry_by_image_and_source.get((row.image_id, row.path))
        if registry_row is None:
            if not retry_failures_only:
                candidates.append(row)
            continue
        # A successful row written before tile_metadata_json was introduced
        # is not a complete source-bound record. Regenerate it so migration
        # does not make an otherwise servable slide unavailable.
        metadata_missing = not (registry_row.tile_metadata_json or "").strip()
        policy_stale = (
            _metadata_policy_version(registry_row.tile_metadata_json or "")
            != decode_policy_version()
        )
        schema_stale = (
            _metadata_schema_version(registry_row.tile_metadata_json or "")
            != TILE_METADATA_SCHEMA_VERSION
        )
        current_fingerprint = _require_source_fingerprint(row)
        needs_regeneration = (
            registry_row.source_fingerprint != current_fingerprint
            or metadata_missing
            or policy_stale
            or schema_stale
        )
        if retry_failures_only:
            if registry_row.status != "success" or needs_regeneration:
                candidates.append(row)
        elif registry_row.status == "success" and needs_regeneration:
            candidates.append(row)
    return candidates


def discover_candidate_rows(
    *,
    warehouse_id: str,
    retry_failures_only: bool,
    limit: int | None = None,
    registry_rows: list[RegistryRow] | None = None,
) -> list[InventoryRow]:
    inventory_rows = _dedupe_inventory_rows(_fetch_inventory_rows(warehouse_id))
    if registry_rows is None:
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
            handle.write(
                json.dumps(
                    {
                        "image_id": row.image_id,
                        "path": row.path,
                        "size": row.size,
                        "last_modified": row.last_modified,
                    },
                    sort_keys=True,
                )
            )
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
                    size=int(payload["size"]) if payload.get("size") not in (None, "") else None,
                    last_modified=(
                        str(payload["last_modified"])
                        if payload.get("last_modified") not in (None, "")
                        else None
                    ),
                )
            )
    return rows


def iter_candidate_rows(path: str) -> Iterable[InventoryRow]:
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            payload = json.loads(line)
            yield InventoryRow(
                image_id=str(payload["image_id"]),
                path=str(payload["path"]),
                size=int(payload["size"]) if payload.get("size") not in (None, "") else None,
                last_modified=(
                    str(payload["last_modified"])
                    if payload.get("last_modified") not in (None, "")
                    else None
                ),
            )


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
                json.dumps(
                    {
                        "image_id": row.image_id,
                        "path": row.path,
                        "size": row.size,
                        "last_modified": row.last_modified,
                    },
                    sort_keys=True,
                )
                + "\n"
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
    registry_rows: Iterable[RegistryRow] = (),
) -> list[dict[str, Any]]:
    del warehouse_id  # Registry writes are intentionally reserved for the finalizer.
    registry_by_image_and_source = _registry_by_image_id_and_source(list(registry_rows))
    failures: list[dict[str, Any]] = []
    result_handle = None
    if result_path:
        result_file = Path(result_path)
        result_file.parent.mkdir(parents=True, exist_ok=True)
        result_handle = result_file.open("w", encoding="utf-8")

    try:
        for index, row in enumerate(rows, start=1):
            logger.info("processing thumbnail candidate index=%d", index)
            current_fingerprint = _require_source_fingerprint(row)
            artifact_uri = _join_uri(root_uri, f"{row.image_id}.jpg")
            try:
                existing = registry_by_image_and_source.get((row.image_id, row.path))
                if (
                    existing is not None
                    and existing.source_fingerprint in (None, current_fingerprint)
                    and _artifact_exists(existing.artifact_uri)
                ):
                    artifact = _build_metadata_only_record(
                        image_id=row.image_id,
                        slide_uri=row.path,
                        existing=existing,
                        source_fingerprint=current_fingerprint,
                    )
                else:
                    artifact = _render_candidate_artifact_subprocess(
                        image_id=row.image_id,
                        slide_uri=row.path,
                        artifact_uri=artifact_uri,
                        master_size=master_size,
                        timeout_sec=timeout_sec,
                    )
                metadata = json.loads(artifact.get("tile_metadata_json") or "{}")
                valid, reason = validate_tile_metadata(metadata, allow_legacy=False)
                if not valid:
                    raise RuntimeError(f"invalid thumbnail metadata for {row.image_id}: {reason}")
                metadata["source_fingerprint"] = current_fingerprint
                artifact["tile_metadata_json"] = json.dumps(metadata, separators=(",", ":"))
                _append_result(
                    result_handle,
                    {
                        "image_id": artifact["image_id"],
                        "source_path": artifact["source_path"],
                        "source_fingerprint": current_fingerprint,
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
                        "source_fingerprint": current_fingerprint,
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
AND target.source_path = source.source_path
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
    _validate_inventory_identity(inventory_rows)
    inventory_by_image_and_source = {
        (row.image_id, row.path): row for row in inventory_rows
    }
    registry_by_image_and_source = _registry_by_image_id_and_source(registry_rows)
    complete: list[RegistryRow] = []
    for key, inventory_row in inventory_by_image_and_source.items():
        row = registry_by_image_and_source.get(key)
        if row is None or row.status != "success":
            continue
        current_fingerprint = _require_source_fingerprint(inventory_row)
        schema = _metadata_schema_version(row.tile_metadata_json)
        if row.source_fingerprint not in (None, current_fingerprint):
            continue
        if not (row.tile_metadata_json or "").strip():
            continue
        try:
            metadata = json.loads(row.tile_metadata_json)
        except (TypeError, json.JSONDecodeError):
            continue
        valid, _ = validate_tile_metadata(metadata, allow_legacy=True)
        if not valid:
            continue
        if schema == TILE_METADATA_SCHEMA_VERSION and _metadata_policy_version(row.tile_metadata_json) != decode_policy_version():
            continue
        complete.append(row)
    return complete


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
            "source_fingerprint": row.source_fingerprint,
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
    registry_rows = _fetch_registry_rows(warehouse_id)
    candidates = discover_candidate_rows(
        warehouse_id=warehouse_id,
        retry_failures_only=retry_failures_only,
        limit=limit,
        registry_rows=registry_rows,
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
            registry_rows=registry_rows,
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
