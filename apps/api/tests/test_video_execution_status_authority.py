from __future__ import annotations

import uuid

import pytest

from aap_inference_contract.routing import RoutingVideoExecutionProof
from aap_inference_contract.video_execution import (
    VIDEO_EXECUTION_DATA_PROTOCOL,
    decode_video_execution_status_result,
    video_execution_status_result_bytes,
)
from app.config import settings
from app.services.ml_routing import authority as authority_module


@pytest.mark.asyncio
async def test_video_execution_status_uses_configured_http_owner(monkeypatch):
    proof = RoutingVideoExecutionProof(
        job_id=uuid.UUID("10b9f6cc-6424-4b4a-8740-3660cb25db17"),
        run_id=uuid.UUID("9f8f00b4-6d2f-4fe0-a75e-a28c4e923714"),
        worker_id=uuid.UUID("8f87cb4e-636d-4606-983f-e7397cb954f9"),
        generation=4,
        lease_token=uuid.UUID("cb72f60a-61b0-4e76-b355-f8e8b976c4a2"),
    )
    monkeypatch.setattr(settings, "inference_routing_url", "http://routing-owner")
    monkeypatch.setattr(settings, "inference_routing_token", "private-token")
    monkeypatch.setattr(settings, "inference_routing_token_file", "")
    clients = []

    class HttpClient:
        def __init__(self, url, token, *, timeout_seconds):
            clients.append((url, token, timeout_seconds))

        async def video_execution_status(self, request):
            assert request == proof
            data = {
                "schema": VIDEO_EXECUTION_DATA_PROTOCOL,
                "status": "ok",
                "intent": None,
                "scope": None,
            }
            return decode_video_execution_status_result(
                request, video_execution_status_result_bytes(request, data)
            )

    monkeypatch.setattr(authority_module, "RoutingHttpClient", HttpClient)
    authority = authority_module.RoutingAuthority()

    result = await authority.video_execution_status(proof)

    assert clients == [
        (
            "http://routing-owner",
            "private-token",
            settings.inference_routing_timeout_seconds,
        )
    ]
    assert result == {"intent": None, "scope": None}
