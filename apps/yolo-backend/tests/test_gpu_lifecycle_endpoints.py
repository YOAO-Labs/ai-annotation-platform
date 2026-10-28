"""HTTP wiring tests for the YOLO managed lifecycle contract."""

from __future__ import annotations

import asyncio
import importlib
import json
import threading
import time

import pytest
from aap_protocol_v2.lifecycle import (
    AdmissionScope,
    AdmissionTokenClaims,
    GPU_ADMISSION_TOKEN_HEADER,
    GPU_GENERATION_HEADER,
    encode_ed25519_public_key,
    sign_admission_token,
)
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class _Model:
    names = {0: "object"}
    device = "cuda:0"


class _OperationRecorder:
    def __init__(self) -> None:
        self.tracked = []

    def track_future(self, future) -> None:
        if future is not None:
            self.tracked.append(future)


@pytest.fixture()
def managed_client(monkeypatch, tmp_path):
    private_key = Ed25519PrivateKey.generate()
    keyring = json.dumps(
        {"current": encode_ed25519_public_key(private_key.public_key())}
    )
    monkeypatch.setenv("GPU_LIFECYCLE_VERIFY_KEYS_JSON", keyring)
    monkeypatch.setenv("GPU_LIFECYCLE_REGISTRY_ID", "backend-1")
    monkeypatch.setenv("GPU_LIFECYCLE_RESOURCE_ID", "node-a/GPU-1")
    monkeypatch.setenv("YOLO_MANAGED_LIFECYCLE_VERIFIED", "1")
    monkeypatch.setenv("YOLO_CHECKPOINTS_DIR", str(tmp_path))

    import main

    main = importlib.reload(main)
    monkeypatch.setattr(main, "_build_model", lambda *_args: _Model())
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
        backend_registry_id="backend-1",
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


def test_setup_and_health_publish_managed_lifecycle(managed_client) -> None:
    _main, client, _private_key = managed_client

    setup = client.get("/setup").json()
    health = client.get("/health").json()

    assert setup["managed_lifecycle"]["protocol_version"] == "1"
    assert setup["managed_lifecycle"]["unload_endpoint"] == "/unload"
    assert health["residency"]["state"] == "unloaded"
    assert health["residency"]["gpu_loaded"] is False
    assert health["residency"]["lifecycle_gate"] == "legacy"


def test_unverified_deployment_does_not_advertise_managed_lifecycle(
    monkeypatch,
) -> None:
    monkeypatch.setenv("YOLO_MANAGED_LIFECYCLE_VERIFIED", "0")
    import main

    main = importlib.reload(main)

    assert "managed_lifecycle" not in main.setup()


def test_invalid_deployment_gate_fails_import(monkeypatch) -> None:
    monkeypatch.setenv("YOLO_MANAGED_LIFECYCLE_VERIFIED", "true")
    import main

    try:
        with pytest.raises(ValueError, match="exactly 0 or 1"):
            importlib.reload(main)
    finally:
        monkeypatch.setenv("YOLO_MANAGED_LIFECYCLE_VERIFIED", "0")
        importlib.reload(main)


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

    for headers in (
        [
            (GPU_ADMISSION_TOKEN_HEADER, "duplicate-token"),
            (GPU_ADMISSION_TOKEN_HEADER, "duplicate-token"),
        ],
        [
            (GPU_GENERATION_HEADER, "1"),
            (GPU_ADMISSION_TOKEN_HEADER, "control-token"),
        ],
    ):
        control = client.post("/lifecycle/mode", headers=headers, content=b"{")
        assert control.status_code == 403
        assert control.json()["detail"]["error_code"] == "gpu_admission_denied"


def test_enforce_rejects_headerless_loading_and_bodyless_unload(managed_client) -> None:
    _main, client, private_key = managed_client
    _promote(client, private_key)

    predict = client.post(
        "/predict",
        json={
            "tasks": [{"id": "t1", "file_path": "unused.jpg"}],
            "context": {
                "type": "detection",
                "model_variants": {"series": "yolo11", "size": "s"},
            },
        },
    )
    warmup = client.post(
        "/warmup",
        json={"task": "detection", "variants": {"series": "yolo11", "size": "s"}},
    )
    unload = client.post("/unload")

    assert predict.status_code == 403
    assert warmup.status_code == 403
    assert unload.status_code == 403
    assert predict.json()["detail"]["error_code"] == "gpu_admission_denied"


def test_invalid_business_body_still_consumes_workload_jti(managed_client) -> None:
    _main, client, private_key = managed_client
    boot_id = _promote(client, private_key)
    headers = _workload_headers(
        private_key,
        boot_id,
        AdmissionScope.PREDICT,
        generation="1",
        jti="predict-once",
    )

    invalid = client.post("/predict", headers=headers, json={})
    replay = client.post(
        "/predict",
        headers=headers,
        json={
            "tasks": [{"id": "t1", "file_path": "unused.jpg"}],
            "context": {
                "type": "detection",
                "model_variants": {"series": "yolo11", "size": "s"},
            },
        },
    )

    assert invalid.status_code == 422
    assert replay.status_code == 403
    assert replay.json()["detail"]["error_code"] == "gpu_admission_denied"


def test_warmup_drain_unload_happy_path_and_tombstone(managed_client) -> None:
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
        json={"task": "detection", "variants": {"series": "yolo11", "size": "s"}},
    )
    assert warmup.status_code == 200, warmup.text
    assert client.get("/health").json()["residency"]["gpu_loaded"] is True

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

    unload = client.post(
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
    assert unload.status_code == 200, unload.text
    assert unload.json()["residency"]["gpu_loaded"] is False
    assert unload.json()["residency"]["generation"] == "2"

    same_generation = client.post(
        "/warmup",
        headers=_workload_headers(
            private_key,
            boot_id,
            AdmissionScope.WARMUP,
            generation="2",
            jti="warmup-stale",
        ),
        json={"task": "detection", "variants": {"series": "yolo11", "size": "s"}},
    )
    assert same_generation.status_code == 409
    assert same_generation.json()["detail"]["error_code"] == "gpu_generation_conflict"


def test_warmup_timeout_keeps_endpoint_operation_active_until_builder_finishes(
    managed_client,
) -> None:
    main, client, private_key = managed_client
    boot_id = _promote(client, private_key)
    started = threading.Event()
    release = threading.Event()

    def build(_task: str, _series: str, _size: str) -> _Model:
        started.set()
        assert release.wait(timeout=1.0)
        return _Model()

    assert main._model_pool is not None
    main._model_pool._build_model = build  # noqa: SLF001
    main._model_pool._build_timeout = 0.02  # noqa: SLF001
    try:
        response = client.post(
            "/warmup",
            headers=_workload_headers(
                private_key,
                boot_id,
                AdmissionScope.WARMUP,
                generation="1",
                jti="warmup-timeout",
            ),
            json={
                "task": "detection",
                "variants": {"series": "yolo11", "size": "s"},
            },
        )
        assert response.status_code == 503, response.text
        assert started.wait(timeout=1.0)

        residency = client.get("/health").json()["residency"]
        assert residency["active_requests"] == 1
        assert residency["builders"] == 1

        release.set()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            residency = client.get("/health").json()["residency"]
            if residency["active_requests"] == 0 and residency["builders"] == 0:
                break
            time.sleep(0.01)
        assert residency["active_requests"] == 0
        assert residency["builders"] == 0
    finally:
        release.set()


@pytest.mark.asyncio
async def test_run_warmup_cancellation_tracks_real_builder(monkeypatch) -> None:
    import main
    from model_pool import ModelPool

    started = threading.Event()
    release = threading.Event()

    def build(_task: str, _series: str, _size: str) -> _Model:
        started.set()
        assert release.wait(timeout=1.0)
        return _Model()

    pool = ModelPool(1, build, lambda: None, build_timeout=1.0)
    monkeypatch.setattr(main, "_model_pool", pool)

    async def forbidden_builder_lookup(*_args):
        raise AssertionError("cancel handling must not yield before tracking builder")

    monkeypatch.setattr(pool, "builder_for", forbidden_builder_lookup)
    operation = _OperationRecorder()
    request = main.WarmupRequest.model_validate(
        {"task": "detection", "variants": {"series": "yolo11", "size": "s"}}
    )
    run = asyncio.create_task(main._run_warmup(request, operation=operation))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(started.wait), timeout=1.0)
        run.cancel()
        await asyncio.sleep(0)
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run
        assert len(operation.tracked) == 1
        assert operation.tracked[0].done() is False
    finally:
        release.set()
    await operation.tracked[0]


@pytest.mark.asyncio
async def test_run_predict_cancellation_tracks_real_builder(monkeypatch) -> None:
    import main
    from model_pool import ModelPool
    from predictor import YoloPredictor

    started = threading.Event()
    release = threading.Event()

    def build(_task: str, _series: str, _size: str) -> _Model:
        started.set()
        assert release.wait(timeout=1.0)
        return _Model()

    pool = ModelPool(1, build, lambda: None, build_timeout=1.0)
    monkeypatch.setattr(main, "_model_pool", pool)
    monkeypatch.setattr(main, "_predictor", YoloPredictor(pool))

    async def forbidden_builder_lookup(*_args):
        raise AssertionError("cancel handling must not yield before tracking builder")

    monkeypatch.setattr(pool, "builder_for", forbidden_builder_lookup)
    operation = _OperationRecorder()
    request = main.BatchPredictRequest.model_validate(
        {
            "tasks": [{"id": "t1", "file_path": "unused.jpg"}],
            "context": {
                "type": "detection",
                "model_variants": {"series": "yolo11", "size": "s"},
            },
        }
    )
    run = asyncio.create_task(main._run_predict(request, operation=operation))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(started.wait), timeout=1.0)
        run.cancel()
        await asyncio.sleep(0)
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run
        assert len(operation.tracked) == 1
        assert operation.tracked[0].done() is False
    finally:
        release.set()
    await operation.tracked[0]


def test_invalid_nonempty_keyring_fails_startup(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("GPU_LIFECYCLE_VERIFY_KEYS_JSON", "not-json")
    monkeypatch.setenv("YOLO_CHECKPOINTS_DIR", str(tmp_path))
    import main

    main = importlib.reload(main)
    from fastapi.testclient import TestClient

    with pytest.raises(ValueError, match="verify keyring"):
        with TestClient(main.app):
            pass
