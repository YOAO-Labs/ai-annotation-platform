from __future__ import annotations

import asyncio
from threading import Event

from fastapi.testclient import TestClient
import httpx

from aap_assets_mask_content.gc_contract import GC_SCHEMA, GcScanPage
from aap_assets_mask_content_runtime.api import create_app

_TOKEN = "cleanup-token-with-more-than-32-characters"
_OPERATION_ID = "2d639c8a-0fd8-4dcb-9f90-26cb8a752f3a"


class ContentStore:
    def __init__(self):
        self.calls = []

    def put(self, request):
        self.calls.append("put")
        return None

    def get(self, request):
        self.calls.append("get")
        return None

    def list(self, request):
        self.calls.append("list")
        return None

    def delete(self, request):
        self.calls.append("delete")
        return None

    def check_ready(self):
        return None


class GcStore:
    def __init__(self):
        self.calls = []
        self.entered = None
        self.release = None

    def scan(self, request):
        self.calls.append("scan")
        if self.entered is not None and self.release is not None:
            self.entered.set()
            assert self.release.wait(5)
        return GcScanPage(items=[], after_key=None, complete=True)

    def execute(self, request):
        self.calls.append("execute")
        return None

    def status(self, request):
        self.calls.append("status")
        return None

    def check_ready(self):
        return None


def test_gc_routes_are_cleanup_only_and_use_the_gc_envelope():
    content_store = ContentStore()
    gc_store = GcStore()
    content_client = TestClient(
        create_app(content_store, _TOKEN, "content", gc_store=gc_store)
    )
    denied = content_client.post(
        "/v1/mask-gc/scan",
        json={"after_key": None, "limit": 1000},
        headers={"Authorization": "Bearer " + _TOKEN},
    )
    assert denied.status_code == 403
    assert denied.json() == {
        "schema": GC_SCHEMA,
        "ok": False,
        "error": {"code": "permission_denied"},
    }
    assert gc_store.calls == []

    cleanup_client = TestClient(
        create_app(content_store, _TOKEN, "cleanup", gc_store=gc_store)
    )
    response = cleanup_client.post(
        "/v1/mask-gc/scan",
        json={"after_key": None, "limit": 1000},
        headers={"Authorization": "Bearer " + _TOKEN},
    )
    assert response.status_code == 200
    assert response.json() == {
        "schema": GC_SCHEMA,
        "ok": True,
        "data": {"items": [], "after_key": None, "complete": True},
    }
    assert gc_store.calls == ["scan"]


def test_gc_routes_reject_bad_auth_and_oversized_bodies_before_store():
    gc_store = GcStore()
    client = TestClient(
        create_app(ContentStore(), _TOKEN, "cleanup", gc_store=gc_store)
    )
    unauthorized = client.post(
        "/v1/mask-gc/status",
        json={"operation_id": _OPERATION_ID},
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert unauthorized.status_code == 401
    assert unauthorized.json()["error"]["code"] == "permission_denied"

    from aap_assets_mask_content.gc_contract import MAX_GC_REQUEST_BYTES

    oversized = client.post(
        "/v1/mask-gc/scan",
        content=b"x" * (MAX_GC_REQUEST_BYTES + 1),
        headers={
            "Authorization": "Bearer " + _TOKEN,
            "Content-Type": "application/json",
        },
    )
    assert oversized.status_code == 413
    assert oversized.json()["error"]["code"] == "resource_limit"
    assert gc_store.calls == []


def test_content_and_gc_routes_share_one_storage_admission():
    async def check():
        content_store = ContentStore()
        gc_store = GcStore()
        gc_store.entered = asyncio.Event()
        gc_store.release = Event()
        loop = asyncio.get_running_loop()

        def mark_entered():
            loop.call_soon_threadsafe(gc_store.entered.set)

        def blocking_scan(request):
            gc_store.calls.append("scan")
            mark_entered()
            assert gc_store.release.wait(5)
            return GcScanPage(items=[], after_key=None, complete=True)

        gc_store.scan = blocking_scan
        transport = httpx.ASGITransport(
            app=create_app(content_store, _TOKEN, "cleanup", gc_store=gc_store)
        )
        async with httpx.AsyncClient(
            transport=transport, base_url="http://mask"
        ) as client:
            first = asyncio.create_task(
                client.post(
                    "/v1/mask-gc/scan",
                    json={"after_key": None, "limit": 10},
                    headers={"Authorization": "Bearer " + _TOKEN},
                )
            )
            try:
                await asyncio.wait_for(gc_store.entered.wait(), timeout=5)
                overloaded = await client.post(
                    "/v1/mask-content/list",
                    json={"cursor": None, "limit": 10},
                    headers={"Authorization": "Bearer " + _TOKEN},
                )
                assert overloaded.status_code == 503
                assert overloaded.json()["error"]["code"] == "storage_unavailable"
                assert content_store.calls == []
            finally:
                gc_store.release.set()
            assert (await first).status_code == 200

    asyncio.run(check())
