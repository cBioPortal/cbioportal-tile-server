from __future__ import annotations

from io import BytesIO
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from app.tiles import NoSafeThumbnailOverview
from tools import generate_slide_thumbnails as module


def _jpeg_size(payload: bytes) -> tuple[int, int]:
    image = Image.open(BytesIO(payload))
    return image.size


def test_thumbnail_candidates_follow_effective_serving_manifest():
    assert "wsi_serving_manifest" in module.SERVABLE_SLIDES_SQL
    assert "slide_path AS path" in module.SERVABLE_SLIDES_SQL
    assert "serving_size AS size" in module.SERVABLE_SLIDES_SQL
    assert "certification_status = 'valid'" in module.SERVABLE_SLIDES_SQL


def _result_record(image_id: str, path: str, *, status: str = "success") -> dict:
    return {
        "image_id": image_id,
        "source_path": path,
        "artifact_uri": f"s3://thumbs/{image_id}.jpg",
        "width": 1024 if status == "success" else None,
        "height": 768 if status == "success" else None,
        "content_type": "image/jpeg",
        "status": status,
        "rendered_at": "2026-08-06 12:00:00",
        "error_message": None if status == "success" else "render failed",
        "manifest_version": "v1",
    }


def _inventory(
    image_id: str,
    path: str,
    *,
    size: int = 100,
    last_modified: str = "2026-08-05T00:00:00Z",
) -> module.InventoryRow:
    return module.InventoryRow(
        image_id=image_id,
        path=path,
        size=size,
        last_modified=last_modified,
    )


def _tile_metadata(row: module.InventoryRow, *, policy_version: str | None = None) -> str:
    return json.dumps(
        {
            "dimensions": {"width": 4096, "height": 4096},
            "levels": 1,
            "level_dimensions": [{"width": 4096, "height": 4096}],
            "level_downsamples": [1.0],
            "max_zoom": 4,
            "tile_size": 256,
            "safe_min_level": 1,
            "identity_version": module.IDENTITY_VERSION,
            "tile_metadata_schema_version": module.TILE_METADATA_SCHEMA_VERSION,
            "source_fingerprint": module.source_fingerprint(row),
            "decode_policy_version": policy_version or module.decode_policy_version(),
            "max_decode_pixels": module.settings.max_decode_pixels,
            "thumbnail_max_decode_pixels": module.settings.thumbnail_max_decode_pixels,
        }
    )


def _run_fixture(tmp_path: Path, records: list[dict]) -> tuple[Path, Path]:
    run_dir = tmp_path / "run"
    candidate_dir = run_dir / "candidates"
    result_dir = run_dir / "results"
    (run_dir / "logs").mkdir(parents=True)
    candidate_dir.mkdir()
    result_dir.mkdir()
    rows = [{"image_id": record["image_id"], "path": record["source_path"]} for record in records]
    (candidate_dir / "task-0000.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    (result_dir / "task-0000.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    (run_dir / "run-meta.json").write_text(
        json.dumps(
            {
                "candidate_dir": str(candidate_dir),
                "manifest_version": "v1",
                "task_count": 1,
            }
        ),
        encoding="utf-8",
    )
    return run_dir, result_dir / "task-0000.jsonl"


class TestBuildThumbnailRecord:
    def test_disables_blockcache_for_offline_generation(self):
        slide = MagicMock()
        slide.dimensions = (800, 600)
        slide.level_count = 1
        slide.level_dimensions = [(800, 600)]
        slide.level_downsamples = [1.0]
        slide.properties = {}
        slide.get_thumbnail.return_value = Image.new("RGB", (800, 600), (120, 120, 120))
        fileobj = MagicMock()
        original_blockcache = module.settings.blockcache_path
        module.settings.blockcache_path = "/gpfs/cache-path"

        def _open_slide(slide_uri: str, logger):
            assert module.settings.blockcache_path == ""
            return slide, fileobj

        try:
            with (
                patch.object(module, "open_slide", side_effect=_open_slide),
                patch.object(
                    module,
                    "get_thumbnail_bytes_with_plan",
                    side_effect=NoSafeThumbnailOverview(
                        level=0,
                        level_width=10_000,
                        level_height=10_000,
                        requested_pixels=100_000_000,
                    ),
                ),
            ):
                module._build_thumbnail_record("1492807", "s3://bucket/1492807.svs", 1024)
        finally:
            module.settings.blockcache_path = original_blockcache

    def test_falls_back_to_unbounded_thumbnail_for_offline_generation(self):
        slide = MagicMock()
        slide.dimensions = (800, 600)
        slide.level_count = 1
        slide.level_dimensions = [(800, 600)]
        slide.level_downsamples = [1.0]
        slide.properties = {}
        slide.get_thumbnail.return_value = Image.new("RGB", (800, 600), (120, 120, 120))
        fileobj = MagicMock()

        with (
            patch.object(module, "open_slide", return_value=(slide, fileobj)),
            patch.object(
                module,
                "get_thumbnail_bytes_with_plan",
                side_effect=NoSafeThumbnailOverview(
                    level=0,
                    level_width=10_000,
                    level_height=10_000,
                    requested_pixels=100_000_000,
                ),
            ),
        ):
            record = module._build_thumbnail_record("1492807", "s3://bucket/1492807.svs", 1024)

        assert _jpeg_size(record["bytes"]) == (800, 600)
        assert record["level"] is None
        assert record["requested_pixels"] is None


class TestBatchSafety:
    def test_renderer_timeout_kills_process_group(self, monkeypatch):
        class FakeProcess:
            pid = 123

            def __init__(self):
                self.wait_calls = 0

            def wait(self, timeout=None):
                self.wait_calls += 1
                if self.wait_calls == 1:
                    raise subprocess.TimeoutExpired("thumbnail", timeout)
                return -9

        process = FakeProcess()
        with (
            patch.object(module.subprocess, "Popen", return_value=process),
            patch.object(module.os, "killpg") as killpg,
            patch.object(module, "settings") as settings,
        ):
            settings.thumbnail_batch_timeout_sec = 600
            with pytest.raises(TimeoutError, match="1492807"):
                module._render_candidate_artifact_subprocess(
                    image_id="1492807",
                    slide_uri="s3://bucket/1492807.svs",
                    artifact_uri="s3://thumbs/1492807.jpg",
                    master_size=1024,
                    timeout_sec=1,
                )

        killpg.assert_called_once_with(123, module.signal.SIGKILL)

    def test_candidate_shards_are_bounded(self, tmp_path):
        rows = [module.InventoryRow(str(index), f"s3://bucket/{index}.svs") for index in range(11)]

        task_count = module.write_candidate_shards(
            str(tmp_path), rows, slides_per_task=3, max_tasks=4
        )

        assert task_count == 4
        assert sum(1 for _ in tmp_path.glob("task-*.jsonl")) == 4
        assert sum(1 for path in tmp_path.glob("task-*.jsonl") for _ in module.iter_candidate_rows(str(path))) == 11

    def test_result_processing_overwrites_retried_task(self, tmp_path):
        rows = [_inventory("1492807", "s3://bucket/1492807.svs")]
        result_path = tmp_path / "task-0000.jsonl"
        artifact = {
            "image_id": "1492807",
            "source_path": "s3://bucket/1492807.svs",
            "artifact_uri": "s3://thumbs/1492807.jpg",
            "width": 1024,
            "height": 768,
            "content_type": "image/jpeg",
            "tile_metadata_json": _tile_metadata(rows[0]),
        }
        with patch.object(module, "_render_candidate_artifact_subprocess", return_value=artifact):
            module.process_candidate_rows(
                warehouse_id="warehouse",
                root_uri="s3://thumbs",
                master_size=1024,
                rows=rows,
                manifest_version="v1",
                result_path=str(result_path),
            )
            module.process_candidate_rows(
                warehouse_id="warehouse",
                root_uri="s3://thumbs",
                master_size=1024,
                rows=rows,
                manifest_version="v1",
                result_path=str(result_path),
            )

        assert len(result_path.read_text(encoding="utf-8").splitlines()) == 1

    def test_existing_thumbnail_is_reused_for_metadata_only_upgrade(self, tmp_path):
        row = _inventory("1492807", "s3://bucket/1492807.svs")
        existing = module.RegistryRow(
            image_id=row.image_id,
            source_path=row.path,
            artifact_uri="s3://thumbs/1492807.jpg",
            width=1024,
            height=768,
            content_type="image/jpeg",
            status="success",
            rendered_at="2026-08-03T00:00:00+00:00",
            error_message="",
            manifest_version="v1",
            source_fingerprint=module.source_fingerprint(row),
        )
        artifact = {
            "image_id": row.image_id,
            "source_path": row.path,
            "artifact_uri": existing.artifact_uri,
            "width": existing.width,
            "height": existing.height,
            "content_type": existing.content_type,
            "tile_metadata_json": _tile_metadata(row),
        }
        with (
            patch.object(module, "_artifact_exists", return_value=True),
            patch.object(module, "_build_metadata_only_record", return_value=artifact) as upgrade,
            patch.object(module, "_render_candidate_artifact_subprocess") as render,
        ):
            module.process_candidate_rows(
                warehouse_id="warehouse",
                root_uri="s3://thumbs",
                master_size=1024,
                rows=[row],
                manifest_version="v2",
                result_path=str(tmp_path / "result.jsonl"),
                registry_rows=[existing],
            )

        upgrade.assert_called_once()
        render.assert_not_called()

    def test_null_fingerprint_forces_rerender(self, tmp_path):
        row = _inventory("1492807", "s3://bucket/1492807.svs")
        existing = module.RegistryRow(
            image_id=row.image_id,
            source_path=row.path,
            artifact_uri="s3://thumbs/1492807.jpg",
            width=1024,
            height=768,
            content_type="image/jpeg",
            status="success",
            rendered_at="2026-08-03T00:00:00+00:00",
            error_message="",
            manifest_version="v1",
        )
        artifact = {
            "image_id": row.image_id,
            "source_path": row.path,
            "artifact_uri": existing.artifact_uri,
            "width": existing.width,
            "height": existing.height,
            "content_type": existing.content_type,
            "tile_metadata_json": _tile_metadata(row),
        }
        with (
            patch.object(module, "_artifact_exists", return_value=True),
            patch.object(module, "_build_metadata_only_record") as upgrade,
            patch.object(module, "_render_candidate_artifact_subprocess", return_value=artifact) as render,
        ):
            module.process_candidate_rows(
                warehouse_id="warehouse",
                root_uri="s3://thumbs",
                master_size=1024,
                rows=[row],
                manifest_version="v2",
                result_path=str(tmp_path / "result.jsonl"),
                registry_rows=[existing],
            )

        upgrade.assert_not_called()
        render.assert_called_once()

    def test_task_audit_rejects_partial_results(self, tmp_path):
        records = [_result_record("1492807", "s3://bucket/1492807.svs")]
        run_dir, _ = _run_fixture(tmp_path, records)
        candidate_path = run_dir / "candidates" / "task-0000.jsonl"
        candidate_path.write_text(
            candidate_path.read_text(encoding="utf-8")
            + json.dumps({"image_id": "1492808", "path": "s3://bucket/1492808.svs"})
            + "\n",
            encoding="utf-8",
        )

        audit = module.audit_thumbnail_run(str(run_dir))

        assert audit["publishable"] is False
        assert audit["incomplete_task_indexes"] == [0]

    def test_task_audit_adopts_matching_legacy_summary(self, tmp_path):
        records = [
            _result_record("1492807", "s3://bucket/1492807.svs"),
            _result_record("1492808", "s3://bucket/1492808.svs", status="failed"),
        ]
        run_dir, _ = _run_fixture(tmp_path, records)
        (run_dir / "logs" / "slide-thumbnail-summary-123-0.json").write_text(
            json.dumps(
                {
                    "candidate_count": 2,
                    "failure_count": 1,
                    "manifest_version": "v1",
                    "success_count": 1,
                    "task_index": 0,
                }
            ),
            encoding="utf-8",
        )

        audit = module.audit_thumbnail_run(str(run_dir), adopt_legacy=True)

        assert audit["publishable"] is True
        assert audit["legacy_adopted_task_indexes"] == [0]
        assert (run_dir / "results" / "task-0000.done.json").exists()

    def test_task_audit_rejects_duplicate_results(self, tmp_path):
        record = _result_record("1492807", "s3://bucket/1492807.svs")
        run_dir, result_path = _run_fixture(tmp_path, [record])
        result_path.write_text(
            json.dumps(record) + "\n" + json.dumps(record) + "\n",
            encoding="utf-8",
        )

        audit = module.audit_thumbnail_run(str(run_dir))

        assert audit["incomplete_task_indexes"] == [0]
        assert "duplicate result" in audit["tasks"][0]["reason"]

    def test_slurm_array_expression_compresses_ranges(self):
        assert module.slurm_array_expression([5, 2, 3, 9, 7, 8]) == "2-3,5,7-9"

    def test_result_publisher_skips_truncated_line(self, tmp_path):
        result_path = tmp_path / "task-0000.jsonl"
        result_path.write_text(
            json.dumps(
                {
                    "image_id": "1492807",
                    "source_path": "s3://bucket/1492807.svs",
                    "artifact_uri": "s3://thumbs/1492807.jpg",
                    "width": 1024,
                    "height": 768,
                    "content_type": "image/jpeg",
                    "status": "success",
                    "rendered_at": "2026-08-06 12:00:00",
                    "error_message": None,
                    "manifest_version": "v1",
                }
            )
            + "\n{\"image_id\":",
            encoding="utf-8",
        )

        with patch.object(module, "_upsert_registry_rows") as upsert:
            stats = module.publish_registry_results("warehouse", [str(result_path)])

        assert stats == {"success_count": 1, "failure_count": 0, "record_count": 1}
        upsert.assert_called_once()

    def test_cleanup_removes_only_ephemeral_run_directories(self, tmp_path):
        for name in ("candidates", "results", "tmp", "blockcache", "logs"):
            (tmp_path / name).mkdir()
            (tmp_path / name / "marker").write_text("x")
        (tmp_path / "run-meta.json").write_text("{}")

        module.cleanup_run_artifacts(str(tmp_path))

        assert not (tmp_path / "candidates").exists()
        assert not (tmp_path / "results").exists()
        assert not (tmp_path / "tmp").exists()
        assert not (tmp_path / "blockcache").exists()
        assert (tmp_path / "logs" / "marker").exists()
        assert (tmp_path / "run-meta.json").exists()


class TestDeltaSelection:
    def test_skips_already_published_matching_rows(self):
        inventory = [_inventory("1492807", "s3://bucket/a.svs")]
        registry = [
            module.RegistryRow(
                image_id="1492807",
                source_path="s3://bucket/a.svs",
                artifact_uri="s3://thumbs/1492807.jpg",
                width=100,
                height=80,
                content_type="image/jpeg",
                status="success",
                rendered_at="2026-08-03T00:00:00+00:00",
                error_message="",
                manifest_version="20260803000000",
                tile_metadata_json=_tile_metadata(inventory[0]),
                source_fingerprint=module.source_fingerprint(inventory[0]),
            )
        ]

        rows = module._select_candidate_rows(
            inventory,
            registry,
            retry_failures_only=False,
        )

        assert rows == []

    def test_path_change_forces_regeneration(self):
        inventory = [_inventory("1492807", "s3://bucket/b.svs")]
        old_source = _inventory("1492807", "s3://bucket/a.svs")
        registry = [
            module.RegistryRow(
                image_id="1492807",
                source_path="s3://bucket/a.svs",
                artifact_uri="s3://thumbs/1492807.jpg",
                width=100,
                height=80,
                content_type="image/jpeg",
                status="success",
                rendered_at="2026-08-03T00:00:00+00:00",
                error_message="",
                manifest_version="20260803000000",
                tile_metadata_json=_tile_metadata(old_source),
                source_fingerprint=module.source_fingerprint(old_source),
            )
        ]

        rows = module._select_candidate_rows(
            inventory,
            registry,
            retry_failures_only=False,
        )

        assert rows == inventory

    def test_retry_failures_only_limits_to_non_success_rows(self):
        inventory = [
            _inventory("1492807", "s3://bucket/a.svs"),
            _inventory("1492808", "s3://bucket/b.svs"),
        ]
        registry = [
            module.RegistryRow(
                image_id="1492807",
                source_path="s3://bucket/a.svs",
                artifact_uri="s3://thumbs/1492807.jpg",
                width=100,
                height=80,
                content_type="image/jpeg",
                status="success",
                rendered_at="2026-08-03T00:00:00+00:00",
                error_message="",
                manifest_version="20260803000000",
                tile_metadata_json=_tile_metadata(inventory[0]),
                source_fingerprint=module.source_fingerprint(inventory[0]),
            ),
            module.RegistryRow(
                image_id="1492808",
                source_path="s3://bucket/b.svs",
                artifact_uri="s3://thumbs/1492808.jpg",
                width=0,
                height=0,
                content_type="image/jpeg",
                status="failed",
                rendered_at="2026-08-03T00:00:00+00:00",
                error_message="boom",
                manifest_version="20260803000000",
            ),
        ]

        rows = module._select_candidate_rows(
            inventory,
            registry,
            retry_failures_only=True,
        )

        assert rows == [inventory[1]]

    def test_legacy_success_without_metadata_is_regenerated(self):
        inventory = [_inventory("1492807", "s3://bucket/a.svs")]
        registry = [
            module.RegistryRow(
                image_id="1492807",
                source_path="s3://bucket/a.svs",
                artifact_uri="s3://thumbs/1492807.jpg",
                width=100,
                height=80,
                content_type="image/jpeg",
                status="success",
                rendered_at="2026-08-03T00:00:00+00:00",
                error_message="",
                manifest_version="20260803000000",
            )
        ]

        assert module._select_candidate_rows(
            inventory,
            registry,
            retry_failures_only=False,
        ) == inventory
        assert module._select_candidate_rows(
            inventory,
            registry,
            retry_failures_only=True,
        ) == inventory

    def test_legacy_metadata_is_regenerated(self):
        inventory = [_inventory("1492807", "s3://bucket/a.svs")]
        metadata = json.loads(_tile_metadata(inventory[0]))
        metadata.pop("tile_metadata_schema_version")
        registry = [
            module.RegistryRow(
                image_id="1492807",
                source_path="s3://bucket/a.svs",
                artifact_uri="s3://thumbs/1492807.jpg",
                width=100,
                height=80,
                content_type="image/jpeg",
                status="success",
                rendered_at="2026-08-03T00:00:00+00:00",
                error_message="",
                manifest_version="20260803000000",
                tile_metadata_json=json.dumps(metadata),
                source_fingerprint=module.source_fingerprint(inventory[0]),
            )
        ]

        assert module._select_candidate_rows(
            inventory,
            registry,
            retry_failures_only=False,
        ) == inventory

    def test_malformed_current_metadata_is_regenerated(self):
        inventory = [_inventory("1492807", "s3://bucket/a.svs")]
        metadata = json.loads(_tile_metadata(inventory[0]))
        metadata["level_dimensions"] = []
        registry = [
            module.RegistryRow(
                image_id="1492807",
                source_path="s3://bucket/a.svs",
                artifact_uri="s3://thumbs/1492807.jpg",
                width=100,
                height=80,
                content_type="image/jpeg",
                status="success",
                rendered_at="2026-08-03T00:00:00+00:00",
                error_message="",
                manifest_version="20260803000000",
                tile_metadata_json=json.dumps(metadata),
                source_fingerprint=module.source_fingerprint(inventory[0]),
            )
        ]

        assert module._select_candidate_rows(
            inventory,
            registry,
            retry_failures_only=False,
        ) == inventory

    def test_default_mode_skips_existing_failed_rows(self):
        inventory = [_inventory("1492808", "s3://bucket/b.svs")]
        registry = [
            module.RegistryRow(
                image_id="1492808",
                source_path="s3://bucket/b.svs",
                artifact_uri="s3://thumbs/1492808.jpg",
                width=0,
                height=0,
                content_type="image/jpeg",
                status="failed",
                rendered_at="2026-08-05T00:00:00+00:00",
                error_message="boom",
                manifest_version="20260805000000",
            )
        ]

        rows = module._select_candidate_rows(
            inventory,
            registry,
            retry_failures_only=False,
        )

        assert rows == []

    def test_retry_mode_uses_failed_current_source_over_old_success(self):
        current = _inventory("1492807", "s3://bucket/promoted.svs", size=200)
        previous = _inventory("1492807", "s3://bucket/original.svs")
        registry = [
            module.RegistryRow(
                image_id="1492807",
                source_path=previous.path,
                artifact_uri="s3://thumbs/1492807.jpg",
                width=100,
                height=80,
                content_type="image/jpeg",
                status="success",
                rendered_at="2026-08-05T00:00:00+00:00",
                error_message="",
                manifest_version="v1",
                tile_metadata_json=_tile_metadata(previous),
                source_fingerprint=module.source_fingerprint(previous),
            ),
            module.RegistryRow(
                image_id="1492807",
                source_path=current.path,
                artifact_uri="s3://thumbs/1492807.jpg",
                width=0,
                height=0,
                content_type="image/jpeg",
                status="failed",
                rendered_at="2026-08-06T00:00:00+00:00",
                error_message="render failed",
                manifest_version="v2",
            ),
        ]

        assert module._select_candidate_rows(
            [current], registry, retry_failures_only=True
        ) == [current]

    def test_incomplete_serving_identity_aborts_candidate_selection(self):
        inventory = [module.InventoryRow("1492807", "s3://bucket/a.svs")]

        with pytest.raises(ValueError, match="incomplete or invalid source identity"):
            module._select_candidate_rows(inventory, [], retry_failures_only=False)


class TestManifestBuild:
    def test_manifest_uses_only_successful_current_inventory_rows(self):
        inventory = [
            _inventory("1492807", "s3://bucket/a.svs"),
            _inventory("1492808", "s3://bucket/b.svs"),
        ]
        registry = [
            module.RegistryRow(
                image_id="1492807",
                source_path="s3://bucket/a.svs",
                artifact_uri="s3://thumbs/1492807.jpg",
                width=100,
                height=80,
                content_type="image/jpeg",
                status="success",
                rendered_at="2026-08-03T00:00:00+00:00",
                error_message="",
                manifest_version="20260803000000",
                tile_metadata_json=_tile_metadata(inventory[0]),
                source_fingerprint=module.source_fingerprint(inventory[0]),
            ),
            module.RegistryRow(
                image_id="1492808",
                source_path="s3://bucket/b.svs",
                artifact_uri="s3://thumbs/1492808.jpg",
                width=0,
                height=0,
                content_type="image/jpeg",
                status="failed",
                rendered_at="2026-08-03T00:00:00+00:00",
                error_message="boom",
                manifest_version="20260803000000",
            ),
        ]

        manifest = module._build_manifest_from_registry(
            module._successful_registry_for_inventory(inventory, registry),
            master_size=1024,
            manifest_version="20260803120000",
        )

        assert list(manifest["slides"]) == ["1492807"]

    def test_manifest_rejects_null_fingerprint_rows(self):
        inventory = [_inventory("1492807", "s3://bucket/a.svs")]
        registry = [
            module.RegistryRow(
                image_id="1492807",
                source_path=inventory[0].path,
                artifact_uri="s3://thumbs/1492807.jpg",
                width=100,
                height=80,
                content_type="image/jpeg",
                status="success",
                rendered_at="2026-08-03T00:00:00+00:00",
                error_message="",
                manifest_version="20260803000000",
                tile_metadata_json=json.dumps(
                    {
                        "dimensions": {"width": 4096, "height": 4096},
                        "levels": 1,
                        "level_dimensions": [{"width": 4096, "height": 4096}],
                        "level_downsamples": [1.0],
                        "max_zoom": 4,
                        "tile_size": 256,
                        "safe_min_level": 1,
                        "tile_metadata_schema_version": module.TILE_METADATA_SCHEMA_VERSION,
                        "decode_policy_version": module.decode_policy_version(),
                        "max_decode_pixels": module.settings.max_decode_pixels,
                        "thumbnail_max_decode_pixels": module.settings.thumbnail_max_decode_pixels,
                    }
                ),
                source_fingerprint=None,
            )
        ]

        assert module._successful_registry_for_inventory(inventory, registry) == []

    def test_manifest_rejects_stale_source_and_decode_policy(self):
        inventory = [_inventory("1492807", "s3://bucket/a.svs")]
        registry = [
            module.RegistryRow(
                image_id="1492807",
                source_path=inventory[0].path,
                artifact_uri="s3://thumbs/1492807.jpg",
                width=100,
                height=80,
                content_type="image/jpeg",
                status="success",
                rendered_at="2026-08-03T00:00:00+00:00",
                error_message="",
                manifest_version="20260803000000",
                tile_metadata_json=_tile_metadata(
                    _inventory("1492807", "s3://bucket/a.svs", size=101),
                    policy_version="geometry-v0",
                ),
                source_fingerprint=module.source_fingerprint(
                    _inventory("1492807", "s3://bucket/a.svs", size=101)
                ),
            )
        ]

        assert module._successful_registry_for_inventory(inventory, registry) == []


class TestRunIncrementalPipeline:
    def test_keeps_prior_good_entries_when_current_batch_has_failures(self):
        inventory = [
            _inventory("1492807", "s3://bucket/a.svs"),
            _inventory("1492808", "s3://bucket/b.svs"),
        ]
        first_registry = [
            module.RegistryRow(
                image_id="1492807",
                source_path="s3://bucket/a.svs",
                artifact_uri="s3://thumbs/1492807.jpg",
                width=100,
                height=80,
                content_type="image/jpeg",
                status="success",
                rendered_at="2026-08-03T00:00:00+00:00",
                error_message="",
                manifest_version="20260803000000",
                tile_metadata_json=_tile_metadata(inventory[0]),
                source_fingerprint=module.source_fingerprint(inventory[0]),
            )
        ]
        second_registry = first_registry + [
            module.RegistryRow(
                image_id="1492808",
                source_path="s3://bucket/b.svs",
                artifact_uri="s3://thumbs/1492808.jpg",
                width=0,
                height=0,
                content_type="image/jpeg",
                status="failed",
                rendered_at="2026-08-03T01:00:00+00:00",
                error_message="boom",
                manifest_version="20260803010000",
            )
        ]

        with (
            patch.object(module, "_ensure_registry_table"),
            patch.object(module, "_fetch_inventory_rows", return_value=inventory),
            patch.object(module, "_fetch_registry_rows", side_effect=[first_registry, second_registry]),
            patch.object(module, "_render_candidate_artifact_subprocess", side_effect=RuntimeError("boom")),
            patch.object(module, "_upsert_registry_rows"),
            patch.object(module, "_publish_manifest") as publish_manifest,
        ):
            manifest, failures, candidates = module.run_incremental_pipeline(
                warehouse_id="wh",
                manifest_uri="s3://thumbs/manifest.json",
                root_uri="s3://thumbs/masters",
                master_size=1024,
                limit=None,
                retry_failures_only=False,
            )

        assert [row.image_id for row in candidates] == ["1492808"]
        assert len(failures) == 1
        assert list(manifest["slides"]) == ["1492807"]
        publish_manifest.assert_called_once()


class TestSummaryPayload:
    def test_reports_candidate_and_failure_counts(self):
        manifest = {
            "generated_at": "2026-08-05T12:00:00+00:00",
            "manifest_version": "20260805120000",
            "slides": {"1492807": {"uri": "s3://thumbs/1492807.jpg"}},
        }
        failures = [{"image_id": "1492808", "error": "boom"}]
        candidates = [
            _inventory("1492807", "s3://bucket/a.svs"),
            _inventory("1492808", "s3://bucket/b.svs"),
        ]

        summary = module._summary_payload(
            manifest=manifest,
            failures=failures,
            candidates=candidates,
        )

        assert summary["candidate_count"] == 2
        assert summary["success_count"] == 1
        assert summary["failure_count"] == 1
        assert summary["manifest_slide_count"] == 1
