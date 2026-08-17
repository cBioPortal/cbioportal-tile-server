import threading
import time
from unittest.mock import MagicMock, patch

from app.slides import SlideCache


def test_failed_open_wakes_all_waiters_and_allows_retry():
    entered = threading.Event()
    release = threading.Event()
    errors = []

    def fail_open(slide_id, logger):
        entered.set()
        release.wait(1)
        raise OSError("open failed")

    cache = SlideCache(1)

    def run():
        try:
            cache.run("slide", lambda slide: slide)
        except Exception as exc:  # noqa: BLE001 - assert the propagated failure
            errors.append(type(exc))

    with patch("app.slides._open_slide", side_effect=fail_open):
        first = threading.Thread(target=run, daemon=True)
        second = threading.Thread(target=run, daemon=True)
        first.start()
        assert entered.wait(1)
        second.start()
        release.set()
        first.join(1)
        second.join(1)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == [OSError, OSError]


def test_same_slide_operations_are_serialized():
    active = 0
    peak = 0
    lock = threading.Lock()
    entered = threading.Event()

    def open_slide(slide_id, logger):
        return MagicMock(), None

    def operation(slide):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            entered.set()
        time.sleep(0.02)
        with lock:
            active -= 1

    cache = SlideCache(1)
    with patch("app.slides._open_slide", side_effect=open_slide):
        first = threading.Thread(target=cache.run, args=("same", operation))
        second = threading.Thread(target=cache.run, args=("same", operation))
        first.start()
        assert entered.wait(1)
        second.start()
        threads = [first, second]
        for thread in threads:
            thread.join(1)

    assert all(not thread.is_alive() for thread in threads)
    assert peak == 1


def test_concurrent_cold_open_calls_opener_once():
    entered = threading.Event()
    release = threading.Event()
    opened = []

    def open_slide(slide_id, logger):
        opened.append(slide_id)
        entered.set()
        release.wait(1)
        return MagicMock(), None

    cache = SlideCache(1)
    with patch("app.slides._open_slide", side_effect=open_slide):
        first = threading.Thread(target=lambda: cache.run("same", lambda slide: None))
        second = threading.Thread(target=lambda: cache.run("same", lambda slide: None))
        first.start()
        assert entered.wait(1)
        second.start()
        time.sleep(0.03)
        assert opened == ["same"]
        release.set()
        first.join(1)
        second.join(1)

    assert not first.is_alive()
    assert not second.is_alive()
    assert opened == ["same"]


def test_capacity_one_never_closes_an_active_slide():
    opened = {}
    started = threading.Event()
    release = threading.Event()
    second_done = threading.Event()

    def open_slide(slide_id, logger):
        slide = MagicMock(name=slide_id)
        opened[slide_id] = slide
        return slide, None

    def hold(slide):
        started.set()
        release.wait(1)

    cache = SlideCache(1)
    with patch("app.slides._open_slide", side_effect=open_slide):
        first = threading.Thread(target=cache.run, args=("a", hold))
        first.start()
        assert started.wait(1)

        second = threading.Thread(
            target=lambda: (cache.run("b", lambda slide: second_done.set()))
        )
        second.start()
        time.sleep(0.03)
        assert not second_done.is_set()
        assert not opened["a"].close.called

        release.set()
        first.join(1)
        second.join(1)

    assert second_done.is_set()
    assert opened["a"].close.called
