from __future__ import annotations

from io import BytesIO
from unittest.mock import MagicMock

from PIL import Image

from app import thumbnail_store


def _jpeg_bytes(size: tuple[int, int]) -> bytes:
    image = Image.new("RGB", size, (120, 140, 160))
    out = BytesIO()
    image.save(out, format="JPEG")
    return out.getvalue()


class TestThumbnailStore:
    def test_s3_read_falls_back_to_fsspec_after_direct_client_failure(self, monkeypatch):
        record = thumbnail_store.ThumbnailRecord(
            image_id="2907269",
            uri="s3://bucket/2907269.jpg",
            width=128,
            height=88,
        )
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
        record = thumbnail_store.ThumbnailRecord(
            image_id="1492807",
            uri="s3://bucket/1492807.jpg",
            width=128,
            height=79,
        )

        result, status = thumbnail_store.render_thumbnail_payload(
            thumb_bytes, record, 128, 96
        )

        assert result == thumb_bytes
        assert status == {"status": "ok", "reason": "master"}

    def test_returns_master_bytes_without_upscaling(self, tmp_path, monkeypatch):
        thumb_bytes = _jpeg_bytes((1024, 512))
        thumb_path = tmp_path / "1492807.jpg"
        thumb_path.write_bytes(thumb_bytes)
        record = thumbnail_store.ThumbnailRecord(
            image_id="1492807",
            uri=str(thumb_path),
            width=1024,
            height=512,
        )
        monkeypatch.setattr(thumbnail_store.settings, "jpeg_quality", 85)

        result, status = thumbnail_store.render_thumbnail_response(record, 2048, 2048)

        assert result == thumb_bytes
        assert status == {"status": "ok", "reason": "master"}

    def test_downsizes_master_for_smaller_requests(self, tmp_path, monkeypatch):
        thumb_path = tmp_path / "1492807.jpg"
        thumb_path.write_bytes(_jpeg_bytes((1024, 512)))
        record = thumbnail_store.ThumbnailRecord(
            image_id="1492807",
            uri=str(thumb_path),
            width=1024,
            height=512,
        )
        monkeypatch.setattr(thumbnail_store.settings, "jpeg_quality", 85)

        result, status = thumbnail_store.render_thumbnail_response(record, 256, 256)

        image = Image.open(BytesIO(result))
        assert image.size == (256, 128)
        assert status == {"status": "ok", "reason": "resized"}
