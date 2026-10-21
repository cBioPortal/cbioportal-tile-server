import os
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

import fsspec
from tifffile import TiffFile, TiffPageSeries
from tiffslide import TiffSlide

from .blockcache import cache_directory_for_slide
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
    opts["config_kwargs"] = {
        "connect_timeout": max(0.1, settings.slide_s3_connect_timeout_seconds),
        "read_timeout": max(0.1, settings.slide_s3_read_timeout_seconds),
        "max_pool_connections": max(1, settings.slide_s3_max_connections),
        "retries": {
            "mode": "standard",
            "max_attempts": max(1, settings.slide_s3_max_attempts),
        },
    }
    return opts


_URI_EXTENSIONS = {
    "source": {".svs", ".tif", ".tiff", ".ndpi", ".mrxs", ".scn"},
    "thumbnail": {".jpg", ".jpeg", ".png"},
}


class UnsafeSourceUri(ValueError):
    """A capability's source URI is outside the deployment's approved objects."""


def check_source_uri(uri: str, *, kind: str, prefixes: Iterable[str] = ()) -> None:
    """
    Accept only a plain s3:// or file:// object under an approved prefix.

    The URI comes from the sealed capability and never leaves this server, so
    the approved prefixes, not its text, are the boundary. Messages never echo it.
    """
    if kind not in _URI_EXTENSIONS:
        raise UnsafeSourceUri(f"unknown URI kind: {kind}")
    try:
        parsed = urlsplit(uri)
    except ValueError as error:
        raise UnsafeSourceUri(f"malformed {kind} URI") from error
    scheme = parsed.scheme.lower()
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise UnsafeSourceUri(f"unsafe {kind} URI")
    if scheme not in {"s3", "file"}:
        raise UnsafeSourceUri(f"unsupported {kind} URI scheme")
    path = unquote(unquote(parsed.path))
    if (
        not path
        or path.endswith("/")
        or (scheme == "s3" and not parsed.netloc)
        or (scheme == "file" and (parsed.netloc not in {"", "localhost"} or not path.startswith("/")))
    ):
        raise UnsafeSourceUri(f"malformed {kind} URI")
    if any(segment in {".", ".."} for segment in path.split("/")):
        raise UnsafeSourceUri(f"unsafe {kind} URI path")
    normalized = uri.rstrip("/")
    if prefixes and not any(normalized.startswith(prefix.rstrip("/") + "/") for prefix in prefixes):
        raise UnsafeSourceUri(f"unapproved {kind} URI prefix")
    stem, dot, extension = path.rsplit("/", 1)[-1].rpartition(".")
    if not dot or not stem or f".{extension.lower()}" not in _URI_EXTENSIONS[kind]:
        raise UnsafeSourceUri(f"{kind} URI is not an image file")


def resolve_s3_location(slide_id: str) -> tuple[str, str, dict]:
    """
    Return (bucket, key, s3_opts) for a source URL.

    The source URL must be a full s3:// URI opened from the sealed capability.
    """
    if not slide_id.startswith("s3://"):
        raise FileNotFoundError("Slide not found (expected s3:// URI)")
    without_scheme = slide_id[5:]
    bucket, _, key = without_scheme.partition("/")
    if not bucket or not key:
        raise FileNotFoundError("Malformed slide URI")
    return bucket, key, s3_opts()


def _keep_first_focal_plane(tf: TiffFile) -> None:
    """
    Reduce a Z-stack SVS baseline series to its first focal plane, in place.

    tifffile groups an Aperio Z-stack's pages into one ZYXS series, which
    tiffslide refuses to open (it reads only 2-D and 3-D series). The first
    page of each level is the nominal-focus plane, the one without OffsetZ.
    This must run before the slide's properties are first read.
    """
    series = tf.series
    baseline = series[0]
    if baseline.ndim != 4 or not baseline.axes.startswith("Z"):
        return

    def plane(level: TiffPageSeries) -> TiffPageSeries:
        page = level.pages[0]
        return TiffPageSeries(
            [page], page.shape, page.dtype, page.axes, parent=tf, name=level.name, kind=level.kind
        )

    first = plane(baseline)
    first.levels.extend(plane(level) for level in baseline.levels[1:])
    series[0] = first


def _tiff_slide(source: Any, **kwargs: Any) -> TiffSlide:
    slide = TiffSlide(source, **kwargs)
    try:
        _keep_first_focal_plane(slide.ts_tifffile)
    except BaseException:
        slide.close()
        raise
    return slide


def open_slide(
    slide_id: str,
    logger: Any,
    cache_identity: str | None = None,
    use_block_cache: bool = True,
) -> tuple[TiffSlide, Any]:
    """
    Open a TiffSlide and return (slide, fileobj).

    fileobj is the fsspec file handle kept alive alongside the slide.
    When BlockCache is not configured, fileobj is None.
    """
    if slide_id.startswith("s3://"):
        bucket, key, storage_options = resolve_s3_location(slide_id)

        cache_dir = cache_directory_for_slide(slide_id, cache_identity) if use_block_cache else None
        if cache_dir is not None:
            cache_dir = os.fspath(cache_dir)
            os.makedirs(cache_dir, exist_ok=True)

            def _open_with_cache(path: str) -> tuple[TiffSlide, Any]:
                fs = fsspec.filesystem(
                    "blockcache",
                    target_protocol="s3",
                    target_options=storage_options,
                    cache_storage=path,
                )
                # CachingFileSystem accepts block_size on open(); passing it
                # to filesystem() is swallowed by **kwargs and leaves the
                # fsspec default (currently 50 MiB) in effect.
                fileobj = fs.open(
                    f"{bucket}/{key}",
                    "rb",
                    block_size=settings.blockcache_block_size,
                )
                try:
                    return _tiff_slide(fileobj), fileobj
                except BaseException:
                    fileobj.close()
                    raise

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
        slide = _tiff_slide(url, storage_options=storage_options)
        return slide, None

    local_path = slide_id[7:] if slide_id.startswith("file://") else slide_id
    if not os.path.isabs(local_path):
        raise FileNotFoundError(f"Slide not found: {slide_id!r}")
    if not os.path.exists(local_path):
        raise FileNotFoundError(f"Slide not found: {slide_id!r}")
    slide = _tiff_slide(local_path)
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
