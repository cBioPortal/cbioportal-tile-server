"""Tests for tile coordinate math and safe pixel/thumbnail rendering."""

import math
from unittest.mock import MagicMock

import pytest
from PIL import Image

from app.tiles import (
    NoSafeThumbnailOverview,
    OverviewTooLarge,
    TILE_SIZE,
    _resize_and_pad,
    _tile_geometry,
    get_tile_bytes,
    get_thumbnail_bytes,
    get_thumbnail_bytes_with_plan,
    max_zoom,
    safe_min_level,
    safe_min_level_from_geometry,
)
from app.metadata_contract import validate_tile_metadata
from tests.conftest import make_mock_slide


class TestMaxZoom:
    def test_large_slide(self):
        slide = make_mock_slide(100_000, 80_000)
        assert max_zoom(slide) == math.ceil(math.log2(100_000 / TILE_SIZE))

    def test_square_1024(self):
        # log2(1024/256) = 2
        assert max_zoom(make_mock_slide(1024, 1024)) == 2

    def test_single_tile_slide(self):
        assert max_zoom(make_mock_slide(256, 256)) == 0

    def test_tall_slide_uses_larger_dimension(self):
        slide = make_mock_slide(512, 4096)
        assert max_zoom(slide) == math.ceil(math.log2(4096 / TILE_SIZE))

    def test_safe_min_level_is_geometry_only_and_bounded(self, monkeypatch):
        slide = make_mock_slide(4096, 4096, levels=1)
        monkeypatch.setattr("app.tiles.settings.max_decode_pixels", 4_194_304)
        slide.read_region = MagicMock(side_effect=AssertionError("must not decode"))

        assert safe_min_level(slide) == 1
        slide.read_region.assert_not_called()

    def test_safe_min_level_geometry_matches_slide_helper(self, monkeypatch):
        slide = make_mock_slide(4096, 4096, levels=1)
        monkeypatch.setattr("app.tiles.settings.max_decode_pixels", 4_194_304)

        assert safe_min_level_from_geometry(
            width=slide.dimensions[0],
            height=slide.dimensions[1],
            level_dimensions=list(slide.level_dimensions),
            level_downsamples=list(slide.level_downsamples),
            tile_size=256,
            max_decode_pixels=4_194_304,
        ) == safe_min_level(slide)

    def test_v2_metadata_requires_complete_bounded_contract(self):
        metadata = {
            "dimensions": {"width": 4096, "height": 4096},
            "levels": 1,
            "level_dimensions": [{"width": 4096, "height": 4096}],
            "level_downsamples": [1.0],
            "max_zoom": 4,
            "tile_size": 256,
            "safe_min_level": 1,
            "tile_metadata_schema_version": 2,
            "decode_policy_version": "geometry-v2;tile-max=16777216;thumbnail-max=16777216",
            "max_decode_pixels": 16_777_216,
            "thumbnail_max_decode_pixels": 16_777_216,
        }
        assert validate_tile_metadata(metadata) == (True, None)

    def test_v2_metadata_rejects_missing_safe_level(self):
        metadata = {
            "dimensions": {"width": 256, "height": 256},
            "levels": 1,
            "level_dimensions": [{"width": 256, "height": 256}],
            "level_downsamples": [1.0],
            "max_zoom": 0,
            "tile_size": 256,
            "tile_metadata_schema_version": 2,
            "decode_policy_version": "geometry-v2;tile-max=16777216;thumbnail-max=16777216",
            "max_decode_pixels": 16_777_216,
            "thumbnail_max_decode_pixels": 16_777_216,
        }
        assert validate_tile_metadata(metadata) == (False, "missing_safe_min_level")

    def test_schema_null_metadata_remains_legacy_compatible(self):
        assert validate_tile_metadata(
            {
                "dimensions": {"width": 256, "height": 256},
                "levels": 1,
                "level_dimensions": [{"width": 256, "height": 256}],
                "max_zoom": 0,
                "tile_size": 256,
            }
        ) == (True, None)


class TestTileHelpers:
    def test_tile_geometry_returns_expected_values(self):
        slide = make_mock_slide(1024, 1024, levels=3)
        _, target_ds, x0, y0, src_w, src_h, out_w, out_h = _tile_geometry(slide, 2, 1, 1)
        assert target_ds == 1
        assert x0 == TILE_SIZE
        assert y0 == TILE_SIZE
        assert src_w == TILE_SIZE
        assert src_h == TILE_SIZE
        assert out_w == TILE_SIZE
        assert out_h == TILE_SIZE

    def test_tile_geometry_rejects_invalid_tile(self):
        slide = make_mock_slide(256, 256, levels=1)
        with pytest.raises(ValueError):
            _tile_geometry(slide, 0, 1, 0)

    def test_resize_and_pad_keeps_full_tiles(self):
        region = Image.new("RGB", (TILE_SIZE, TILE_SIZE), (0, 0, 0))
        result = _resize_and_pad(region, TILE_SIZE, TILE_SIZE)
        assert result.size == (TILE_SIZE, TILE_SIZE)

    def test_resize_and_pad_pads_edge_tiles(self):
        region = Image.new("RGB", (128, 128), (0, 0, 0))
        result = _resize_and_pad(region, 128, 128)
        assert result.size == (TILE_SIZE, TILE_SIZE)


class TestGetTileBytes:
    def test_valid_tile_returns_jpeg(self):
        slide = make_mock_slide(1024, 1024, levels=3)
        result = get_tile_bytes(slide, max_zoom(slide), 0, 0)
        assert isinstance(result, bytes)
        assert result[:2] == b"\xff\xd8"          # JPEG SOI marker

    def test_zoom_zero_overview_tile(self):
        result = get_tile_bytes(make_mock_slide(4096, 4096, levels=5), 0, 0, 0)
        assert result[:2] == b"\xff\xd8"

    def test_overview_rejected_when_decode_budget_exceeded(self, monkeypatch):
        slide = make_mock_slide(4096, 4096, levels=1)
        slide.read_region = MagicMock(side_effect=AssertionError("read_region should not run"))
        monkeypatch.setattr("app.tiles.settings.max_decode_pixels", 4_194_304)
        with pytest.raises(OverviewTooLarge):
            get_tile_bytes(slide, 0, 0, 0)

    def test_overview_at_pixel_boundary_is_allowed(self, monkeypatch):
        slide = make_mock_slide(2048, 2048, levels=1)
        monkeypatch.setattr("app.tiles.settings.max_decode_pixels", 4_194_304)
        result = get_tile_bytes(slide, 0, 0, 0)
        assert result[:2] == b"\xff\xd8"

    def test_z_above_max_raises(self):
        slide = make_mock_slide()
        mz = max_zoom(slide)
        with pytest.raises(ValueError, match="zoom"):
            get_tile_bytes(slide, mz + 1, 0, 0)

    def test_negative_z_raises(self):
        with pytest.raises(ValueError, match="zoom"):
            get_tile_bytes(make_mock_slide(), -1, 0, 0)

    def test_out_of_bounds_x_raises(self):
        # 256×256 slide has max_zoom=0; only tile (0,0) is valid
        slide = make_mock_slide(256, 256, levels=1)
        with pytest.raises(ValueError):
            get_tile_bytes(slide, 0, 1, 0)

    def test_out_of_bounds_y_raises(self):
        slide = make_mock_slide(256, 256, levels=1)
        with pytest.raises(ValueError):
            get_tile_bytes(slide, 0, 0, 1)

    def test_output_always_tile_size(self):
        """Even edge tiles must be padded to TILE_SIZE×TILE_SIZE."""
        from io import BytesIO
        slide = make_mock_slide(300, 300, levels=2)
        result = get_tile_bytes(slide, max_zoom(slide), 1, 0)   # partial edge tile
        img = Image.open(BytesIO(result))
        assert img.size == (TILE_SIZE, TILE_SIZE)


class TestThumbnailBytes:
    def test_thumbnail_selects_best_safe_level_for_requested_size(self, monkeypatch):
        slide = make_mock_slide(4096, 4096, levels=5)
        monkeypatch.setattr("app.tiles.settings.thumbnail_max_decode_pixels", 1_000_000)

        _, plan = get_thumbnail_bytes_with_plan(slide, 1024, 1024)

        assert plan.requested_pixels <= 1_000_000
        assert plan.level == 3

    def test_thumbnail_uses_aspect_ratio_when_selecting_level(self, monkeypatch):
        slide = make_mock_slide(4096, 1024, levels=5)
        monkeypatch.setattr("app.tiles.settings.thumbnail_max_decode_pixels", 4_194_304)

        _, plan = get_thumbnail_bytes_with_plan(slide, 1024, 1024)

        assert plan.level == 2
        assert (plan.target_width, plan.target_height) == (1024, 256)

    def test_thumbnail_raises_when_no_safe_overview_exists(self, monkeypatch):
        slide = make_mock_slide(4096, 4096, levels=1)
        monkeypatch.setattr("app.tiles.settings.thumbnail_max_decode_pixels", 4_194_304)

        with pytest.raises(NoSafeThumbnailOverview):
            get_thumbnail_bytes_with_plan(slide, 256, 256)

    def test_thumbnail_preserves_slide_aspect_ratio(self):
        slide = make_mock_slide(4096, 1024, levels=5)
        result = get_thumbnail_bytes(slide, 256, 256)
        from io import BytesIO

        img = Image.open(BytesIO(result))
        assert img.size == (256, 64)

    def test_thumbnail_rejected_when_overview_decode_budget_exceeded(self, monkeypatch):
        slide = make_mock_slide(4096, 4096, levels=1)
        slide.read_region = MagicMock(side_effect=AssertionError("read_region should not run"))
        monkeypatch.setattr("app.tiles.settings.max_decode_pixels", 4_194_304)
        with pytest.raises(OverviewTooLarge):
            get_thumbnail_bytes(slide, 256, 256)
