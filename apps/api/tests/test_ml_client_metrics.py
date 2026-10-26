"""Portable backend transport metrics, concurrency and event-loop isolation."""

from __future__ import annotations

import asyncio
from unittest.mock import patch
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest

from aap_inference.backend_transport import BackendTransport
from fastapi import HTTPException

from app.observability.metrics import observe_ml_backend


def _backend(
    url="http://fake:9090",
    *,
    backend_id="00000000-0000-0000-0000-000000000099",
    extra_params=None,
):
    return SimpleNamespace(
        id=backend_id,
        url=url,
        auth_method="none",
        auth_token=None,
        extra_params=extra_params or {},
        health_meta=None,
    )


@asynccontextmanager
async def _unmanaged_dispatch(_operation, _scope):
    yield None


def _transport(backend, *, dispatch_context=_unmanaged_dispatch):
    return BackendTransport(
        backend,
        options=SimpleNamespace(ml_health_timeout=10, ml_predict_timeout=100),
        dispatch_context=dispatch_context,
        observe=observe_ml_backend,
        error_factory=HTTPException,
    )


@pytest.mark.asyncio
async def test_predict_observes_success_histogram():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "results": [
                    {"task": "t1", "result": [], "score": 0.9, "model_version": "m"}
                ]
            },
        )
    )
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real(*args, **kwargs)

    from app.observability.metrics import ML_BACKEND_REQUEST_DURATION

    before = sum(
        s.value
        for m in ML_BACKEND_REQUEST_DURATION.collect()
        for s in m.samples
        if s.name.endswith("_count") and s.labels.get("outcome") == "success"
    )

    client = _transport(_backend())
    with patch(
        "aap_inference.backend_transport.httpx.AsyncClient", side_effect=factory
    ):
        results = await client.predict([{"id": "t1"}])

    assert len(results) == 1
    # 没带 inference_time_ms 时 wall-clock 兜底 → 应是非负 int
    assert results[0].inference_time_ms is not None
    assert results[0].inference_time_ms >= 0

    after = sum(
        s.value
        for m in ML_BACKEND_REQUEST_DURATION.collect()
        for s in m.samples
        if s.name.endswith("_count") and s.labels.get("outcome") == "success"
    )
    assert after - before == 1


@pytest.mark.asyncio
async def test_predict_observes_error_histogram_and_reraises():
    def boom(request):
        raise httpx.ConnectError("boom")

    transport = httpx.MockTransport(boom)
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real(*args, **kwargs)

    from app.observability.metrics import ML_BACKEND_REQUEST_DURATION

    before = sum(
        s.value
        for m in ML_BACKEND_REQUEST_DURATION.collect()
        for s in m.samples
        if s.name.endswith("_count") and s.labels.get("outcome") == "error"
    )

    client = _transport(_backend())
    with patch(
        "aap_inference.backend_transport.httpx.AsyncClient", side_effect=factory
    ):
        with pytest.raises(httpx.ConnectError):
            await client.predict([{"id": "t1"}])

    after = sum(
        s.value
        for m in ML_BACKEND_REQUEST_DURATION.collect()
        for s in m.samples
        if s.name.endswith("_count") and s.labels.get("outcome") == "error"
    )
    assert after - before == 1


@pytest.mark.asyncio
async def test_max_concurrency_semaphore_caps_in_flight_predictions():
    """v0.9.12 B-17 · extra_params.max_concurrency=2 时 5 个并发 predict 同时 in-flight ≤ 2."""
    in_flight = 0
    peak = 0
    lock = asyncio.Lock()

    async def slow_handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        async with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        await asyncio.sleep(0.05)
        async with lock:
            in_flight -= 1
        return httpx.Response(
            200, json={"results": [{"task": "t", "result": [], "score": 0.5}]}
        )

    transport = httpx.MockTransport(slow_handler)
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real(*args, **kwargs)

    # 用唯一 backend_id 避免被前序测试缓存的 Semaphore 污染
    bid = "11111111-2222-3333-4444-555555555555"
    backend = _backend(backend_id=bid, extra_params={"max_concurrency": 2})
    client = _transport(backend)

    with patch(
        "aap_inference.backend_transport.httpx.AsyncClient", side_effect=factory
    ):
        await asyncio.gather(*(client.predict([{"id": f"t{i}"}]) for i in range(5)))

    assert peak == 2


def test_semaphore_cache_is_event_loop_local() -> None:
    """Celery repeated asyncio.run calls must not reuse a loop-bound semaphore."""

    backend = _backend(
        backend_id="bbbbbbbb-cccc-dddd-eeee-ffffffffffff",
        extra_params={"max_concurrency": 1},
    )

    async def exercise() -> asyncio.Semaphore:
        semaphore = _transport(backend)._semaphore
        assert semaphore is not None
        await semaphore.acquire()
        waiter = asyncio.create_task(semaphore.acquire())
        await asyncio.sleep(0)
        assert waiter.done() is False
        semaphore.release()
        await waiter
        semaphore.release()
        return semaphore

    first = asyncio.run(exercise())
    second = asyncio.run(exercise())

    assert first is not second
