from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import pytest

from aap_inference.gpu.client import GPUAuthorityHTTPClient
from aap_inference.gpu.contracts import GPUArbiterDispatchError
from aap_inference_contract.gpu import GPU_ADMIN_HTTP_PROTOCOL, GPU_ADMIN_PROBE_PATH


class _BytesResponse:
    def __init__(
        self,
        content: bytes,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.content = content
        self.status_code = status_code
        self.headers = headers or {}


class _BytesTransport:
    def __init__(self, response: _BytesResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def post(self, url: str, *, json: dict[str, Any]) -> _BytesResponse:
        self.calls.append((url, json))
        return self.response


@pytest.mark.asyncio
async def test_gpu_authority_client_decodes_uuid_from_real_wire_json_bytes() -> None:
    backend_id = UUID("8f8e0b94-6fc7-4e8a-9893-53070fd66c73")
    resource_id = "node-wire/index:0"
    response = _BytesResponse(
        json.dumps(
            {
                "protocol": GPU_ADMIN_HTTP_PROTOCOL,
                "backend_id": str(backend_id),
                "resource_id": resource_id,
                "membership_epoch": 7,
                "reason": "retirement_proof_unavailable",
                "proof_available": False,
            },
            separators=(",", ":"),
        ).encode("utf-8")
    )
    transport = _BytesTransport(response)
    client = GPUAuthorityHTTPClient("http://inference-owner", transport)

    reply = await client.probe_retired_membership(backend_id, resource_id, 7)

    assert reply.backend_id == backend_id
    assert reply.resource_id == resource_id
    assert transport.calls == [
        (
            f"http://inference-owner{GPU_ADMIN_PROBE_PATH}",
            {
                "protocol": GPU_ADMIN_HTTP_PROTOCOL,
                "backend_id": str(backend_id),
                "resource_id": resource_id,
                "membership_epoch": 7,
            },
        )
    ]


@pytest.mark.asyncio
async def test_gpu_authority_client_preserves_remote_error_from_wire_json_bytes() -> (
    None
):
    backend_id = UUID("8f8e0b94-6fc7-4e8a-9893-53070fd66c73")
    response = _BytesResponse(
        json.dumps(
            {
                "status_code": 409,
                "detail": {"error_code": "gpu_dispatch_blocked", "message": "held"},
                "headers": {"X-Authority-Reason": "rollout"},
                "error_code": "gpu_dispatch_blocked",
                "retry_after_s": 11,
            },
            separators=(",", ":"),
        ).encode("utf-8"),
        status_code=409,
        headers={"X-Transport-Reason": "owner"},
    )
    client = GPUAuthorityHTTPClient(
        "http://inference-owner",
        _BytesTransport(response),
    )

    with pytest.raises(GPUArbiterDispatchError) as caught:
        await client.probe_retired_membership(backend_id, "node-wire/index:0", 7)

    assert caught.value.status_code == 409
    assert caught.value.error_code == "gpu_dispatch_blocked"
    assert caught.value.detail == {
        "error_code": "gpu_dispatch_blocked",
        "message": "held",
    }
    assert caught.value.headers == {
        "X-Transport-Reason": "owner",
        "X-Authority-Reason": "rollout",
    }
    assert caught.value.retry_after_s == 11
