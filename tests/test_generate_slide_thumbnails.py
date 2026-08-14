from __future__ import annotations

from io import BytesIO
import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from app.tiles import NoSafeThumbnailOverview
from tools import generate_slide_thumbnails as module


def _jpeg_size(payload: bytes) -> tuple[int, int]:
    image = Image.open(BytesIO(payload))
    return image.size


class TestBuildThumbnailRecord:
    def test_disables_blockcache_for_offline_generation(self):
        slide = MagicMock()
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
        inventory = [module.InventoryRow(image_id="1492807", path="s3://bucket/a.svs")]
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
                tile_metadata_json='{"width":1024,"height":768}',
            )
        ]

        rows = module._select_candidate_rows(
            inventory,
            registry,
            retry_failures_only=False,
        )

        assert rows == []

    def test_path_change_forces_regeneration(self):
        inventory = [module.InventoryRow(image_id="1492807", path="s3://bucket/b.svs")]
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
                tile_metadata_json='{"width":1024,"height":768}',
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
            module.InventoryRow(image_id="1492807", path="s3://bucket/a.svs"),
            module.InventoryRow(image_id="1492808", path="s3://bucket/b.svs"),
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
                tile_metadata_json='{"width":1024,"height":768}',
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
        inventory = [module.InventoryRow(image_id="1492807", path="s3://bucket/a.svs")]
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

    def test_default_mode_skips_existing_failed_rows(self):
        inventory = [module.InventoryRow(image_id="1492808", path="s3://bucket/b.svs")]
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


class TestManifestBuild:
    def test_manifest_uses_only_successful_current_inventory_rows(self):
        inventory = [
            module.InventoryRow(image_id="1492807", path="s3://bucket/a.svs"),
            module.InventoryRow(image_id="1492808", path="s3://bucket/b.svs"),
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


class TestRunIncrementalPipeline:
    def test_keeps_prior_good_entries_when_current_batch_has_failures(self):
        inventory = [
            module.InventoryRow(image_id="1492807", path="s3://bucket/a.svs"),
            module.InventoryRow(image_id="1492808", path="s3://bucket/b.svs"),
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
                tile_metadata_json='{"width":1024,"height":768}',
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
            module.InventoryRow(image_id="1492807", path="s3://bucket/a.svs"),
            module.InventoryRow(image_id="1492808", path="s3://bucket/b.svs"),
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
