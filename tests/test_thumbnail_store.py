from __future__ import annotations

import json
from io import BytesIO

from PIL import Image

from app import thumbnail_store


def _jpeg_bytes(size: tuple[int, int]) -> bytes:
    image = Image.new("RGB", size, (120, 140, 160))
    out = BytesIO()
    image.save(out, format="JPEG")
    return out.getvalue()


class TestThumbnailStore:
    def test_loads_manifest_record_from_local_file(self, tmp_path, monkeypatch):
        thumb_path = tmp_path / "1492807.jpg"
        thumb_path.write_bytes(_jpeg_bytes((1024, 512)))
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "slides": {
                        "1492807": {
                            "uri": str(thumb_path),
                            "width": 1024,
                            "height": 512,
                        }
                    }
                }
            )
        )

        monkeypatch.setattr(thumbnail_store.settings, "thumbnail_manifest_uri", str(manifest_path))
        monkeypatch.setattr(thumbnail_store.settings, "thumbnail_manifest_refresh_sec", 300)
        thumbnail_store.manifest_cache.invalidate()

        record = thumbnail_store.get_thumbnail_record("1492807")

        assert record is not None
        assert record.uri == str(thumb_path)
        assert record.width == 1024
        assert record.height == 512

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

    def test_stores_generated_thumbnail_next_to_manifest(self, tmp_path, monkeypatch):
        manifest_path = tmp_path / "manifest.json"
        monkeypatch.setattr(thumbnail_store.settings, "thumbnail_manifest_uri", str(manifest_path))
        thumbnail_store.manifest_cache.invalidate()

        record = thumbnail_store.store_generated_thumbnail("1492807", _jpeg_bytes((640, 320)))

        assert record is not None
        assert record.uri == str(tmp_path / "masters" / "1492807.jpg")
        assert record.width == 640
        assert record.height == 320
        assert (tmp_path / "masters" / "1492807.jpg").exists()

    def test_reads_persisted_generated_thumbnail_without_manifest_entry(self, tmp_path, monkeypatch):
        masters_dir = tmp_path / "masters"
        masters_dir.mkdir()
        thumb_path = masters_dir / "1492807.jpg"
        thumb_path.write_bytes(_jpeg_bytes((320, 160)))
        manifest_path = tmp_path / "manifest.json"
        monkeypatch.setattr(thumbnail_store.settings, "thumbnail_manifest_uri", str(manifest_path))
        thumbnail_store.manifest_cache.invalidate()

        record = thumbnail_store.get_persisted_generated_thumbnail_record("1492807")

        assert record is not None
        assert record.uri == str(thumb_path)
        assert record.width == 320
        assert record.height == 160

    def test_generated_record_cache_is_bounded(self, tmp_path, monkeypatch):
        manifest_path = tmp_path / "manifest.json"
        monkeypatch.setattr(thumbnail_store.settings, "thumbnail_manifest_uri", str(manifest_path))
        monkeypatch.setattr(thumbnail_store.settings, "thumbnail_generated_record_cache_capacity", 1)
        thumbnail_store.manifest_cache.invalidate()

        thumbnail_store.store_generated_thumbnail("first", _jpeg_bytes((100, 50)))
        thumbnail_store.store_generated_thumbnail("second", _jpeg_bytes((200, 100)))

        assert thumbnail_store.get_thumbnail_record("first") is None
        assert thumbnail_store.get_thumbnail_record("second") is not None

    def test_manifest_record_takes_precedence_over_generated_record(self, tmp_path, monkeypatch):
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "slides": {
                        "1492807": {
                            "uri": str(tmp_path / "published.jpg"),
                            "width": 200,
                            "height": 100,
                        }
                    }
                }
            )
        )
        monkeypatch.setattr(thumbnail_store.settings, "thumbnail_manifest_uri", str(manifest_path))
        thumbnail_store.manifest_cache.invalidate()
        thumbnail_store.manifest_cache.register_generated(
            thumbnail_store.ThumbnailRecord(
                image_id="1492807",
                uri=str(tmp_path / "generated.jpg"),
                width=400,
                height=200,
            )
        )

        record = thumbnail_store.get_thumbnail_record("1492807")

        assert record is not None
        assert record.uri == str(tmp_path / "published.jpg")
