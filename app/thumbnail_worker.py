"""Process-isolated worker for on-demand thumbnail generation."""

from __future__ import annotations

import argparse
import json
import logging
from contextlib import contextmanager
from io import BytesIO

from .config import settings
from .slide_store import open_slide
from .thumbnail_store import ThumbnailRecord, store_generated_thumbnail
from .tiles import NoSafeThumbnailOverview, get_thumbnail_bytes_with_plan

logger = logging.getLogger("thumbnail-worker")


@contextmanager
def _without_blockcache():
    original = settings.blockcache_path
    settings.blockcache_path = ""
    try:
        yield
    finally:
        settings.blockcache_path = original


def _build_thumbnail_master_bytes(slide, master_size: int) -> bytes:
    try:
        payload, _ = get_thumbnail_bytes_with_plan(slide, master_size, master_size)
        return payload
    except NoSafeThumbnailOverview:
        # This fallback is intentionally confined to the child process. A
        # malformed or undersampled slide must not consume API worker memory.
        image = slide.get_thumbnail((master_size, master_size)).convert("RGB")
        out = BytesIO()
        image.save(out, format="JPEG", quality=settings.jpeg_quality)
        return out.getvalue()


def generate_thumbnail(image_id: str, source_uri: str, master_size: int):
    with _without_blockcache():
        slide, fileobj = open_slide(source_uri, logger)
    try:
        payload = _build_thumbnail_master_bytes(slide, master_size)
    finally:
        try:
            slide.close()
        except Exception:
            pass
        if fileobj is not None:
            try:
                fileobj.close()
            except Exception:
                pass

    record = store_generated_thumbnail(image_id, payload)
    if record is None:
        raise RuntimeError("THUMBNAIL_MANIFEST_URI is required for on-demand generation")
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-id", required=True)
    parser.add_argument("--source-uri", required=True)
    parser.add_argument("--master-size", type=int, default=settings.thumbnail_master_size)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

    record = generate_thumbnail(args.image_id, args.source_uri, max(1, args.master_size))
    print(json.dumps({
        "image_id": record.image_id,
        "uri": record.uri,
        "width": record.width,
        "height": record.height,
        "content_type": record.content_type,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
