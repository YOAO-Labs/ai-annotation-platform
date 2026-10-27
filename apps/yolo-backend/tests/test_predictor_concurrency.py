"""Predictor cancellation and tracker cleanup invariants."""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import MagicMock

import pytest


class _BlockingModel:
    names = {0: "object"}
    device = "cuda:0"

    def __init__(self, started: threading.Event, release: threading.Event) -> None:
        self._started = started
        self._release = release

    def predict(self, *_args, **_kwargs):
        self._started.set()
        assert self._release.wait(timeout=1.0)
        return []


@pytest.mark.asyncio
async def test_cancelled_predict_keeps_borrower_until_executor_finishes(
    monkeypatch,
) -> None:
    import predictor as predictor_module
    from model_pool import ModelPool, PoolBusyError
    from predictor import YoloPredictor
    from schemas import Context

    started = threading.Event()
    release = threading.Event()
    model = _BlockingModel(started, release)
    pool = ModelPool(1, lambda *_args: model, lambda: None, build_timeout=1.0)
    await pool.warmup("detection", "yolo11", "s")
    monkeypatch.setattr(
        predictor_module,
        "fetch_image",
        lambda _path: MagicMock(size=(32, 32)),
    )
    ctx = Context(type="detection", variants={"series": "yolo11", "size": "s"})

    task = asyncio.create_task(YoloPredictor(pool).predict_one("image.jpg", ctx))
    assert await asyncio.wait_for(asyncio.to_thread(started.wait), timeout=1.0)
    task.cancel()
    await asyncio.sleep(0)

    assert (await pool.snapshot())["borrowers"] == 1
    with pytest.raises(PoolBusyError):
        await pool.unload_all(reason="manual")

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await pool.snapshot())["borrowers"] == 0


class _TrackerStream:
    def __init__(self, *, fail_after_first: bool = False) -> None:
        self.closed = False
        self._index = 0
        self._fail_after_first = fail_after_first

    def __iter__(self):
        return self

    def __next__(self):
        if self._index == 0:
            self._index += 1
            return object()
        if self._fail_after_first:
            raise RuntimeError("decoder failed")
        self._index += 1
        return object()

    def close(self) -> None:
        self.closed = True


class _TrackerModel:
    device = "cuda:0"

    def __init__(self, stream: _TrackerStream) -> None:
        self._stream = stream

    def track(self, **_kwargs):
        return self._stream


class _BlockingTrackerStream:
    def __init__(self, started: threading.Event, release: threading.Event) -> None:
        self._started = started
        self._release = release
        self._yielded = False
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        if self._yielded:
            raise StopIteration
        self._started.set()
        assert self._release.wait(timeout=1.0)
        self._yielded = True
        return object()

    def close(self) -> None:
        self.closed = True


def _tracker_context():
    from schemas import Context

    return Context(type="tracker", variants={"series": "yolo11", "size": "s"})


def test_tracker_closes_stream_when_frame_limit_truncates(monkeypatch) -> None:
    import predictor as predictor_module
    from predictor import YoloPredictor

    stream = _TrackerStream()
    monkeypatch.setenv("YOLO_TRACKER_MAX_FRAMES", "1")
    monkeypatch.setattr(
        predictor_module, "_fetch_video", lambda _path: ("video.mp4", False)
    )
    monkeypatch.setattr(
        predictor_module, "_accumulate_track_frame", lambda *_args: None
    )
    monkeypatch.setattr(predictor_module, "record_inference", lambda *_args: None)

    YoloPredictor._predict_tracker_sync(
        _TrackerModel(stream), "video.mp4", _tracker_context()
    )

    assert stream.closed is True


def test_tracker_closes_stream_when_iteration_fails(monkeypatch) -> None:
    import predictor as predictor_module
    from predictor import YoloPredictor

    stream = _TrackerStream(fail_after_first=True)
    monkeypatch.setenv("YOLO_TRACKER_MAX_FRAMES", "10")
    monkeypatch.setattr(
        predictor_module, "_fetch_video", lambda _path: ("video.mp4", False)
    )
    monkeypatch.setattr(
        predictor_module, "_accumulate_track_frame", lambda *_args: None
    )
    monkeypatch.setattr(predictor_module, "record_inference", lambda *_args: None)

    with pytest.raises(RuntimeError, match="decoder failed"):
        YoloPredictor._predict_tracker_sync(
            _TrackerModel(stream), "video.mp4", _tracker_context()
        )

    assert stream.closed is True


@pytest.mark.asyncio
async def test_cancelled_tracker_defers_stream_and_temp_file_cleanup(
    monkeypatch,
    tmp_path,
) -> None:
    import predictor as predictor_module
    from model_pool import ModelPool
    from predictor import YoloPredictor

    started = threading.Event()
    release = threading.Event()
    stream = _BlockingTrackerStream(started, release)
    model = _TrackerModel(stream)
    pool = ModelPool(1, lambda *_args: model, lambda: None, build_timeout=1.0)
    await pool.warmup("tracker", "yolo11", "s")
    temp_video = tmp_path / "downloaded.mp4"
    temp_video.write_bytes(b"video")
    monkeypatch.setattr(
        predictor_module,
        "_fetch_video",
        lambda _path: (str(temp_video), True),
    )
    monkeypatch.setattr(
        predictor_module, "_accumulate_track_frame", lambda *_args: None
    )
    monkeypatch.setattr(predictor_module, "record_inference", lambda *_args: None)

    task = asyncio.create_task(
        YoloPredictor(pool).predict_one("remote-video", _tracker_context())
    )
    assert await asyncio.wait_for(asyncio.to_thread(started.wait), timeout=1.0)
    task.cancel()
    await asyncio.sleep(0)

    assert stream.closed is False
    assert temp_video.exists()
    assert (await pool.snapshot())["borrowers"] == 1

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed is True
    assert not temp_video.exists()
    assert (await pool.snapshot())["borrowers"] == 0
