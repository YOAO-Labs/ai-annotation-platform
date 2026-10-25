"""Mocked HTTP checks for bounded Registry command recovery semantics."""

from __future__ import annotations

import uuid

import httpx
import pytest

from aap_inference_contract.capabilities import CapabilityLimits
from aap_inference_contract.registry_ingress import (
    REGISTRY_ENV_SYNC_REPLY_MAX_BYTES,
    REGISTRY_ENV_SYNC_RESULT_PROTOCOL,
    RegistryEnvSyncResult,
    RegistryEnvSyncUnavailable,
    decode_registry_env_sync_request,
    decode_registry_env_sync_status_request,
    make_registry_env_sync_request,
    registry_env_sync_command_sha256,
    registry_env_sync_result_bytes,
    registry_env_sync_status_request,
)
from aap_inference_contract.registry_ingress_client import RegistryEnvSyncHttpClient

_REQUEST_ID = uuid.UUID("6c5921c6-9c53-4433-b727-0029c4ba5f1b")
_HTTPX_ASYNC_CLIENT = httpx.AsyncClient


def _request():
    return make_registry_env_sync_request(
        ["https://registry.example.test/v1"],
        timeout_seconds=3.5,
        capability_limits=CapabilityLimits(300, 16),
        request_id=_REQUEST_ID,
    )


def _result(request, *, state="committed", observed_urls=1, digest=None) -> bytes:
    return registry_env_sync_result_bytes(
        RegistryEnvSyncResult.model_validate(
            {
                "schema": REGISTRY_ENV_SYNC_RESULT_PROTOCOL,
                "request_id": request.request_id,
                "command_sha256": digest or registry_env_sync_command_sha256(request),
                "state": state,
                "observed_urls": observed_urls,
            }
        )
    )


def _install_mock_transport(
    monkeypatch, transport: httpx.AsyncBaseTransport
) -> list[dict]:
    options = []

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        options.append(
            {
                "follow_redirects": kwargs.get("follow_redirects"),
                "trust_env": kwargs.get("trust_env"),
            }
        )
        return _HTTPX_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    return options


@pytest.mark.asyncio
async def test_sync_returns_only_committed_ack_and_sends_one_command(
    monkeypatch,
) -> None:
    paths = []

    async def handle(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        typed = decode_registry_env_sync_request(await request.aread())
        return httpx.Response(
            200,
            content=_result(typed),
            headers={"content-type": "application/json"},
        )

    options = _install_mock_transport(monkeypatch, httpx.MockTransport(handle))
    client = RegistryEnvSyncHttpClient(
        "http://owner.local/base", "registry-token", timeout_seconds=4
    )
    result = await client.sync(_request())

    assert result.state == "committed"
    assert result.observed_urls == 1
    assert paths == ["/base/v1/registry/env-sync"]
    assert options == [{"follow_redirects": False, "trust_env": False}]


@pytest.mark.asyncio
async def test_ambiguous_lost_ack_recovers_with_one_status_read_without_resubmit(
    monkeypatch,
) -> None:
    paths = []
    command_request = _request()

    async def handle(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/env-sync"):
            raise httpx.ReadError("ack dropped after commit", request=request)
        status = decode_registry_env_sync_status_request(await request.aread())
        assert status.request_id == command_request.request_id
        assert status.command_sha256 == registry_env_sync_command_sha256(
            command_request
        )
        return httpx.Response(
            200,
            content=_result(command_request),
            headers={"content-type": "application/json"},
        )

    _install_mock_transport(monkeypatch, httpx.MockTransport(handle))
    result = await RegistryEnvSyncHttpClient("http://owner.local", "token").sync(
        command_request
    )

    assert result.state == "committed"
    assert paths == [
        "/v1/registry/env-sync",
        "/v1/registry/env-sync-status",
    ]
    assert paths.count("/v1/registry/env-sync") == 1


@pytest.mark.asyncio
async def test_malformed_ack_uses_one_status_probe_and_unknown_is_not_success(
    monkeypatch,
) -> None:
    paths = []
    request_value = _request()

    async def handle(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/env-sync"):
            return httpx.Response(
                200,
                json={
                    "schema": REGISTRY_ENV_SYNC_RESULT_PROTOCOL,
                    "state": "committed",
                },
            )
        return httpx.Response(
            200,
            content=_result(request_value, state="unknown", observed_urls=None),
            headers={"content-type": "application/json"},
        )

    _install_mock_transport(monkeypatch, httpx.MockTransport(handle))
    with pytest.raises(RegistryEnvSyncUnavailable):
        await RegistryEnvSyncHttpClient("http://owner.local", "token").sync(
            request_value
        )
    assert paths == ["/v1/registry/env-sync", "/v1/registry/env-sync-status"]


@pytest.mark.asyncio
async def test_noncommitted_ack_queries_status_once_and_never_returns_pending(
    monkeypatch,
) -> None:
    paths = []
    request_value = _request()

    async def handle(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/env-sync"):
            return httpx.Response(
                200,
                content=_result(request_value, state="caller_pending"),
                headers={"content-type": "application/json"},
            )
        return httpx.Response(
            200,
            content=_result(request_value, state="caller_pending"),
            headers={"content-type": "application/json"},
        )

    _install_mock_transport(monkeypatch, httpx.MockTransport(handle))
    with pytest.raises(RegistryEnvSyncUnavailable):
        await RegistryEnvSyncHttpClient("http://owner.local", "token").sync(
            request_value
        )
    assert paths.count("/v1/registry/env-sync") == 1
    assert paths.count("/v1/registry/env-sync-status") == 1


@pytest.mark.asyncio
async def test_status_fails_closed_on_oversize_and_never_retries(monkeypatch) -> None:
    calls = 0
    status_request = registry_env_sync_status_request(_request())

    async def oversized(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            content=b"",
            headers={
                "content-type": "application/json",
                "content-length": str(REGISTRY_ENV_SYNC_REPLY_MAX_BYTES + 1),
            },
        )

    _install_mock_transport(monkeypatch, httpx.MockTransport(oversized))
    with pytest.raises(RegistryEnvSyncUnavailable):
        await RegistryEnvSyncHttpClient("http://owner.local", "token").status(
            status_request
        )
    assert calls == 1


def test_client_rejects_invalid_provider_configuration() -> None:
    for base_url, token, timeout in (
        ("file:///tmp/owner", "token", 30),
        ("http://user:password@owner.local", "token", 30),
        ("http://owner.local", "", 30),
        ("http://owner.local", "token", True),
    ):
        with pytest.raises(RegistryEnvSyncUnavailable):
            RegistryEnvSyncHttpClient(base_url, token, timeout_seconds=timeout)
