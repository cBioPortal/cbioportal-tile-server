import os
import shutil
from dataclasses import dataclass
from typing import Any

import fsspec
from tiffslide import TiffSlide

from .config import settings


def s3_opts() -> dict:
    """fsspec/s3fs options built from canonical AWS env vars."""
    opts: dict = {}
    if settings.aws_endpoint_url:
        opts["endpoint_url"] = settings.aws_endpoint_url
    if settings.aws_access_key_id:
        opts["key"] = settings.aws_access_key_id
    if settings.aws_secret_access_key:
        opts["secret"] = settings.aws_secret_access_key
    return opts


def resolve_s3_location(slide_id: str) -> tuple[str, str, dict]:
    """
    Return (bucket, key, s3_opts) for a slide_id.

    slide_id must be a full s3:// URI as stored in the Databricks inventory table.
    """
    if not slide_id.startswith("s3://"):
        raise FileNotFoundError(f"Slide not found: {slide_id!r} (expected s3:// URI)")
    without_scheme = slide_id[5:]
    bucket, _, key = without_scheme.partition("/")
    if not bucket or not key:
        raise FileNotFoundError(f"Malformed slide URI: {slide_id!r}")
    return bucket, key, s3_opts()


def open_slide(slide_id: str, logger: Any) -> tuple[TiffSlide, Any]:
    """
    Open a TiffSlide and return (slide, fileobj).

    fileobj is the fsspec file handle kept alive alongside the slide.
    When BlockCache is not configured, fileobj is None.
    """
    if slide_id.startswith("s3://"):
        bucket, key, storage_options = resolve_s3_location(slide_id)

        if settings.blockcache_path:
            safe_id = slide_id.replace("/", "_").replace(":", "_").lstrip("_")
            cache_dir = os.path.join(settings.blockcache_path, safe_id)
            os.makedirs(cache_dir, exist_ok=True)

            def _open_with_cache(path: str) -> tuple[TiffSlide, Any]:
                fs = fsspec.filesystem(
                    "blockcache",
                    target_protocol="s3",
                    target_options=storage_options,
                    cache_storage=path,
                    block_size=settings.blockcache_block_size,
                )
                fileobj = fs.open(f"{bucket}/{key}", "rb")
                return TiffSlide(fileobj), fileobj

            slide, fileobj = _open_with_cache(cache_dir)

            if slide.level_count < 2:
                logger.warning(
                    "slide opened with level_count=%d — stale blockcache suspected; clearing cache dir and retrying",
                    slide.level_count,
                )
                try:
                    fileobj.close()
                    slide.close()
                except Exception:
                    pass
                shutil.rmtree(cache_dir, ignore_errors=True)
                os.makedirs(cache_dir, exist_ok=True)
                slide, fileobj = _open_with_cache(cache_dir)
                logger.info("slide re-opened: level_count=%d", slide.level_count)

            return slide, fileobj

        url = f"s3://{bucket}/{key}"
        slide = TiffSlide(url, storage_options=storage_options)
        return slide, None

    local_path = slide_id[7:] if slide_id.startswith("file://") else slide_id
    if not os.path.isabs(local_path):
        raise FileNotFoundError(f"Slide not found: {slide_id!r}")
    if not os.path.exists(local_path):
        raise FileNotFoundError(f"Slide not found: {slide_id!r}")
    slide = TiffSlide(local_path)
    return slide, None


@dataclass
class SlideEntry:
    slide: TiffSlide
    fileobj: Any
    cache_lease: Any = None


def close_entry(entry: SlideEntry) -> None:
    try:
        entry.slide.close()
    except Exception:
        pass
    if entry.fileobj is not None:
        try:
            entry.fileobj.close()
        except Exception:
            pass
    if entry.cache_lease is not None:
        try:
            entry.cache_lease.__exit__(None, None, None)
        except Exception:
            pass
