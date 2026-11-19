"""验证 /health/celery 的队列长度、worker 心跳与 Prometheus 指标。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from httpx import AsyncClient


@pytest.fixture
def fake_inspect_data():
    """构造 stats + broker 队列长度的 mock 返回值。"""
    return {
        "stats": {
            "worker@h1": {"pool": {"max-concurrency": 4}},
            "worker@h2": {"pool": {"max-concurrency": 2}},
        },
        "queues": {"ml": 3, "audit": 1},
    }


@pytest.mark.asyncio
async def test_health_celery_returns_queues_and_workers(
    httpx_client: AsyncClient, fake_inspect_data
):
    inspect = MagicMock()
    inspect.stats.return_value = fake_inspect_data["stats"]

    with (
        patch("app.api.health.celery_app.control.inspect", return_value=inspect),
        patch(
            "app.api.health._read_celery_queue_lengths",
            return_value=fake_inspect_data["queues"],
        ),
    ):
        resp = await httpx_client.get("/health/celery")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert {q["name"] for q in data["queues"]} == {"ml", "audit"}
    ml_q = next(q for q in data["queues"] if q["name"] == "ml")
    assert ml_q["length"] == 3  # 2 active + 1 reserved
    audit_q = next(q for q in data["queues"] if q["name"] == "audit")
    assert audit_q["length"] == 1
    assert {w["name"] for w in data["workers"]} == {"worker@h1", "worker@h2"}
    # /health/celery 从 Redis 读各 worker bootstep 写入的心跳。Fake worker
    # 无心跳 key，故新鲜度为 None（未知），不再是旧的硬编码 0。
    assert all(w["last_heartbeat_seconds_ago"] is None for w in data["workers"])
    assert any(w["pool_max"] == 4 for w in data["workers"])


@pytest.mark.asyncio
async def test_health_celery_no_workers_503(httpx_client: AsyncClient):
    inspect = MagicMock()
    inspect.stats.return_value = None
    with patch("app.api.health.celery_app.control.inspect", return_value=inspect):
        resp = await httpx_client.get("/health/celery")
    assert resp.status_code == 503
    assert resp.json()["status"] == "error"
    assert resp.json()["queues"] == []


@pytest.mark.asyncio
async def test_celery_queue_length_gauge_observed(fake_inspect_data):
    """Prometheus Gauge 应被设置；可从 generate_latest() 读到值。"""
    from app.observability.metrics import CELERY_QUEUE_LENGTH
    from app.api.health import _check_celery

    inspect = MagicMock()
    inspect.stats.return_value = fake_inspect_data["stats"]

    with (
        patch("app.api.health.celery_app.control.inspect", return_value=inspect),
        patch(
            "app.api.health._read_celery_queue_lengths",
            return_value=fake_inspect_data["queues"],
        ),
    ):
        _check_celery()

    # 直接读 Gauge 内部 value
    sample = CELERY_QUEUE_LENGTH.labels(queue="ml")._value.get()
    assert sample == 3
