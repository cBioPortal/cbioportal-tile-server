"""Tests for tile coordinate math and slide metadata extraction (app/tiles.py)."""

import math
from unittest.mock import MagicMock

import pytest
from PIL import Image

from app.tiles import (
    OverviewTooLarge,
    TILE_SIZE,
    _resize_and_pad,
    _slide_properties_metadata,
    _tile_geometry,
    get_tile_bytes,
    get_thumbnail_bytes,
    max_zoom,
    slide_metadata,
)
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


class TestSlideMetadata:
    def test_slide_properties_helper_parses_values(self):
        slide = make_mock_slide()
        assert _slide_properties_metadata(slide) == (
            pytest.approx(0.5034),
            pytest.approx(0.5034),
            "aperio",
            20,
        )

    def test_tiffslide_namespace_parsed(self):
        meta = slide_metadata(make_mock_slide())
        assert meta["mpp"]["x"]       == pytest.approx(0.5034)
        assert meta["mpp"]["y"]       == pytest.approx(0.5034)
        assert meta["vendor"]         == "aperio"
        assert meta["objective_power"] == 20

    def test_openslide_fallback(self):
        slide = make_mock_slide()
        slide.properties = {
            "openslide.mpp-x":          "0.25",
            "openslide.mpp-y":          "0.25",
            "openslide.vendor":         "leica",
            "openslide.objective-power": "40",
        }
        meta = slide_metadata(slide)
        assert meta["mpp"]["x"]        == pytest.approx(0.25)
        assert meta["vendor"]          == "leica"
        assert meta["objective_power"] == 40

    def test_missing_properties_returns_defaults(self):
        slide = make_mock_slide()
        slide.properties = {}
        meta = slide_metadata(slide)
        assert meta["mpp"]["x"]       == 0.0
        assert meta["vendor"]         == ""
        assert meta["objective_power"] is None

    def test_structure_keys_present(self):
        meta = slide_metadata(make_mock_slide(2048, 1024))
        for key in ("dimensions", "levels", "max_zoom", "tile_size",
                    "mpp", "vendor", "objective_power",
                    "level_dimensions", "level_downsamples"):
            assert key in meta

    def test_dimensions_correct(self):
        meta = slide_metadata(make_mock_slide(2048, 1536))
        assert meta["dimensions"]["width"]  == 2048
        assert meta["dimensions"]["height"] == 1536


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
