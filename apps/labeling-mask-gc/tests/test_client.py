from __future__ import annotations

import json

import httpx
import pytest

from aap_labeling_mask_gc import contract as c
from aap_labeling_mask_gc.client import (
    LabelingMaskGcClient,
    LabelingMaskGcClientFailure,
)

TOKEN = "r" * 48


def test_http_client_uses_closed_schema_and_service_token():
    calls = []
    result = c.CycleStats(scan_pages=1, scanned=12, skipped=12)

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "schema": c.SCHEMA,
                "ok": True,
                "data": result.model_dump(mode="json"),
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with LabelingMaskGcClient("http://mask-gc:8771", TOKEN, client=client) as gc_client:
        reply = gc_client.run(c.RunRequest(scan_pages=1, delete_limit=10))
    client.close()

    assert reply == result
    assert calls[0].url.path == "/v1/mask-gc/run"
    assert calls[0].headers["authorization"] == f"Bearer {TOKEN}"


def test_http_client_rejects_provider_text_and_foreign_schema():
    def handler(_request):
        return httpx.Response(
            503,
            content=json.dumps(
                {
                    "schema": c.SCHEMA,
                    "ok": False,
                    "error": {
                        "code": "storage_unavailable",
                        "provider": "secret bucket name",
                    },
                }
            ),
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with LabelingMaskGcClient("http://mask-gc:8771", TOKEN, client=client) as gc_client:
        with pytest.raises(LabelingMaskGcClientFailure) as error:
            gc_client.progress()
    client.close()
    assert error.value.code == "invalid_response"
    assert "secret bucket" not in str(error.value)


def test_http_client_preserves_safe_service_error_code():
    def handler(_request):
        return httpx.Response(
            503,
            json={
                "schema": c.SCHEMA,
                "ok": False,
                "error": {"code": "storage_unavailable"},
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with LabelingMaskGcClient("http://mask-gc:8771", TOKEN, client=client) as gc_client:
        with pytest.raises(LabelingMaskGcClientFailure) as error:
            gc_client.progress()
    client.close()
    assert error.value.code == "storage_unavailable"


@pytest.mark.parametrize(
    "result",
    [
        c.CycleStats(dry_run=False),
        c.CycleStats(dry_run=True, scan_pages=2),
        c.CycleStats(delete_claims=2, finish_errors=2),
        c.CycleStats(dry_run=True, scan_pages=1, scanned=1001, skipped=1001),
    ],
)
def test_http_client_rejects_run_stats_outside_request_scope(result):
    def handler(_request):
        return httpx.Response(
            200,
            json={
                "schema": c.SCHEMA,
                "ok": True,
                "data": result.model_dump(mode="json"),
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with LabelingMaskGcClient("http://mask-gc:8771", TOKEN, client=client) as gc_client:
        with pytest.raises(LabelingMaskGcClientFailure) as error:
            gc_client.run(c.RunRequest(scan_pages=1, delete_limit=1, dry_run=True))
    client.close()
    assert error.value.code == "invalid_response"
