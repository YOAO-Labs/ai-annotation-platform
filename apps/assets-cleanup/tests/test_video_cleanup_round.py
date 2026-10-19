"""A full cleanup poll visits independent lanes after recoverable lane errors."""

import asyncio
import threading
from uuid import uuid4

import pytest

from aap_assets_cleanup import worker


@pytest.mark.asyncio
async def test_round_continues_through_all_lanes_and_counts_failures(monkeypatch):
    events = []

    async def fail_outbox(*_args, **_kwargs):
        events.append("outbox")
        raise OSError("outbox unavailable")

    async def image(*_args, **_kwargs):
        events.append("image")
        return {"completed": 2, "failed": 1, "batches": 1}

    async def fail_images(*_args, **_kwargs):
        events.append("accepted_image")
        raise OSError("image input storage unavailable")

    async def video(*_args, **_kwargs):
        events.append("accepted_video")
        return 3

    monkeypatch.setattr(worker, "drain_pending", fail_outbox)
    monkeypatch.setattr(worker, "drain_image_garbage", image)
    monkeypatch.setattr(worker, "collect_inputs_before_stop", fail_images)
    monkeypatch.setattr(worker, "collect_video_inputs_before_stop", video)

    result = await worker.run_cleanup_round(
        object(),
        object(),
        bucket="media-cache",
        page_size=10,
        worker_id=uuid4(),
        input_collector=object(),
        video_collector=object(),
        stop=asyncio.Event(),
        storage_stop=threading.Event(),
    )

    assert events == ["outbox", "image", "accepted_image", "accepted_video"]
    assert result == {"completed": 5, "failed": 3, "lane_failures": 2}


@pytest.mark.asyncio
async def test_round_stops_starting_new_lanes_after_shutdown(monkeypatch):
    stop = asyncio.Event()
    events = []

    async def stop_outbox(*_args, **_kwargs):
        events.append("outbox")
        stop.set()
        return {"completed": 0, "failed": 0, "pages": 0}

    async def unexpected(*_args, **_kwargs):
        events.append("later lane")
        return 1

    monkeypatch.setattr(worker, "drain_pending", stop_outbox)
    monkeypatch.setattr(worker, "drain_image_garbage", unexpected)
    monkeypatch.setattr(worker, "collect_inputs_before_stop", unexpected)
    monkeypatch.setattr(worker, "collect_video_inputs_before_stop", unexpected)

    result = await worker.run_cleanup_round(
        object(),
        object(),
        bucket="media-cache",
        page_size=10,
        worker_id=uuid4(),
        input_collector=object(),
        video_collector=object(),
        stop=stop,
        storage_stop=threading.Event(),
    )

    assert events == ["outbox"]
    assert result == {"completed": 0, "failed": 0, "lane_failures": 0}


@pytest.mark.asyncio
async def test_video_s3_adapter_is_closed_even_if_image_adapter_close_fails(caplog):
    events = []

    class Storage:
        def __init__(self, name, failure=False):
            self.name, self.failure = name, failure

        async def close(self):
            events.append(self.name)
            if self.failure:
                raise RuntimeError("close failed")

    await worker.close_input_storages(Storage("image", failure=True), Storage("video"))

    assert events == ["image", "video"]
    assert "Accepted-image input storage close failed" in caplog.text
