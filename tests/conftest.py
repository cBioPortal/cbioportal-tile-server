"""Shared test fixtures."""

import math
from unittest.mock import MagicMock

import pytest
from PIL import Image

from app.tiles import TILE_SIZE


def make_mock_slide(width: int = 1024, height: int = 1024, levels: int = 3) -> MagicMock:
    """
    Return a TiffSlide MagicMock with a simple power-of-two pyramid.

    Defaults to 1024×1024 with 3 levels (1024, 512, 256) so max_zoom == 2.
    """
    slide = MagicMock()
    slide.dimensions = (width, height)
    slide.level_count = levels

    dims = [(max(1, width // (2 ** i)), max(1, height // (2 ** i))) for i in range(levels)]
    ds   = [float(2 ** i) for i in range(levels)]
    slide.level_dimensions  = dims
    slide.level_downsamples = ds

    def best_level(target_ds: float) -> int:
        best = 0
        for i, d in enumerate(ds):
            if d <= target_ds:
                best = i
        return best

    slide.get_best_level_for_downsample = best_level

    def read_region(loc, level, size):
        return Image.new("RGBA", size, (255, 255, 255, 255))

    slide.read_region = read_region

    slide.properties = {
        "tiffslide.mpp-x":          "0.5034",
        "tiffslide.mpp-y":          "0.5034",
        "tiffslide.vendor":         "aperio",
        "tiffslide.objective-power": "20",
    }
    return slide


@pytest.fixture
def mock_slide():
    return make_mock_slide()
