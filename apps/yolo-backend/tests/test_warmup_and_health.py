"""端点契约: POST /warmup + /health.pool 统一 PoolStatus 格式.

stub torch + ultralytics, 用 FastAPI TestClient 直接打 app, mock predictor 与 pool 的
回调点验证响应 body 形状.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

import pytest
from aap_protocol_v2.lifecycle import (
    GPU_HEALTH_CHALLENGE_HEADER,
    GPU_HEALTH_CHALLENGE_QUERY_PARAM,
)


@pytest.fixture(scope="module", autouse=True)
def _stub_modules() -> None:
    sys.modules.setdefault(
        "torch", MagicMock(cuda=MagicMock(is_available=MagicMock(return_value=False)))
    )
    sys.modules.setdefault("ultralytics", MagicMock())


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    """启动 FastAPI TestClient. lifespan 内会建一个真实 ModelPool, 但 build_model 是
    mock (sys.modules ultralytics MagicMock), 不会真的拉权重."""
    # CHECKPOINTS_DIR 默认 /app/checkpoints, 在测试机上不可写; 改指向 tmp.
    tmp = tmp_path_factory.mktemp("yolo-test-")
    os.environ["YOLO_CHECKPOINTS_DIR"] = str(tmp)
    # main 已被其他测试 import 过, 用 importlib.reload 让 env 重新生效.
    import importlib
    import main

    main = importlib.reload(main)
    from fastapi.testclient import TestClient

    with TestClient(main.app) as c:
        yield c


def test_warmup_first_call_cache_miss(client) -> None:
    resp = client.post(
        "/warmup",
        json={"task": "detection", "variants": {"series": "yolo11", "size": "s"}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["cache_hit"] is False
    assert body["model_load_ms"] is not None
    assert body["evicted"] is None


def test_warmup_second_call_cache_hit(client) -> None:
    client.post(
        "/warmup",
        json={"task": "detection", "variants": {"series": "yolo11", "size": "m"}},
    )
    resp = client.post(
        "/warmup",
        json={"task": "detection", "variants": {"series": "yolo11", "size": "m"}},
    )
    body = resp.json()
    assert body["cache_hit"] is True
    assert body["model_load_ms"] is None


def test_warmup_invalid_variant_returns_422(client) -> None:
    """yolov9 在 detection 没有 size=n 组合, 应返回 422 variant_not_supported."""
    resp = client.post(
        "/warmup",
        json={"task": "detection", "variants": {"series": "yolov9", "size": "n"}},
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error_code"] == "variant_not_supported"
    assert detail["axis"] == "size"


def test_health_pool_uses_pool_status_format(client) -> None:
    """/health.pool 必须用统一 PoolStatus 格式 (cap/current_size/loaded_keys/last_evict)."""
    # 先 warmup 一个让 pool 有内容
    client.post(
        "/warmup",
        json={"task": "detection", "variants": {"series": "yolo11", "size": "s"}},
    )
    resp = client.get("/health")
    pool = resp.json()["pool"]
    assert set(pool.keys()) >= {"cap", "current_size", "loaded_keys", "last_evict"}
    assert isinstance(pool["loaded_keys"], list)
    for entry in pool["loaded_keys"]:
        assert set(entry.keys()) >= {"key", "loaded_at", "last_used_at", "hit_count"}
        # key 是 opaque 字符串 series/size/task
        assert "/" in entry["key"]


def test_health_pool_loaded_key_string_uses_series_size_task(client) -> None:
    client.post(
        "/warmup",
        json={"task": "detection", "variants": {"series": "yolo11", "size": "s"}},
    )
    pool = client.get("/health").json()["pool"]
    keys = [e["key"] for e in pool["loaded_keys"]]
    assert "yolo11/s/detection" in keys


def test_health_declares_cpu_fallback_capability(client) -> None:
    compute = client.get("/health").json()["compute"]
    assert compute["cpu_fallback_supported"] is True


def test_health_echoes_only_exact_gpu_challenge(client) -> None:
    challenge = "ab" * 32
    response = client.get(
        "/health",
        headers={GPU_HEALTH_CHALLENGE_HEADER: challenge},
        params={GPU_HEALTH_CHALLENGE_QUERY_PARAM: challenge},
    )
    assert response.status_code == 200
    assert response.headers[GPU_HEALTH_CHALLENGE_HEADER] == challenge
    assert response.headers["cache-control"] == "no-store"

    ordinary = client.get("/health")
    assert GPU_HEALTH_CHALLENGE_HEADER not in ordinary.headers
    mismatch = client.get(
        "/health",
        headers={GPU_HEALTH_CHALLENGE_HEADER: challenge},
        params={GPU_HEALTH_CHALLENGE_QUERY_PARAM: "cd" * 32},
    )
    assert mismatch.status_code == 200
    assert GPU_HEALTH_CHALLENGE_HEADER not in mismatch.headers

    duplicate_query = client.get(
        "/health",
        headers={GPU_HEALTH_CHALLENGE_HEADER: challenge},
        params=[
            (GPU_HEALTH_CHALLENGE_QUERY_PARAM, challenge),
            (GPU_HEALTH_CHALLENGE_QUERY_PARAM, challenge),
        ],
    )
    assert duplicate_query.status_code == 200
    assert GPU_HEALTH_CHALLENGE_HEADER not in duplicate_query.headers

    duplicate_header = client.get(
        "/health",
        headers=[
            (GPU_HEALTH_CHALLENGE_HEADER, challenge),
            (GPU_HEALTH_CHALLENGE_HEADER, challenge),
        ],
        params={GPU_HEALTH_CHALLENGE_QUERY_PARAM: challenge},
    )
    assert duplicate_header.status_code == 200
    assert GPU_HEALTH_CHALLENGE_HEADER not in duplicate_header.headers


def test_health_survives_cuda_runtime_failure(client, monkeypatch) -> None:
    import main

    def _broken_cuda_check():
        raise RuntimeError("CUDA error: unknown error")

    monkeypatch.setattr(main.torch.cuda, "is_available", _broken_cuda_check)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["gpu_info"]["device_index"] is None
    assert response.json()["gpu_info"]["process_memory_mb"] is None


def test_managed_cleanup_propagates_cuda_runtime_failure(monkeypatch) -> None:
    import main

    monkeypatch.setattr(main.torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(main.torch.cuda, "is_initialized", lambda: True)
    monkeypatch.setattr(main.torch.cuda, "synchronize", lambda: None)
    monkeypatch.setattr(
        main.torch._C,
        "_cuda_clearCublasWorkspaces",
        lambda: None,
    )

    def _broken_empty_cache() -> None:
        raise RuntimeError("CUDA allocator unavailable")

    monkeypatch.setattr(main.torch.cuda, "empty_cache", _broken_empty_cache)

    with pytest.raises(RuntimeError, match="allocator unavailable"):
        main._strict_free_gpu_memory()


def test_managed_cleanup_releases_cublas_workspace_before_allocator(
    monkeypatch,
) -> None:
    import main

    events: list[str] = []
    monkeypatch.setattr(main.torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(main.torch.cuda, "is_initialized", lambda: True)
    monkeypatch.setattr(
        main.torch.cuda,
        "synchronize",
        lambda: events.append("synchronize"),
    )
    monkeypatch.setattr(
        main.torch._C,
        "_cuda_clearCublasWorkspaces",
        lambda: events.append("clear-cublas"),
    )
    monkeypatch.setattr(
        main.torch.cuda,
        "empty_cache",
        lambda: events.append("empty-cache"),
    )
    monkeypatch.setattr(
        main.torch.cuda,
        "ipc_collect",
        lambda: events.append("ipc-collect"),
    )

    main._strict_free_gpu_memory()

    assert events == [
        "synchronize",
        "clear-cublas",
        "empty-cache",
        "ipc-collect",
    ]


def test_managed_cleanup_does_not_initialize_an_unused_cuda_context(
    monkeypatch,
) -> None:
    import main

    synchronize = MagicMock()
    monkeypatch.setattr(main.torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(main.torch.cuda, "is_initialized", lambda: False)
    monkeypatch.setattr(main.torch.cuda, "synchronize", synchronize)

    main._strict_free_gpu_memory()

    synchronize.assert_not_called()


def test_model_move_commits_latch_after_cpu_replacement(monkeypatch) -> None:
    import main

    events: list[str] = []

    class _Model:
        def to(self, device):
            events.append(f"to:{device}")
            if device == "cuda":
                raise RuntimeError("CUDA error: unknown error")
            return self

    monkeypatch.setattr(main, "effective_device", lambda _configured: "cuda")
    monkeypatch.setattr(main, "free_gpu_memory", lambda: events.append("cleanup"))
    monkeypatch.setattr(main, "latch_cpu", lambda _reason: events.append("latch"))

    model = _Model()
    assert main._move_model_to_effective_device(model) is model
    assert events == ["to:cuda", "to:cpu", "cleanup", "latch"]


def test_model_move_cpu_failure_does_not_commit_latch(monkeypatch) -> None:
    import main

    latched: list[str] = []

    class _Model:
        def to(self, device):
            if device == "cuda":
                raise RuntimeError("CUDA error: unknown error")
            raise RuntimeError("CPU move failed")

    monkeypatch.setattr(main, "effective_device", lambda _configured: "cuda")
    monkeypatch.setattr(main, "latch_cpu", latched.append)

    with pytest.raises(RuntimeError, match="CPU move failed"):
        main._move_model_to_effective_device(_Model())
    assert latched == []


def test_model_move_non_device_error_is_not_retried(monkeypatch) -> None:
    import main

    devices: list[str] = []

    class _Model:
        def to(self, device):
            devices.append(device)
            raise ValueError("invalid model")

    monkeypatch.setattr(main, "effective_device", lambda _configured: "cuda")

    with pytest.raises(ValueError, match="invalid model"):
        main._move_model_to_effective_device(_Model())
    assert devices == ["cuda"]
