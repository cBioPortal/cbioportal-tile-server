import os
from unittest.mock import patch

from app import blockcache
from app.blockcache import BlockCacheManager


def test_prune_removes_oldest_directories_until_below_limit(tmp_path):
    manager = BlockCacheManager(str(tmp_path), max_bytes=8192, prune_interval_seconds=60)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "blocks").write_bytes(b"x" * 4096)
    (second / "blocks").write_bytes(b"y" * 4096)
    os.utime(first, (1, 1))

    removed = manager.prune(force=True)

    assert removed > 0
    assert len([path for path in (first, second) if path.exists()]) == 1


def test_prune_keeps_a_locked_slide_directory(tmp_path):
    manager = BlockCacheManager(str(tmp_path), max_bytes=1, prune_interval_seconds=60)
    locked = tmp_path / "locked"
    other = tmp_path / "other"
    locked.mkdir()
    other.mkdir()
    (locked / "blocks").write_bytes(b"x" * 4096)
    (other / "blocks").write_bytes(b"y" * 4096)

    with manager.lease(locked):
        manager.prune(force=True)

    assert locked.exists()
    assert not other.exists()


def test_slide_cache_directory_is_private_to_worker_and_block_size(tmp_path, monkeypatch):
    monkeypatch.setattr(blockcache.settings, "blockcache_path", str(tmp_path))
    monkeypatch.setattr(blockcache.settings, "blockcache_block_size", 1_048_576)
    source = "s3://bucket/slide.svs"

    with patch.object(blockcache.os, "getpid", return_value=101):
        first = blockcache.cache_directory_for_slide(source)
    with patch.object(blockcache.os, "getpid", return_value=202):
        second = blockcache.cache_directory_for_slide(source)

    assert first != second
    assert first.parent == tmp_path
    assert second.parent == tmp_path
    assert first.name.startswith("b1048576-p101-")
    assert second.name.startswith("b1048576-p202-")


def test_source_fingerprint_uses_a_distinct_cache_namespace(tmp_path, monkeypatch):
    monkeypatch.setattr(blockcache.settings, "blockcache_path", str(tmp_path))
    monkeypatch.setattr(blockcache.settings, "blockcache_block_size", 1_048_576)
    source = "s3://bucket/slide.svs"

    first = blockcache.cache_directory_for_slide(source, "a" * 64)
    second = blockcache.cache_directory_for_slide(source, "b" * 64)

    assert first != second
    assert first.parent == tmp_path
    assert second.parent == tmp_path


def test_purge_removes_one_cache_directory(tmp_path):
    manager = BlockCacheManager(str(tmp_path), max_bytes=8192, prune_interval_seconds=60)
    cache_dir = tmp_path / "slide-cache"
    cache_dir.mkdir()
    (cache_dir / "blocks").write_bytes(b"cached")

    assert manager.purge(cache_dir) is True
    assert not cache_dir.exists()
    assert manager.purge(cache_dir) is False
