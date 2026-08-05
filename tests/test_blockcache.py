import os

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
