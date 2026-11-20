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


@pytest.fixture(
    params=["sqlite", pytest.param("postgres", marks=pytest.mark.integration)]
)
async def annotation_storage(request, tmp_path, monkeypatch):
    """Annotation storage on SQLite, and on Postgres under ``-m integration``.

    Postgres runs in a fresh schema of ``POSTGRES_TEST_DSN`` per test,
    dropped afterwards.
    """
    import os
    import uuid
    from urllib.parse import urlencode

    from app import annotation_db, annotations

    monkeypatch.setattr(annotations.settings, "annotation_database_url", "")
    monkeypatch.setattr(annotations.settings, "annotation_trusted_writers", ["loader"])
    if request.param == "sqlite":
        await annotations.init_db(str(tmp_path / "annotations.db"), db_url="")
        yield "sqlite"
        annotations._db_path = ""
        annotations._db_url = ""
        return

    import asyncpg

    dsn = os.environ.get("POSTGRES_TEST_DSN")
    if not dsn:
        pytest.fail("POSTGRES_TEST_DSN is required for the PostgreSQL integration suite")
    schema = f"ann_test_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(dsn)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    # The pool belongs to the event loop that opened it; each test has its own.
    monkeypatch.setattr(annotation_db, "_pool", None)
    monkeypatch.setattr(annotation_db, "_pool_dsn", "")
    separator = "&" if "?" in dsn else "?"
    scoped = f"{dsn}{separator}{urlencode({'search_path': schema})}"
    try:
        await annotations.init_db(db_url=scoped)
        yield "postgres"
    finally:
        await annotations.close_db()
        annotations._db_path = ""
        annotations._db_url = ""
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()
