from io import BytesIO
from unittest.mock import MagicMock, patch

from PIL import Image

from app import thumbnail_worker
from app.tiles import NoSafeThumbnailOverview


def test_unsafe_overview_fallback_runs_in_worker_and_persists_record():
    slide = MagicMock()
    slide.get_thumbnail.return_value = Image.new("RGB", (800, 600), (120, 120, 120))
    fileobj = MagicMock()
    record = thumbnail_worker.ThumbnailRecord(
        image_id="1492807",
        uri="s3://thumbs/masters/1492807.jpg",
        width=800,
        height=600,
    )

    with (
        patch.object(thumbnail_worker, "open_slide", return_value=(slide, fileobj)),
        patch.object(
            thumbnail_worker,
            "get_thumbnail_bytes_with_plan",
            side_effect=NoSafeThumbnailOverview(
                level=0,
                level_width=10_000,
                level_height=10_000,
                requested_pixels=100_000_000,
            ),
        ),
        patch.object(thumbnail_worker, "store_generated_thumbnail", return_value=record) as store,
    ):
        result = thumbnail_worker.generate_thumbnail(
            "1492807",
            "s3://bucket/1492807.svs",
            1024,
        )

    assert result == record
    store.assert_called_once()
    payload = store.call_args.args[1]
    assert Image.open(BytesIO(payload)).size == (800, 600)
    slide.close.assert_called_once()
    fileobj.close.assert_called_once()
