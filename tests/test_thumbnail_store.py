from __future__ import annotations

from io import BytesIO
from unittest.mock import MagicMock

import pytest

from PIL import Image

from app import thumbnail_store


def _jpeg_bytes(size: tuple[int, int]) -> bytes:
    image = Image.new("RGB", size, (120, 140, 160))
    out = BytesIO()
    image.save(out, format="JPEG")
    return out.getvalue()


class TestThumbnailStore:
    def test_prewarm_reads_a_range_after_head(self, monkeypatch):
        client = MagicMock()
        body = MagicMock()
        client.get_object.return_value = {"Body": body}
        previous_client = thumbnail_store._runtime_s3_client
        monkeypatch.setattr(thumbnail_store, "_runtime_s3_client", None)
        monkeypatch.setattr(
            thumbnail_store, "_new_runtime_s3_client", lambda: client
        )
        monkeypatch.setattr(
            thumbnail_store.settings,
            "thumbnail_prewarm_uri",
            "s3://bucket/variants/nav-128x96/slide.jpg",
        )

        try:
            thumbnail_store.initialize_runtime_store()
        finally:
            thumbnail_store._runtime_s3_client = previous_client

        client.head_object.assert_called_once_with(
            Bucket="bucket", Key="variants/nav-128x96/slide.jpg"
        )
        client.get_object.assert_called_once_with(
            Bucket="bucket",
            Key="variants/nav-128x96/slide.jpg",
            Range="bytes=0-4095",
        )
        body.read.assert_called_once_with()
        body.close.assert_called_once_with()

    def test_s3_read_falls_back_to_fsspec_after_direct_client_failure(self, monkeypatch):
        record = thumbnail_store.ThumbnailRecord(uri="s3://bucket/2907269.jpg")
        client = MagicMock()
        client.get_object.side_effect = RuntimeError("direct ECS read failed")
        handle = MagicMock()
        handle.__enter__.return_value = handle
        handle.read.return_value = b"fallback-jpeg"
        filesystem = MagicMock()
        filesystem.open.return_value = handle

        monkeypatch.setattr(thumbnail_store, "_runtime_s3", lambda: client)
        monkeypatch.setattr(
            thumbnail_store, "_filesystem_for_uri", lambda uri: filesystem
        )

        assert thumbnail_store.read_thumbnail_bytes(record) == b"fallback-jpeg"
        client.get_object.assert_called_once_with(
            Bucket="bucket", Key="2907269.jpg"
        )
        filesystem.open.assert_called_once_with("bucket/2907269.jpg", "rb")

    def test_payload_passthrough_for_display_sized_variant(self):
        thumb_bytes = _jpeg_bytes((128, 79))

        result, status = thumbnail_store.render_thumbnail_payload(thumb_bytes, 128, 96)

        assert result == thumb_bytes
        assert status == {"status": "ok", "reason": "master"}

    def test_returns_master_bytes_without_upscaling(self):
        thumb_bytes = _jpeg_bytes((1024, 512))

        result, status = thumbnail_store.render_thumbnail_payload(thumb_bytes, 2048, 2048)

        assert result == thumb_bytes
        assert status == {"status": "ok", "reason": "master"}

    def test_downsizes_master_for_smaller_requests(self, monkeypatch):
        monkeypatch.setattr(thumbnail_store.settings, "jpeg_quality", 85)

        result, status = thumbnail_store.render_thumbnail_payload(_jpeg_bytes((1024, 512)), 256, 256)

        image = Image.open(BytesIO(result))
        assert image.size == (256, 128)
        assert status == {"status": "ok", "reason": "resized"}

    def test_size_is_read_from_the_stored_thumbnail(self):
        assert thumbnail_store.thumbnail_size(_jpeg_bytes((1024, 512))) == (1024, 512)

    def test_unreadable_thumbnail_is_invalid(self):
        with pytest.raises(thumbnail_store.InvalidThumbnail):
            thumbnail_store.thumbnail_size(b"not an image")
        with pytest.raises(thumbnail_store.InvalidThumbnail):
            thumbnail_store.render_thumbnail_payload(b"not an image", 16, 16)
