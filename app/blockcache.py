"""Bounded, cross-worker housekeeping for the local fsspec block cache."""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import settings

try:  # pragma: no cover - exercised on the Linux deployment image
    import fcntl
except ImportError:  # pragma: no cover - Windows is not a deployment target
    fcntl = None

logger = logging.getLogger(__name__)


class BlockCacheManager:
    def __init__(self, root: str, max_bytes: int, prune_interval_seconds: int) -> None:
        self.root = Path(root) if root else None
        self.max_bytes = max(0, max_bytes)
        self.prune_interval_seconds = max(1, prune_interval_seconds)
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.root is not None and self.max_bytes > 0

    def _locks_dir(self) -> Path:
        assert self.root is not None
        path = self.root / ".locks"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _lock_path(self, cache_dir: str | Path) -> Path:
        digest = hashlib.sha256(str(cache_dir).encode()).hexdigest()
        return self._locks_dir() / f"{digest}.lock"

    @contextmanager
    def lease(self, cache_dir: str | Path) -> Iterator[None]:
        """Hold a shared lock while a cached slide handle may use its blocks."""
        if self.root is None or fcntl is None:
            yield
            return
        lock_path = self._lock_path(cache_dir)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def touch(self, cache_dir: str | Path) -> None:
        try:
            os.utime(cache_dir, None)
        except OSError:
            pass

    @staticmethod
    def _allocated_bytes(path: Path) -> int:
        total = 0
        try:
            for child in path.rglob("*"):
                try:
                    total += child.stat().st_blocks * 512
                except OSError:
                    continue
        except OSError:
            return 0
        return total

    def prune(self, force: bool = False) -> int:
        """Prune oldest unlocked slide directories and return removed bytes."""
        if not self.enabled or fcntl is None:
            return 0
        assert self.root is not None
        self.root.mkdir(parents=True, exist_ok=True)
        global_lock_path = self._locks_dir() / ".prune.lock"
        fd = os.open(global_lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 0

            with self._lock:
                candidates: list[tuple[float, int, Path]] = []
                total = 0
                for path in self.root.iterdir():
                    if not path.is_dir() or path.name == ".locks":
                        continue
                    size = self._allocated_bytes(path)
                    total += size
                    try:
                        modified = path.stat().st_mtime
                    except OSError:
                        modified = 0.0
                    candidates.append((modified, size, path))

                if not force and total <= self.max_bytes:
                    return 0
                target = int(self.max_bytes * 0.8)
                removed = 0
                for _, size, path in sorted(candidates):
                    if total <= target:
                        break
                    slide_lock = os.open(self._lock_path(path), os.O_CREAT | os.O_RDWR, 0o600)
                    try:
                        try:
                            fcntl.flock(slide_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            continue
                        shutil.rmtree(path, ignore_errors=True)
                        total -= size
                        removed += size
                    finally:
                        fcntl.flock(slide_lock, fcntl.LOCK_UN)
                        os.close(slide_lock)
                if removed:
                    logger.info("Pruned %d bytes from block cache", removed)
                return removed
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def purge(self, cache_dir: str | Path) -> bool:
        """Remove one unlocked cache directory under an exclusive lease."""
        if self.root is None or fcntl is None:
            return False
        path = Path(cache_dir)
        if path.parent != self.root:
            raise ValueError("cache directory must be a direct child of the cache root")
        self.root.mkdir(parents=True, exist_ok=True)
        lock_path = self._lock_path(path)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            existed = path.exists()
            shutil.rmtree(path, ignore_errors=True)
            return existed
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


_manager: BlockCacheManager | None = None
_manager_signature: tuple[str, int, int] | None = None
_manager_lock = threading.Lock()


def get_blockcache_manager() -> BlockCacheManager:
    global _manager, _manager_signature
    signature = (
        settings.blockcache_path,
        settings.blockcache_max_bytes,
        settings.blockcache_prune_interval_seconds,
    )
    with _manager_lock:
        if _manager is None or _manager_signature != signature:
            _manager = BlockCacheManager(*signature)
            _manager_signature = signature
        return _manager


def cache_directory_for_slide(slide_id: str) -> Path | None:
    """Return a process-private cache directory for an S3 slide.

    fsspec's block-cache metadata is mutable and is not a multi-process store.
    Include the worker PID and configured block size so Gunicorn workers never
    concurrently update the same metadata or reopen it with a different size.
    """
    if not settings.blockcache_path or not slide_id.startswith("s3://"):
        return None
    digest = hashlib.sha256(slide_id.encode("utf-8")).hexdigest()
    name = f"b{settings.blockcache_block_size}-p{os.getpid()}-{digest}"
    return Path(settings.blockcache_path) / name


def cache_lease_for_slide(slide_id: str):
    cache_dir = cache_directory_for_slide(slide_id)
    if cache_dir is None:
        return None
    return get_blockcache_manager().lease(cache_dir)


def touch_slide_cache(slide_id: str) -> None:
    cache_dir = cache_directory_for_slide(slide_id)
    if cache_dir is None:
        return
    get_blockcache_manager().touch(cache_dir)


def purge_slide_cache(slide_id: str) -> bool:
    cache_dir = cache_directory_for_slide(slide_id)
    if cache_dir is None:
        return False
    return get_blockcache_manager().purge(cache_dir)
