import asyncio
from unittest.mock import patch

import pytest

import app.main as main_module
from app.config import settings


class TestSingleFlight:
    @pytest.mark.asyncio
    async def test_identical_cache_misses_share_one_decode(self):
        singleflight = main_module._SingleFlight()
        calls = 0

        async def producer():
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.01)
            return b"jpeg"

        results = await asyncio.gather(
            *[singleflight.do("tile:1:0:0:0", "tile", producer) for _ in range(5)]
        )

        assert results == [b"jpeg"] * 5
        assert calls == 1


class TestImageOperationGate:
    @pytest.mark.asyncio
    async def test_distinct_requests_respect_image_operation_cap(self):
        active = 0
        peak = 0

        async def fake_in_thread(fn, *args):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1
            return fn(*args)

        main_module._image_operation_semaphore = asyncio.Semaphore(2)

        with patch.object(main_module, "_in_thread", fake_in_thread):
            results = await asyncio.gather(
                *[main_module._run_image_operation(lambda value=value: value) for value in range(5)]
            )

        assert results == [0, 1, 2, 3, 4]
        assert peak == 2


class TestThumbnailWorker:
    @pytest.mark.asyncio
    async def test_timeout_terminates_child_process(self, monkeypatch):
        class FakeProcess:
            def __init__(self):
                self.returncode = None
                self.killed = False

            async def communicate(self):
                if self.killed:
                    self.returncode = -9
                    return b"", b""
                await asyncio.sleep(60)

            def kill(self):
                self.killed = True

        process = FakeProcess()

        async def fake_create_process(*args, **kwargs):
            return process

        monkeypatch.setattr(main_module.asyncio, "create_subprocess_exec", fake_create_process)
        monkeypatch.setattr(main_module.settings, "thumbnail_timeout_sec", 0.01)

        with pytest.raises(asyncio.TimeoutError):
            await main_module._run_thumbnail_worker(
                "1492807",
                "s3://bucket/1492807.svs",
            )

        assert process.killed is True


class TestPathCache:
    def test_path_cache_evicts_least_recently_used_entry(self, monkeypatch):
        main_module._path_cache.clear()
        monkeypatch.setattr(settings, "path_cache_capacity", 2)

        with patch("app.main.meta.get_slide_path", side_effect=lambda image_id, _: f"s3://bucket/{image_id}.svs"):
            assert main_module._resolve_slide_id("a") == "s3://bucket/a.svs"
            assert main_module._resolve_slide_id("b") == "s3://bucket/b.svs"
            assert main_module._resolve_slide_id("a") == "s3://bucket/a.svs"
            assert main_module._resolve_slide_id("c") == "s3://bucket/c.svs"

        assert list(main_module._path_cache.keys()) == ["a", "c"]


def test_build_patient_hierarchy_supports_deployed_canonical_rows():
    hierarchy = main_module._build_patient_hierarchy(
        [
            {
                "match_level": "UNMATCHED",
                "patient_id": "P-1",
                "sample_id": None,
                "reference_sample_id": "S-1",
                "image_id": "unmatched-slide",
                "block_id": "S-1/2-4A",
                "block_label": "4A",
                "part_type": "COLON",
                "part_description": "Primary tumor",
                "path_dx_title": "Primary tumor",
                "stain_name": "H&E, Initial",
                "stain_group": "H&E (Initial)",
                "magnification": "40x",
                "file_size_bytes": 10,
                "slide_path": None,
                "slide_timepoint_days": -4,
                "slide_timepoint_source": "Procedure date",
            },
            {
                "match_level": "PART",
                "patient_id": "P-1",
                "sample_id": "S-1",
                "reference_sample_id": "S-1",
                "image_id": "part-slide",
                "block_id": "part/1-2T",
                "block_label": "2T",
                "part_type": "COLON",
                "part_description": "Primary tumor",
                "path_dx_title": "Primary tumor",
                "stain_name": "H&E, Initial",
                "stain_group": "H&E (Initial)",
                "magnification": "40x",
                "file_size_bytes": 20,
                "slide_path": "s3://slides/part-slide.svs",
                "slide_timepoint_days": -3,
                "slide_timepoint_source": "Procedure date",
            },
            {
                "match_level": "BLOCK",
                "patient_id": "P-1",
                "sample_id": "S-1",
                "reference_sample_id": "S-1",
                "image_id": "block-slide",
                "block_id": "S-1/2-3A",
                "block_label": "3A",
                "part_type": "COLON",
                "part_description": "Primary tumor",
                "path_dx_title": "Primary tumor",
                "stain_name": "CDX2 IHC",
                "stain_group": "IHC",
                "magnification": "20x",
                "file_size_bytes": 30,
                "slide_path": "s3://slides/block-slide.svs",
                "slide_timepoint_days": -2,
                "slide_timepoint_source": "Procedure date",
            },
        ]
    )

    assert hierarchy["referenceSampleId"] == "S-1"
    assert {group["sampleId"] for group in hierarchy["sampleGroups"]} == {None, "S-1"}

    unmatched = next(group for group in hierarchy["sampleGroups"] if group["sampleId"] is None)
    unmatched_slide = unmatched["parts"][0]["blocks"][0]["slides"][0]
    assert unmatched_slide["matchLevel"] == "UNMATCHED"
    assert unmatched_slide["canServeTiles"] is False
    assert unmatched_slide["specimenKey"] == "unmatched::2::4"

    sample = next(group for group in hierarchy["sampleGroups"] if group["sampleId"] == "S-1")
    slides = [
        slide
        for part in sample["parts"]
        for block in part["blocks"]
        for slide in block["slides"]
    ]
    assert {slide["imageId"] for slide in slides} == {"part-slide", "block-slide"}
    assert next(slide for slide in slides if slide["imageId"] == "block-slide")["slideType"] == "IHC"
