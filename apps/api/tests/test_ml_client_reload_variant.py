"""v0.10.26 · MLBackendClient reload 变体 body 透传 + health_meta 保留 pool 键。

不连真实 backend, 用 httpx.MockTransport 捕获请求 body / 注入 /health 响应。
"""

from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest

from app.db.models.ml_backend_registry import MLBackendRegistry
from aap_inference.backend_transport import (
    GPU_HEALTH_CHALLENGE_ECHO_MARKER,
    GPU_HEALTH_CHALLENGE_HEADER,
    GPU_HEALTH_CHALLENGE_QUERY_PARAM,
)
from app.services.ml_client import MLBackendClient


pytestmark = pytest.mark.usefixtures("isolated_routing_port")


def _backend(url="http://fake:9090"):
    b = MLBackendRegistry()
    b.id = "00000000-0000-0000-0000-0000000000aa"
    b.url = url
    b.auth_method = "none"
    b.auth_token = None
    b.extra_params = {}
    return b


def _patched(transport):
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real(*args, **kwargs)

    return patch(
        "aap_inference.backend_transport.httpx.AsyncClient", side_effect=factory
    )


@pytest.mark.asyncio
async def test_reload_with_variant_posts_body():
    captured: dict = {}

    def handler(request):
        captured["body"] = json.loads(request.content) if request.content else None
        return httpx.Response(200, json={"ok": True, "loaded": True, "reloaded": True})

    client = MLBackendClient(_backend())
    with _patched(httpx.MockTransport(handler)):
        await client.reload(sam_variant="small", dino_variant="B")

    assert captured["body"] == {"sam_variant": "small", "dino_variant": "B"}


@pytest.mark.asyncio
async def test_reload_without_variant_sends_no_body():
    captured: dict = {}

    def handler(request):
        captured["raw"] = request.content
        return httpx.Response(200, json={"ok": True, "loaded": True, "reloaded": False})

    client = MLBackendClient(_backend())
    with _patched(httpx.MockTransport(handler)):
        await client.reload()

    # body 留空 → 不发 JSON (backend 用默认变体)。
    assert captured["raw"] in (b"", b"null")


@pytest.mark.asyncio
async def test_health_meta_keeps_pool():
    pool = {
        "cap": 2,
        "loaded_variants": [{"sam_variant": "small", "dino_variant": "B"}],
        "evict_count": 0,
        "per_variant_lru_ts": {"small/B": 1.0},
    }

    def handler(request):
        return httpx.Response(
            200,
            json={
                "ok": True,
                "gpu_info": {"memory_used_mb": 1},
                "cache": {"hit_rate": 0.5, "buckets": {"small/B": {"hit_rate": 0.5}}},
                "model_version": "grounded-sam2-dinoB-sam2.1small",
                "loaded": True,
                "idle_unload_seconds": 600,
                "last_request_age_seconds": 12.5,
                "pool": pool,
                "video_pool": {"loaded_variants": [], "active_sessions": 0},
                "compute": {
                    "configured_device": "cuda",
                    "effective_device": "cpu",
                    "cpu_fallback_supported": True,
                },
                "residency": {
                    "state": "resident",
                    "gpu_loaded": True,
                    "active_requests": 1,
                    "builders": 0,
                    "borrowers": 2,
                    "evictable": False,
                    "pools": {
                        "models": {
                            "resident": True,
                            "device": "cuda:0",
                            "provider": None,
                        }
                    },
                },
            },
        )

    client = MLBackendClient(_backend())
    with _patched(httpx.MockTransport(handler)):
        ok, meta = await client.health_meta()

    assert ok is True
    assert meta is not None
    assert meta["loaded"] is True
    assert meta["idle_unload_seconds"] == 600
    assert meta["last_request_age_seconds"] == 12.5
    assert meta["pool"] == pool
    assert meta["video_pool"]["active_sessions"] == 0
    assert meta["compute"] == {
        "configured_device": "cuda",
        "effective_device": "cpu",
        "cpu_fallback_supported": True,
    }
    assert meta["residency"]["gpu_loaded"] is True
    assert meta["residency"]["active_requests"] == 1
    assert meta["residency"]["borrowers"] == 2
    assert "buckets" in meta["cache"]


@pytest.mark.asyncio
async def test_health_meta_binds_exact_echo_to_challenge_request() -> None:
    challenge = "ab" * 32
    captured: dict[str, object] = {}
    assert GPU_HEALTH_CHALLENGE_HEADER == "X-AAP-GPU-Health-Challenge"
    assert GPU_HEALTH_CHALLENGE_QUERY_PARAM == "aap_gpu_health_challenge"

    def handler(request: httpx.Request) -> httpx.Response:
        captured["header"] = request.headers[GPU_HEALTH_CHALLENGE_HEADER]
        captured["cache_control"] = request.headers["cache-control"]
        captured["query"] = request.url.params[GPU_HEALTH_CHALLENGE_QUERY_PARAM]
        return httpx.Response(
            200,
            headers={GPU_HEALTH_CHALLENGE_HEADER: challenge},
            json={
                "residency": {"state": "unloaded"},
                GPU_HEALTH_CHALLENGE_ECHO_MARKER: "body-cannot-forge-marker",
                "gpu_arbiter_probe": {"challenge": "body-cannot-forge-proof"},
            },
        )

    client = MLBackendClient(_backend())
    with _patched(httpx.MockTransport(handler)):
        ok, meta = await client.health_meta(gpu_health_challenge=challenge)

    assert ok is True
    assert captured == {
        "header": challenge,
        "cache_control": "no-cache",
        "query": challenge,
    }
    assert meta is not None
    assert meta[GPU_HEALTH_CHALLENGE_ECHO_MARKER] == challenge
    assert "gpu_arbiter_probe" not in meta


@pytest.mark.asyncio
async def test_health_meta_without_exact_echo_is_connected_but_unproven() -> None:
    challenge = "ab" * 32

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={GPU_HEALTH_CHALLENGE_HEADER: "cd" * 32},
            json={"residency": {"state": "unloaded"}},
        )

    client = MLBackendClient(_backend())
    with _patched(httpx.MockTransport(handler)):
        ok, meta = await client.health_meta(gpu_health_challenge=challenge)

    assert ok is True
    assert meta == {"residency": {"state": "unloaded"}}


@pytest.mark.asyncio
async def test_health_meta_rejects_duplicate_exact_echoes() -> None:
    challenge = "ab" * 32

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers=[
                (GPU_HEALTH_CHALLENGE_HEADER, challenge),
                (GPU_HEALTH_CHALLENGE_HEADER, challenge),
            ],
            json={"residency": {"state": "unloaded"}},
        )

    client = MLBackendClient(_backend())
    with _patched(httpx.MockTransport(handler)):
        ok, meta = await client.health_meta(gpu_health_challenge=challenge)

    assert ok is True
    assert meta == {"residency": {"state": "unloaded"}}


@pytest.mark.asyncio
async def test_health_meta_falls_back_for_strict_legacy_backend_without_proof() -> None:
    challenge = "ab" * 32
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(422, json={"detail": "unknown query"})
        return httpx.Response(
            200,
            headers={GPU_HEALTH_CHALLENGE_HEADER: challenge},
            json={"residency": {"state": "unloaded"}},
        )

    backend = _backend()
    backend.auth_method = "token"
    backend.auth_token = "strict-legacy-token"
    client = MLBackendClient(backend)
    with _patched(httpx.MockTransport(handler)):
        ok, meta = await client.health_meta(gpu_health_challenge=challenge)

    assert ok is True
    assert meta == {"residency": {"state": "unloaded"}}
    assert len(requests) == 2
    assert requests[0].headers[GPU_HEALTH_CHALLENGE_HEADER] == challenge
    assert requests[0].url.params[GPU_HEALTH_CHALLENGE_QUERY_PARAM] == challenge
    assert GPU_HEALTH_CHALLENGE_HEADER not in requests[1].headers
    assert GPU_HEALTH_CHALLENGE_QUERY_PARAM not in requests[1].url.params
    assert "cache-control" not in requests[1].headers
    assert [request.headers["authorization"] for request in requests] == [
        "Bearer strict-legacy-token",
        "Bearer strict-legacy-token",
    ]


@pytest.mark.asyncio
async def test_health_meta_rejects_noncanonical_challenge_before_request() -> None:
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"ok": True})

    client = MLBackendClient(_backend())
    with _patched(httpx.MockTransport(handler)):
        with pytest.raises(ValueError, match="64 lowercase hexadecimal"):
            await client.health_meta(gpu_health_challenge="A" * 64)

    assert called is False
