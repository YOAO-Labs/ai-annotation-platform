from __future__ import annotations

import uuid

import httpx
import pytest

from aap_inference_contract.capabilities import CapabilityLimits
from aap_inference import (
    BackendSetupLocator,
    fetch_backend_setup,
    probe_backend_capabilities,
)


class Response:
    status_code = 200

    def __init__(self, value):
        self.value = value

    def raise_for_status(self):
        return None

    def json(self):
        return self.value


class AsyncClient:
    calls = []
    value = {"name": "backend", "supported_prompts": ["none"]}
    error = None

    def __init__(self, *, timeout):
        self.timeout = timeout
        if type(self).error is not None:
            raise type(self).error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def get(self, url, *, headers):
        type(self).calls.append((url, headers, self.timeout))
        if type(self).error is not None:
            raise type(self).error
        return Response(type(self).value)


@pytest.fixture
def setup_client(monkeypatch):
    AsyncClient.calls = []
    AsyncClient.value = {"name": "backend", "supported_prompts": ["none"]}
    AsyncClient.error = None
    monkeypatch.setattr(httpx, "AsyncClient", AsyncClient)


def _locator():
    return BackendSetupLocator(
        id=uuid.uuid4(),
        url="http://backend/",
        auth_method="token",
        auth_token="secret-token",
        extra_params={"max_concurrency": 3},
    )


@pytest.mark.asyncio
async def test_setup_probe_uses_current_url_auth_headers_and_timeout(setup_client):
    locator = _locator()
    setup = await fetch_backend_setup(locator, timeout=7.5)

    assert setup == AsyncClient.value
    assert AsyncClient.calls == [
        (
            "http://backend/setup",
            {
                "Content-Type": "application/json",
                "Authorization": "Bearer secret-token",
                "Cache-Control": "no-cache",
            },
            7.5,
        )
    ]


@pytest.mark.asyncio
async def test_setup_probe_omits_auth_for_non_token_methods(setup_client):
    locator = BackendSetupLocator(
        id=uuid.uuid4(),
        url="http://backend",
        auth_method="none",
        auth_token="unused",
    )
    await fetch_backend_setup(locator, timeout=3.0)
    assert AsyncClient.calls[0][1] == {
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
    }


@pytest.mark.asyncio
async def test_probe_fails_open_for_constructor_network_and_normalization_errors(
    setup_client, monkeypatch
):
    locator = _locator()
    AsyncClient.error = RuntimeError("transport unavailable")
    assert await probe_backend_capabilities(locator, timeout=1.0) is None
    with pytest.raises(RuntimeError, match="transport unavailable"):
        await fetch_backend_setup(locator, timeout=1.0)

    AsyncClient.error = None
    AsyncClient.value = {
        "models": [{"id": "invalid", "supported_geometric_outputs": 12}]
    }
    assert await probe_backend_capabilities(locator, timeout=1.0) is None

    def bad_constructor(**_):
        raise RuntimeError("client construction failed")

    monkeypatch.setattr(httpx, "AsyncClient", bad_constructor)
    assert await probe_backend_capabilities(locator, timeout=1.0) is None


@pytest.mark.asyncio
async def test_probe_preserves_client_constructor_coercion_before_http(setup_client):
    locator = BackendSetupLocator(
        id=uuid.uuid4(),
        url="http://backend",
        auth_method="none",
        auth_token=None,
        extra_params={"max_concurrency": "invalid"},
    )

    assert await probe_backend_capabilities(locator, timeout=1.0) is None
    assert AsyncClient.calls == []

    # The direct transport remains an HTTP operation and does not apply the
    # higher-level MLBackendClient constructor's concurrency setting.
    assert await fetch_backend_setup(locator, timeout=1.0) == AsyncClient.value
    assert len(AsyncClient.calls) == 1


@pytest.mark.asyncio
async def test_probe_normalizes_capabilities_with_caller_limits(setup_client):
    AsyncClient.value = {
        "models": [
            {
                "id": "tracker",
                "task": "tracker",
                "supported_trackers": ["sam3_video"],
                "max_window_frames": 64,
            }
        ]
    }
    result = await probe_backend_capabilities(
        _locator(),
        timeout=2.0,
        limits=CapabilityLimits(
            video_tracker_window_size_frames=24,
            video_tracker_sam3_window_size_frames=8,
        ),
    )
    assert result["models"][0]["max_window_frames"] == 8
