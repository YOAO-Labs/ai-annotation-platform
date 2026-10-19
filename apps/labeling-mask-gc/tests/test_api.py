from __future__ import annotations

import asyncio
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient

from aap_assets_mask_content.gc_contract import GcScanPage
from aap_labeling_mask_gc import contract as c
from aap_labeling_mask_gc_runtime.api import create_app

TOKEN = "t" * 48
WORKER = UUID("00000000-0000-0000-0000-000000000031")


class FakeData:
    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def call(self, operation, _request):
        if operation == "claim_mask_gc_sweep":
            self.entered.set()
            await self.release.wait()
            return c.ClaimMaskGcSweepResult(sweep=None)
        if operation == "claim_mask_gc_deletes":
            return c.ClaimMaskGcDeletesResult(items=())
        if operation == "read_mask_gc_progress":
            return c.ReadMaskGcProgressResult(
                active_intents=0,
                indeterminate_intents=0,
                completed_intents=0,
                sweep=None,
            )
        raise AssertionError(operation)


class FakeStorage:
    def scan(self, _request):
        return GcScanPage(items=(), after_key=None, complete=True)

    def execute(self, _request):
        raise AssertionError("no delete claims are expected")

    def status(self, _request):
        raise AssertionError("no delete claims are expected")

    def check_ready(self):
        return None


def test_api_authenticates_and_returns_only_closed_progress():
    app = create_app(FakeData(), FakeStorage(), TOKEN, worker_id=WORKER)
    with TestClient(app) as client:
        denied = client.post("/v1/mask-gc/progress", json={})
        assert denied.status_code == 401
        assert denied.json() == {
            "schema": c.SCHEMA,
            "ok": False,
            "error": {"code": "permission_denied"},
        }

        response = client.post(
            "/v1/mask-gc/progress",
            json={},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
    assert response.status_code == 200
    assert response.json()["schema"] == c.SCHEMA
    assert response.json()["data"]["active_intents"] == 0


def test_run_rejects_unexpected_fields_and_oversized_body():
    app = create_app(FakeData(), FakeStorage(), TOKEN, worker_id=WORKER)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    with TestClient(app) as client:
        invalid = client.post(
            "/v1/mask-gc/run",
            json={"scan_pages": 1, "provider": "secret"},
            headers=headers,
        )
        assert invalid.status_code == 422
        assert invalid.json()["error"]["code"] == "invalid_request"

        oversized = client.post(
            "/v1/mask-gc/run",
            content=b" " * (c.MAX_REQUEST_BYTES + 1),
            headers={**headers, "Content-Type": "application/json"},
        )
        assert oversized.status_code == 413


@pytest.mark.asyncio
async def test_only_one_run_is_admitted_per_process():
    data = FakeData()
    app = create_app(data, FakeStorage(), TOKEN, worker_id=WORKER)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = asyncio.create_task(
            client.post("/v1/mask-gc/run", json={}, headers=headers)
        )
        await asyncio.wait_for(data.entered.wait(), timeout=2)
        second = await client.post("/v1/mask-gc/run", json={}, headers=headers)
        assert second.status_code == 503
        assert second.json()["error"]["code"] == "storage_unavailable"
        data.release.set()
        completed = await asyncio.wait_for(first, timeout=2)
    assert completed.status_code == 200
