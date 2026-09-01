"""
TiffSlide source cache.

Keeps up to MAX_OPEN_SLIDES TiffSlide objects open in an LRU cache so repeated
tile requests for the same slide don't pay the cost of re-opening from ECS.
Thread-safe via a lock.

When BLOCKCACHE_PATH is set, each slide is opened through an fsspec BlockCache
filesystem that stores fixed-size blocks (default 8 MB) on local NVMe.  After
the first read of a block, subsequent reads come from disk rather than ECS —
this turns the p95 ~160 ms ECS latency into <1 ms NVMe reads.
"""

import logging
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator, TypeVar

from tiffslide import TiffSlide

from .config import settings
from .blockcache import cache_lease_for_slide, purge_slide_cache, touch_slide_cache
from .metrics import SLIDE_OPEN_ERRORS, SLIDE_OPEN_SECONDS
from .slide_store import SlideEntry as _Entry
from .slide_store import close_entry as _close_entry
from .slide_store import open_slide as _open_slide

log = logging.getLogger(__name__)
T = TypeVar("T")


def _resolve_s3_location(slide_id: str) -> tuple[str, str, dict]:
    """
    Return (bucket, key, s3_opts) for a source URL.

    The URL is supplied by cBioPortal, e.g.
    "s3://mskmind-bkt/reef-slides/3735444.svs".
    """
    if not slide_id.startswith("s3://"):
        raise FileNotFoundError(f"Slide not found: {slide_id!r} (expected s3:// URI)")
    without_scheme = slide_id[5:]
    bucket, _, key = without_scheme.partition("/")
    if not bucket or not key:
        raise FileNotFoundError(f"Malformed slide URI: {slide_id!r}")

    opts: dict = {}
    if settings.aws_endpoint_url:
        opts["endpoint_url"] = settings.aws_endpoint_url
    if settings.aws_access_key_id:
        opts["key"] = settings.aws_access_key_id
    if settings.aws_secret_access_key:
        opts["secret"] = settings.aws_secret_access_key
    return bucket, key, opts


@dataclass
class _CacheEntry:
    entry: _Entry
    active: bool = False


@dataclass
class _OpenState:
    event: threading.Event
    error: BaseException | None = None


class SlideCache:
    """Thread-safe LRU cache with exclusive leases for open TiffSlide objects."""

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        if capacity < 1:
            raise ValueError("slide cache capacity must be at least one")
        self._capacity = capacity
        self._cache: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._condition = threading.Condition()
        # Opening state coalesces cold opens without leaving waiters blocked
        # when the opener fails.
        self._opening: dict[str, _OpenState] = {}
        self._repairing: set[str] = set()

    @staticmethod
    def _cache_key(slide_id: str, cache_identity: str | None) -> str:
        return cache_identity or slide_id

    def _acquire(self, slide_id: str, cache_identity: str | None = None) -> _CacheEntry:
        cache_key = self._cache_key(slide_id, cache_identity)
        while True:
            opener: _OpenState | None = None
            evicted: _CacheEntry | None = None
            is_owner = False
            with self._condition:
                if cache_key in self._repairing:
                    self._condition.wait()
                    continue
                cached = self._cache.get(cache_key)
                if cached is not None:
                    if cached.active:
                        self._condition.wait()
                        continue
                    cached.active = True
                    self._cache.move_to_end(cache_key)
                    return cached

                opener = self._opening.get(cache_key)
                if opener is not None:
                    # Wait outside the condition so the opener can publish its
                    # result and wake all waiters.
                    pass
                else:
                    # Capacity is strict: do not close a slide that another
                    # request may still be using. Wait until an idle entry is
                    # available before starting a new open.
                    if len(self._cache) >= self._capacity:
                        idle_key = next(
                            (key for key, candidate in self._cache.items() if not candidate.active),
                            None,
                        )
                        if idle_key is None:
                            self._condition.wait()
                            continue
                        _, evicted = self._cache.popitem(last=False)

                    opener = _OpenState(event=threading.Event())
                    self._opening[cache_key] = opener
                    is_owner = True

            if opener is not None and not is_owner:
                opener.event.wait()
                if opener.error is not None:
                    raise opener.error
                continue

            if evicted is not None:
                _close_entry(evicted.entry)

            cache_lease = cache_lease_for_slide(slide_id, cache_identity)
            started = time.perf_counter()
            try:
                try:
                    if cache_lease is not None:
                        cache_lease.__enter__()
                    if cache_identity:
                        slide, fileobj = _open_slide(
                            slide_id, log, cache_identity=cache_identity
                        )
                    else:
                        slide, fileobj = _open_slide(slide_id, log)
                except BaseException as exc:
                    SLIDE_OPEN_ERRORS.labels(error_type=type(exc).__name__).inc()
                    if cache_lease is not None:
                        cache_lease.__exit__(type(exc), exc, exc.__traceback__)
                    with self._condition:
                        state = self._opening.pop(cache_key, None)
                        if state is not None:
                            state.error = exc
                            state.event.set()
                        self._condition.notify_all()
                    raise

                with self._condition:
                    state = self._opening.pop(cache_key, None)
                    cached = _CacheEntry(
                        _Entry(slide=slide, fileobj=fileobj, cache_lease=cache_lease),
                        active=True,
                    )
                    self._cache[cache_key] = cached
                    touch_slide_cache(slide_id, cache_identity)
                    self._condition.notify_all()
                    if state is not None:
                        state.event.set()
                    return cached
            finally:
                SLIDE_OPEN_SECONDS.observe(time.perf_counter() - started)

    def _release(self, slide_id: str, entry: _CacheEntry, cache_identity: str | None = None) -> None:
        cache_key = self._cache_key(slide_id, cache_identity)
        with self._condition:
            current = self._cache.get(cache_key)
            if current is entry:
                entry.active = False
                self._cache.move_to_end(cache_key)
                touch_slide_cache(slide_id, cache_identity)
            self._condition.notify_all()

    @contextmanager
    def lease(self, slide_id: str, cache_identity: str | None = None) -> Iterator[TiffSlide]:
        cached = self._acquire(slide_id, cache_identity)
        try:
            yield cached.entry.slide
        finally:
            self._release(slide_id, cached, cache_identity)

    def run(
        self,
        slide_id: str,
        operation: Callable[..., T],
        *args,
        cache_identity: str | None = None,
    ) -> T:
        with self.lease(slide_id, cache_identity) as slide:
            return operation(slide, *args)

    def get(self, slide_id: str, cache_identity: str | None = None) -> TiffSlide:
        """Compatibility helper; callers performing work should use ``run``."""
        cached = self._acquire(slide_id, cache_identity)
        self._release(slide_id, cached, cache_identity)
        return cached.entry.slide

    def invalidate(self, slide_id: str, cache_identity: str | None = None) -> bool:
        cache_key = self._cache_key(slide_id, cache_identity)
        entry = None
        with self._condition:
            cached = self._cache.get(cache_key)
            if cached is not None and not cached.active:
                entry = self._cache.pop(cache_key)
                self._condition.notify_all()
        if entry is not None:
            _close_entry(entry.entry)
            return True
        return False

    def repair(self, slide_id: str, cache_identity: str | None = None) -> bool:
        """Evict one idle handle and its worker-private block cache."""
        cache_key = self._cache_key(slide_id, cache_identity)
        with self._condition:
            while cache_key in self._opening:
                self._condition.wait()
            cached = self._cache.get(cache_key)
            if cached is not None and cached.active:
                return False
            self._repairing.add(cache_key)
            entry = self._cache.pop(cache_key, None)
            self._condition.notify_all()
        try:
            if entry is not None:
                _close_entry(entry.entry)
            purge_slide_cache(slide_id, cache_identity)
            return True
        finally:
            with self._condition:
                self._repairing.discard(cache_key)
                self._condition.notify_all()

    def run_uncached(self, slide_id: str, operation: Callable[..., T], *args) -> T:
        """Run one operation with a fresh direct object-store reader."""
        slide, fileobj = _open_slide(slide_id, log, use_block_cache=False)
        try:
            return operation(slide, *args)
        finally:
            _close_entry(_Entry(slide=slide, fileobj=fileobj))

    def close_all(self) -> None:
        with self._condition:
            entries = list(self._cache.values())
            self._cache.clear()
            self._condition.notify_all()
        for entry in entries:
            _close_entry(entry.entry)
