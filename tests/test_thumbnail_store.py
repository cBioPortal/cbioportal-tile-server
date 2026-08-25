from __future__ import annotations

from io import BytesIO

from PIL import Image

from app import thumbnail_store


def _jpeg_bytes(size: tuple[int, int]) -> bytes:
    image = Image.new("RGB", size, (120, 140, 160))
    out = BytesIO()
    image.save(out, format="JPEG")
    return out.getvalue()


class TestThumbnailStore:
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
