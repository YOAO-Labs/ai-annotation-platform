"""v0.8.7 F2 · MLBackendClient 计时 + Histogram observe + wall-clock fallback。

v0.9.12 · 加 max_concurrency Semaphore 限速测试 (B-17).
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import httpx
import pytest

from app.db.models.ml_backend_registry import MLBackendRegistry
from app.services.ml_client import MLBackendClient


pytestmark = pytest.mark.usefixtures("isolated_routing_port")


def _backend(
    url="http://fake:9090",
    *,
    backend_id="00000000-0000-0000-0000-000000000099",
    extra_params=None,
):
    b = MLBackendRegistry()
    b.id = backend_id
    b.url = url
    b.auth_method = "none"
    b.auth_token = None
    b.extra_params = extra_params or {}
    return b


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

    client = MLBackendClient(_backend())
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
    assert after - before >= 1  # 一次 success observe


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

    client = MLBackendClient(_backend())
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
    assert after - before >= 1


@pytest.mark.asyncio
async def test_predict_uses_backend_reported_time_when_present():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "results": [
                    {
                        "task": "t1",
                        "result": [],
                        "inference_time_ms": 42,  # backend 自报
                    }
                ]
            },
        )
    )
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real(*args, **kwargs)

    client = MLBackendClient(_backend())
    with patch(
        "aap_inference.backend_transport.httpx.AsyncClient", side_effect=factory
    ):
        results = await client.predict([{"id": "t1"}])
    # backend 自报值优先
    assert results[0].inference_time_ms == 42


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
    client = MLBackendClient(backend)

    with patch(
        "aap_inference.backend_transport.httpx.AsyncClient", side_effect=factory
    ):
        await asyncio.gather(*(client.predict([{"id": f"t{i}"}]) for i in range(5)))

    assert peak <= 2, f"peak in-flight {peak} 超过 max_concurrency=2"
    assert peak >= 1


@pytest.mark.asyncio
async def test_no_max_concurrency_defaults_to_4():
    """无 extra_params.max_concurrency 时默认 cap=4."""
    bid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    backend = _backend(backend_id=bid)
    client = MLBackendClient(backend)
    assert client.max_concurrency == 4
    assert client._semaphore is not None


def test_semaphore_cache_is_event_loop_local() -> None:
    """Celery repeated asyncio.run calls must not reuse a loop-bound semaphore."""

    backend = _backend(
        backend_id="bbbbbbbb-cccc-dddd-eeee-ffffffffffff",
        extra_params={"max_concurrency": 1},
    )

    async def exercise() -> asyncio.Semaphore:
        semaphore = MLBackendClient(backend)._semaphore
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
