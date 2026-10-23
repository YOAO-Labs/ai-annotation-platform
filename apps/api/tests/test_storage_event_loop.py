"""Slow S3 calls must not stall unrelated API requests."""

import asyncio
import threading

import httpx
import pytest
from fastapi import FastAPI

from app.api import health
from app.api.v1 import storage


@pytest.mark.parametrize("operation", ["health", "minio-health"])
async def test_slow_storage_keeps_other_requests_responsive(monkeypatch, operation):
    entered = threading.Event()
    release = threading.Event()

    def block_once():
        if not entered.is_set():
            entered.set()
            release.wait(timeout=1)

    def head_bucket(**kwargs):
        block_once()

    monkeypatch.setattr(storage.storage_service.client, "head_bucket", head_bucket)
    app = FastAPI()

    @app.get("/storage")
    async def slow_storage():
        if operation == "health":
            return await storage.storage_health(None)
        if operation == "minio-health":
            return await health.health_minio()
        raise AssertionError("Unexpected operation")

    @app.get("/ping")
    async def ping():
        return {"status": "ok"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        pending = asyncio.create_task(client.get("/storage"))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            assert not pending.done(), (
                "S3 blocked the event loop until its call finished"
            )
            fast = await asyncio.wait_for(client.get("/ping"), timeout=0.5)
            assert fast.status_code == 200
            assert fast.json() == {"status": "ok"}
        finally:
            release.set()
            response = await asyncio.wait_for(pending, timeout=2)

    assert response.status_code == 200
    payload = response.json()
    if operation == "health":
        assert payload == {"status": "ok", "bucket": storage.storage_service.bucket}
    elif operation == "minio-health":
        assert payload["status"] == "ok"
        assert payload["latency_ms"] >= 0
