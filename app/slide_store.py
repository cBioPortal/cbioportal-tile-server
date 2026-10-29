import os
import re
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

import fsspec
from s3fs.utils import FileExpired
from tifffile import TiffFile, TiffPageSeries
from tiffslide import TiffSlide

from .blockcache import cache_directory_for_slide
from .config import settings

_OFFSET_Z = re.compile(r"\|\s*OffsetZ\s*=\s*([-+]?[\d.]+)")


def s3_opts() -> dict:
    """fsspec/s3fs options built from canonical AWS env vars."""
    opts: dict = {}
    if settings.aws_endpoint_url:
        opts["endpoint_url"] = settings.aws_endpoint_url
    if settings.aws_access_key_id:
        opts["key"] = settings.aws_access_key_id
    if settings.aws_secret_access_key:
        opts["secret"] = settings.aws_secret_access_key
    # Compare every range read's ETag with the one the file was opened at, on
    # the client, so a source replaced in place fails with FileExpired whether
    # or not the object store honours If-Match.
    opts["local_expiry_check"] = True
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


def source_etag(slide_id: str) -> str | None:
    """
    The object's current ETag, read with a fresh HEAD (None for local files).

    Sources can be rewritten in place (an SVS cutover keeps the key but moves
    every tile), so a cached handle or cached block is only valid for the ETag
    it was opened against.
    """
    if not slide_id.startswith("s3://"):
        return None
    bucket, key, storage_options = resolve_s3_location(slide_id)
    fs = fsspec.filesystem("s3", **storage_options)
    etag = fs.info(f"{bucket}/{key}", refresh=True).get("ETag")
    return str(etag) if etag else None


def is_source_changed_error(exc: BaseException) -> bool:
    """Whether a read failed because the object changed after it was opened."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, FileExpired):
            return True
        response = getattr(current, "response", None)
        if isinstance(response, dict):
            error = response.get("Error", {}) or {}
            status = (response.get("ResponseMetadata", {}) or {}).get("HTTPStatusCode")
            if error.get("Code") in ("PreconditionFailed", "412") or status == 412:
                return True
        current = current.__cause__ or current.__context__
    return False


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


def _is_nominal_focus(page: Any) -> bool:
    match = _OFFSET_Z.search(page.description or "")
    if match is None:
        return True
    try:
        return float(match.group(1)) == 0
    except ValueError:
        return False


def _keep_nominal_focus_plane(tf: TiffFile) -> int:
    """
    Reduce a Z-stack SVS baseline series to its nominal-focus plane, in place.

    Returns the number of focal planes the slide had, or 0 if it was not a
    Z-stack.

    tifffile groups an Aperio Z-stack's pages into one ZYXS series, which
    tiffslide refuses to open (it reads only 2-D and 3-D series). Each level
    keeps its nominal-focus page: the one whose description has no OffsetZ or
    OffsetZ = 0, else its first page. This must run before the slide's
    properties are first read.
    """
    series = tf.series
    baseline = series[0]
    if baseline.ndim != 4 or not baseline.axes.startswith("Z"):
        return 0

    def plane(level: TiffPageSeries) -> TiffPageSeries:
        page = next((p for p in level.pages if _is_nominal_focus(p)), level.pages[0])
        return TiffPageSeries(
            [page], page.shape, page.dtype, page.axes, parent=tf, name=level.name, kind=level.kind
        )

    first = plane(baseline)
    first.levels.extend(plane(level) for level in baseline.levels[1:])
    series[0] = first
    return int(baseline.shape[0])


def _tiff_slide(source: Any, logger: Any, **kwargs: Any) -> TiffSlide:
    slide = TiffSlide(source, **kwargs)
    try:
        planes = _keep_nominal_focus_plane(slide.ts_tifffile)
    except BaseException:
        slide.close()
        raise
    if planes:
        # Grep for "zstack_flattened" to count Z-stack slides: they render
        # without error now, so this log line is the only sign of them.
        logger.warning("zstack_flattened: serving the nominal-focus plane of %d focal planes", planes)
    return slide


def open_slide(
    slide_id: str,
    logger: Any,
    use_block_cache: bool = True,
    etag: str | None = None,
) -> tuple[TiffSlide, Any]:
    """
    Open a TiffSlide and return (slide, fileobj).

    fileobj is the fsspec file handle kept alive alongside the slide.
    When BlockCache is not configured, fileobj is None.

    The block cache is namespaced by etag, the object's ETag when the caller
    checked it. Reads of an object replaced after opening fail with
    FileExpired (s3fs compares each response's ETag with the one it opened)
    instead of mixing the old file's offsets with the new file's bytes.
    """
    if slide_id.startswith("s3://"):
        bucket, key, storage_options = resolve_s3_location(slide_id)

        cache_dir = cache_directory_for_slide(slide_id, etag) if use_block_cache else None
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
                    return _tiff_slide(fileobj, logger), fileobj
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
        slide = _tiff_slide(url, logger, storage_options=storage_options)
        return slide, None

    local_path = slide_id[7:] if slide_id.startswith("file://") else slide_id
    if not os.path.isabs(local_path):
        raise FileNotFoundError(f"Slide not found: {slide_id!r}")
    if not os.path.exists(local_path):
        raise FileNotFoundError(f"Slide not found: {slide_id!r}")
    slide = _tiff_slide(local_path, logger)
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
