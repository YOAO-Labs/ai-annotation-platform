"""HTTP wiring tests for the ONNXTools managed lifecycle contract."""

from __future__ import annotations

import asyncio
import importlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from aap_protocol_v2.lifecycle import (
    AdmissionScope,
    AdmissionTokenClaims,
    GPU_ADMISSION_TOKEN_HEADER,
    GPU_GENERATION_HEADER,
    GPU_HEALTH_CHALLENGE_HEADER,
    GPU_HEALTH_CHALLENGE_QUERY_PARAM,
    encode_ed25519_public_key,
    sign_admission_token,
)
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from starlette.datastructures import Headers


class _Session:
    def get_providers(self) -> list[str]:
        return ["CUDAExecutionProvider", "CPUExecutionProvider"]


class _Detector:
    _onnx_session = _Session()
    class_names = {0: "car"}


class _Classifier:
    _onnx_session = _Session()


class _Pipeline:
    detector = _Detector()
    va_classifier = _Classifier()


@pytest.fixture()
def managed_client(monkeypatch):
    private_key = Ed25519PrivateKey.generate()
    keyring = json.dumps(
        {"current": encode_ed25519_public_key(private_key.public_key())}
    )
    monkeypatch.setenv("GPU_LIFECYCLE_VERIFY_KEYS_JSON", keyring)
    monkeypatch.setenv("ONNXTOOLS_MANAGED_LIFECYCLE_VERIFIED", "1")

    import main

    main = importlib.reload(main)
    monkeypatch.setattr(main, "_probe_providers", lambda: ["CUDAExecutionProvider"])
    monkeypatch.setattr(main, "_make_detector", _Detector)
    monkeypatch.setattr(main, "_make_va_classifier", _Classifier)
    monkeypatch.setattr(main, "_make_pipeline", _Pipeline)
    from fastapi.testclient import TestClient

    with TestClient(main.app) as client:
        yield main, client, private_key


def _token(
    private_key: Ed25519PrivateKey,
    boot_id: str,
    scope: AdmissionScope,
    *,
    jti: str,
    generation: str | None = None,
    control_epoch: str = "1",
    owner: str = "platform-1",
    operation: str = "operation-1",
) -> str:
    control = scope in {
        AdmissionScope.DRAIN,
        AdmissionScope.UNLOAD,
        AdmissionScope.RESUME,
        AdmissionScope.MODE,
        AdmissionScope.RESET,
    }
    claims = AdmissionTokenClaims(
        backend_registry_id="backend-onnx",
        gpu_resource_id="node-a/GPU-1",
        boot_id=boot_id,
        generation=generation,
        control_epoch=control_epoch,
        scope=scope,
        jti=jti,
        exp=int(time.time()) + 60,
        owner=owner if control else None,
        operation=operation if control else None,
    )
    return sign_admission_token(claims, private_key=private_key, kid="current")


def _promote(client, private_key: Ed25519PrivateKey) -> str:
    boot_id = client.get("/health").json()["residency"]["boot_id"]
    response = client.post(
        "/lifecycle/mode",
        headers={
            "X-AAP-GPU-Admission-Token": _token(
                private_key,
                boot_id,
                AdmissionScope.MODE,
                jti="mode-1",
                operation="mode-1",
            )
        },
        json={"gate": "enforce", "control_epoch": "1"},
    )
    assert response.status_code == 200, response.text
    return boot_id


def test_health_echoes_only_exact_gpu_challenge(managed_client) -> None:
    _, client, _ = managed_client
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


def test_health_reports_physical_gpu_selection(managed_client, monkeypatch) -> None:
    _, client, _ = managed_client
    monkeypatch.setenv(
        "AAP_GPU_PHYSICAL_DEVICE_TOKEN",
        "GPU-physical-a",
    )
    monkeypatch.setenv("NVIDIA_VISIBLE_DEVICES", "void")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0")

    assert client.get("/health").json()["gpu_info"] == {
        "physical_device_token": "GPU-physical-a",
        "device_uuid": "GPU-physical-a",
    }


def _workload_headers(
    private_key: Ed25519PrivateKey,
    boot_id: str,
    scope: AdmissionScope,
    *,
    generation: str,
    jti: str,
) -> dict[str, str]:
    return {
        "X-AAP-GPU-Generation": generation,
        "X-AAP-GPU-Admission-Token": _token(
            private_key,
            boot_id,
            scope,
            generation=generation,
            jti=jti,
        ),
    }


def test_setup_and_health_publish_verified_managed_lifecycle(managed_client) -> None:
    _main, client, _private_key = managed_client

    setup = client.get("/setup").json()
    health = client.get("/health").json()

    assert setup["managed_lifecycle"]["protocol_version"] == "1"
    assert health["residency"]["state"] == "unloaded"
    assert health["residency"]["gpu_loaded"] is False
    assert set(health["residency"]["pools"]) == {"pipeline", "detector", "va"}
    assert all(
        pool["resident"] is False for pool in health["residency"]["pools"].values()
    )


def test_legacy_wire_rejects_partial_duplicate_and_bodyless_managed_headers(
    managed_client,
) -> None:
    _main, client, private_key = managed_client
    boot_id = client.get("/health").json()["residency"]["boot_id"]
    token = _token(
        private_key,
        boot_id,
        AdmissionScope.WARMUP,
        generation="1",
        jti="legacy-header-guard",
    )
    workload_headers = [
        [(GPU_GENERATION_HEADER, "1")],
        [(GPU_ADMISSION_TOKEN_HEADER, token)],
        [
            (GPU_GENERATION_HEADER, "1"),
            (GPU_GENERATION_HEADER, "1"),
            (GPU_ADMISSION_TOKEN_HEADER, token),
        ],
        [
            (GPU_GENERATION_HEADER, "1"),
            (GPU_ADMISSION_TOKEN_HEADER, token),
            (GPU_ADMISSION_TOKEN_HEADER, token),
        ],
    ]

    for headers in workload_headers:
        response = client.post("/warmup", headers=headers, content=b"{")
        assert response.status_code == 403
        assert response.json()["detail"]["error_code"] == "gpu_admission_denied"

    unload = client.post(
        "/unload",
        headers={
            GPU_GENERATION_HEADER: "1",
            GPU_ADMISSION_TOKEN_HEADER: token,
        },
    )
    assert unload.status_code == 403
    assert unload.json()["detail"]["error_code"] == "gpu_admission_denied"


def test_legacy_wire_still_warms_and_bodyless_unloads(managed_client) -> None:
    _main, client, _private_key = managed_client

    warmup = client.post("/warmup")
    unload = client.post("/unload")

    assert warmup.status_code == 200, warmup.text
    assert unload.status_code == 200, unload.text
    assert unload.json() == {"ok": True, "unloaded": 1}
    assert client.get("/health").json()["residency"]["gpu_loaded"] is False


def test_enforce_rejects_headerless_loading_and_bodyless_unload(managed_client) -> None:
    _main, client, private_key = managed_client
    _promote(client, private_key)

    predict = client.post("/predict", json={"tasks": [], "context": {}})
    warmup = client.post("/warmup", json={"model_id": "vehicle-detect"})
    unload = client.post("/unload")

    assert predict.status_code == 403
    assert warmup.status_code == 403
    assert unload.status_code == 403
    assert predict.json()["detail"]["error_code"] == "gpu_admission_denied"


def test_invalid_body_consumes_workload_jti_before_validation(managed_client) -> None:
    _main, client, private_key = managed_client
    boot_id = _promote(client, private_key)
    headers = _workload_headers(
        private_key,
        boot_id,
        AdmissionScope.PREDICT,
        generation="1",
        jti="predict-once",
    )

    invalid = client.post("/predict", headers=headers, json={"tasks": "bad"})
    replay = client.post(
        "/predict",
        headers=headers,
        json={"tasks": [], "context": {}},
    )

    assert invalid.status_code == 422
    assert replay.status_code == 403
    assert replay.json()["detail"]["error_code"] == "gpu_admission_denied"


def test_warmup_drain_unload_and_generation_tombstone(managed_client) -> None:
    _main, client, private_key = managed_client
    boot_id = _promote(client, private_key)
    warmup = client.post(
        "/warmup",
        headers=_workload_headers(
            private_key,
            boot_id,
            AdmissionScope.WARMUP,
            generation="1",
            jti="warmup-1",
        ),
        json={"model_id": "vehicle-detect"},
    )
    assert warmup.status_code == 200, warmup.text
    residency = client.get("/health").json()["residency"]
    assert residency["gpu_loaded"] is True
    assert residency["evictable"] is True

    owner = "evictor-1"
    operation = "drain-unload-1"
    drain = client.post(
        "/drain",
        headers={
            "X-AAP-GPU-Generation": "2",
            "X-AAP-GPU-Admission-Token": _token(
                private_key,
                boot_id,
                AdmissionScope.DRAIN,
                generation="2",
                jti="drain-1",
                owner=owner,
                operation=operation,
            ),
        },
        json={"generation": "2"},
    )
    assert drain.status_code == 200, drain.text
    assert drain.json()["ready_to_unload"] is True

    unloaded = client.post(
        "/unload",
        headers={
            "X-AAP-GPU-Generation": "2",
            "X-AAP-GPU-Admission-Token": _token(
                private_key,
                boot_id,
                AdmissionScope.UNLOAD,
                generation="2",
                jti="unload-1",
                owner=owner,
                operation=operation,
            ),
        },
        json={"generation": "2"},
    )
    assert unloaded.status_code == 200, unloaded.text
    assert unloaded.json()["residency"]["gpu_loaded"] is False
    assert unloaded.json()["residency"]["generation"] == "2"

    stale = client.post(
        "/warmup",
        headers=_workload_headers(
            private_key,
            boot_id,
            AdmissionScope.WARMUP,
            generation="2",
            jti="warmup-stale",
        ),
        json={"model_id": "vehicle-detect"},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["error_code"] == "gpu_generation_conflict"


def test_warmup_timeout_keeps_active_until_real_builder_finishes(
    managed_client,
    monkeypatch,
) -> None:
    main, client, private_key = managed_client
    boot_id = _promote(client, private_key)
    started = threading.Event()
    release = threading.Event()
    tracked = threading.Event()
    track_future = main.WorkloadOperation.track_future

    def observe_future(operation, future) -> None:
        track_future(operation, future)
        if future is not None:
            tracked.set()

    monkeypatch.setattr(main.WorkloadOperation, "track_future", observe_future)

    def build() -> _Detector:
        started.set()
        release.wait()
        return _Detector()

    assert main._handle_pool is not None
    main._handle_pool._factories["detector"] = build  # noqa: SLF001
    main._handle_pool._build_timeout = 0.02  # noqa: SLF001
    with ThreadPoolExecutor(max_workers=1) as executor:
        response_future = executor.submit(
            client.post,
            "/warmup",
            headers=_workload_headers(
                private_key,
                boot_id,
                AdmissionScope.WARMUP,
                generation="1",
                jti="warmup-timeout",
            ),
            json={"model_id": "vehicle-detect"},
        )
        try:
            assert started.wait(timeout=1)
            assert tracked.wait(timeout=1)
            assert not response_future.done()
            residency = client.get("/health").json()["residency"]
            assert residency["active_requests"] == 1
            assert residency["builders"] == 1
        finally:
            release.set()
        response = response_future.result(timeout=5)
        assert response.status_code == 503, response.text
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            residency = client.get("/health").json()["residency"]
            if residency["active_requests"] == 0 and residency["builders"] == 0:
                break
            time.sleep(0.01)
        assert residency["active_requests"] == 0
        assert residency["builders"] == 0


@pytest.mark.asyncio
async def test_repeated_cancel_keeps_warmup_active_until_real_builder_finishes(
    monkeypatch,
) -> None:
    import main
    from gpu_lifecycle import OnnxToolsGpuLifecycle
    from handle_pool import HandlePool

    started = threading.Event()
    release = threading.Event()

    def build_detector() -> _Detector:
        started.set()
        assert release.wait(timeout=2)
        return _Detector()

    pool = HandlePool(
        {
            "pipeline": _Pipeline,
            "detector": build_detector,
            "va": _Classifier,
        },
        main.inspect_handle_providers,
        build_timeout=1.0,
    )
    lifecycle = OnnxToolsGpuLifecycle(
        pool,
        verify_keyring={},
        boot_id="boot-cancel",
    )
    monkeypatch.setattr(main, "_handle_pool", pool)
    monkeypatch.setattr(main, "_gpu_lifecycle", lifecycle)

    async def forbidden_builder_lookup(_name: str):
        raise AssertionError(
            "cancel handling must carry the real builder synchronously"
        )

    monkeypatch.setattr(pool, "builder_for", forbidden_builder_lookup)

    class _Request:
        headers = Headers()
        app = main.app

        async def body(self) -> bytes:
            return b'{"model_id":"vehicle-detect"}'

        async def json(self) -> dict[str, str]:
            return {"model_id": "vehicle-detect"}

    task = asyncio.create_task(main.warmup(_Request()))  # type: ignore[arg-type]
    try:
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        residency = await lifecycle.residency()
        assert residency.active_requests == 1
        assert residency.builders == 1

        release.set()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            residency = await lifecycle.residency()
            if residency.active_requests == 0 and residency.builders == 0:
                break
            await asyncio.sleep(0.01)
        assert residency.active_requests == 0
        assert residency.builders == 0
    finally:
        release.set()
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await lifecycle.shutdown()


def test_unverified_deployment_does_not_advertise_managed_lifecycle(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ONNXTOOLS_MANAGED_LIFECYCLE_VERIFIED", "0")
    import main

    main = importlib.reload(main)

    assert "managed_lifecycle" not in main.setup()


def test_invalid_deployment_gate_fails_import(monkeypatch) -> None:
    monkeypatch.setenv("ONNXTOOLS_MANAGED_LIFECYCLE_VERIFIED", "true")
    import main

    try:
        with pytest.raises(ValueError, match="exactly 0 or 1"):
            importlib.reload(main)
    finally:
        monkeypatch.setenv("ONNXTOOLS_MANAGED_LIFECYCLE_VERIFIED", "0")
        importlib.reload(main)


def test_invalid_nonempty_keyring_fails_startup(monkeypatch) -> None:
    monkeypatch.setenv("GPU_LIFECYCLE_VERIFY_KEYS_JSON", "not-json")
    import main

    main = importlib.reload(main)
    from fastapi.testclient import TestClient

    with pytest.raises(ValueError, match="verify keyring"):
        with TestClient(main.app):
            pass
