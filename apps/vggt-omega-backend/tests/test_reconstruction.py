from __future__ import annotations

import asyncio
import hashlib
import importlib
import io
import sys
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import httpx
import numpy as np
import pytest
from aap_backend_runtime import BuildArtifact, ManagedLruPool
from aap_protocol_v2.lifecycle import (
    AdmissionScope,
    AdmissionTokenClaims,
    sign_admission_token,
)
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError

from config import OUTPUT_HEIGHT, OUTPUT_WIDTH, PROFILE, Config
from predictor import (
    PredictionArrays,
    ProducerUnavailable,
    preprocessing_for,
    strict_gpu_cleanup,
    validate_vendor,
    validate_weights,
)
from schemas import ReconstructRequest
from service import ReconstructionError, ReconstructionService


@pytest.fixture
def image_bytes():
    stream = io.BytesIO()
    Image.new("RGB", (1600, 900), (10, 50, 100)).save(stream, format="JPEG")
    return stream.getvalue()


@pytest.fixture
def envelope(image_bytes):
    return {
        "protocol_version": "1",
        "request_id": "request-1",
        "idempotency_key": "unit-1",
        "input_digest": "a" * 64,
        "profile": PROFILE,
        "cameras": [
            {
                "role": f"CAM_{i}",
                "image_url": f"https://objects/source/{i}",
                "sha256": hashlib.sha256(image_bytes).hexdigest(),
                "width": 1600,
                "height": 900,
            }
            for i in range(6)
        ],
        "upload_targets": [
            {
                "role": f"CAM_{i}",
                "depth": {"url": f"https://objects/staging/{i}/depth"},
                "confidence": {"url": f"https://objects/staging/{i}/confidence"},
            }
            for i in range(6)
        ],
    }


class FakeModel:
    device = "cuda:0"

    def __init__(self):
        self.calls = 0

    def predict(self, paths):
        assert len(paths) == 6
        assert all(path.is_file() for path in paths)
        self.calls += 1
        shape = (6, OUTPUT_HEIGHT, OUTPUT_WIDTH)
        return PredictionArrays(
            np.ones(shape, dtype=np.float32), np.full(shape, 2, dtype=np.float32), 27
        )


class Operation:
    def __init__(self):
        self.futures = []

    def track_future(self, future):
        if future is not None:
            self.futures.append(future)


def make_service(tmp_path, image_bytes, *, fail_put=False):
    model = FakeModel()
    pool = ManagedLruPool(1, lambda _: BuildArtifact(model), str, lambda: None)
    calls = []

    def transfer(request):
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, content=image_bytes)
        return httpx.Response(500 if fail_put else 200)

    config = replace(Config.from_environment(), weights=tmp_path / "unused")
    return (
        ReconstructionService(pool, config, transport=httpx.MockTransport(transfer)),
        model,
        calls,
    )


def test_source_snapshot_and_weights_integrity(tmp_path):
    validate_vendor()
    with pytest.raises(ProducerUnavailable, match="not provisioned"):
        validate_weights(tmp_path / "missing.pt")
    wrong = tmp_path / "wrong.pt"
    wrong.write_bytes(b"wrong checkpoint")
    with pytest.raises(ProducerUnavailable, match="integrity"):
        validate_weights(wrong)


def test_empty_unload_does_not_create_cuda_context(monkeypatch):
    def forbidden():
        raise AssertionError("empty unload must not initialize CUDA")

    monkeypatch.setitem(
        sys.modules,
        "torch",
        SimpleNamespace(
            cuda=SimpleNamespace(
                is_available=lambda: True,
                is_initialized=lambda: False,
                synchronize=forbidden,
            )
        ),
    )
    strict_gpu_cleanup()


def test_unload_clears_workspaces_and_surfaces_cleanup_failure(monkeypatch):
    calls = []

    def ipc_failure():
        raise RuntimeError("IPC cleanup failed")

    monkeypatch.setitem(
        sys.modules,
        "torch",
        SimpleNamespace(
            cuda=SimpleNamespace(
                is_available=lambda: True,
                is_initialized=lambda: True,
                synchronize=lambda: calls.append("synchronize"),
                empty_cache=lambda: calls.append("empty_cache"),
                ipc_collect=ipc_failure,
            ),
            clear_autocast_cache=lambda: calls.append("autocast"),
            _C=SimpleNamespace(_cuda_clearCublasWorkspaces=lambda: calls.append("cublas")),
        ),
    )
    with pytest.raises(RuntimeError, match="IPC cleanup failed"):
        strict_gpu_cleanup()
    assert calls == ["synchronize", "autocast", "cublas", "empty_cache"]


def test_pixel_center_affine_and_qualified_grid():
    transform = preprocessing_for(1600, 900)
    forward = np.array(transform.source_to_output).reshape(3, 3)
    inverse = np.array(transform.output_to_source).reshape(3, 3)
    np.testing.assert_allclose(forward @ inverse, np.eye(3), atol=1e-12)
    np.testing.assert_allclose(forward @ [0, 0, 1], [-0.3, 352 / 900 * 0.5 - 0.5, 1])
    with pytest.raises(ValueError, match="not qualified"):
        preprocessing_for(1000, 1000)


@pytest.mark.parametrize("change", ["duplicate", "count", "role", "scheme", "upload", "headers"])
def test_invalid_input_contract(envelope, change):
    if change == "duplicate":
        envelope["cameras"][1]["role"] = envelope["cameras"][0]["role"]
    elif change == "count":
        envelope["cameras"].pop()
    elif change == "role":
        envelope["upload_targets"].reverse()
    elif change == "scheme":
        envelope["cameras"][0]["image_url"] = "file:///etc/passwd"
    elif change == "upload":
        envelope["upload_targets"][0]["depth"]["url"] = envelope["upload_targets"][0]["confidence"][
            "url"
        ]
    else:
        envelope["upload_targets"][0]["depth"]["headers"] = {"Authorization": "forbidden"}
    with pytest.raises(ValidationError):
        ReconstructRequest.model_validate(envelope)


@pytest.mark.asyncio
async def test_binary_export_and_fresh_url_idempotent_retry(tmp_path, image_bytes, envelope):
    service, model, calls = make_service(tmp_path, image_bytes)
    first = await service.reconstruct(ReconstructRequest.model_validate(envelope), Operation())
    assert first.cache_hit is False
    assert first.timings.inference_ms == 27
    assert model.calls == 1
    puts = [request for request in calls if request.method == "PUT"]
    assert len(puts) == 12
    assert len([request for request in calls if request.method == "GET"]) == 6
    artifact = np.load(io.BytesIO(puts[0].content), allow_pickle=False)
    assert artifact.shape == (352, 640)
    assert artifact.dtype == np.dtype("<f4")
    assert hashlib.sha256(puts[0].content).hexdigest() == first.cameras[0].depth.sha256
    assert len(puts[0].content) == first.cameras[0].depth.byte_size
    assert "predictions" not in first.model_dump()
    for target in envelope["upload_targets"]:
        for kind in ("depth", "confidence"):
            target[kind]["url"] += "?new-signature"
    second = await service.reconstruct(ReconstructRequest.model_validate(envelope), Operation())
    assert second.cache_hit is True
    assert model.calls == 1
    assert str(calls[-1].url).endswith("?new-signature")
    assert len([request for request in calls if request.method == "PUT"]) == 24
    assert first.cameras == second.cameras
    assert all(not hasattr(value, "url") for value in service.cache.values())
    envelope["input_digest"] = "b" * 64
    with pytest.raises(ReconstructionError, match="different immutable inputs") as error:
        await service.reconstruct(ReconstructRequest.model_validate(envelope), Operation())
    assert error.value.status == 409
    await service.pool.shutdown()


@pytest.mark.asyncio
async def test_source_hash_and_aspect_reject_before_build(tmp_path, image_bytes, envelope):
    service, model, calls = make_service(tmp_path, image_bytes)
    envelope["cameras"][0]["sha256"] = "f" * 64
    with pytest.raises(ReconstructionError) as error:
        await service.reconstruct(ReconstructRequest.model_validate(envelope), Operation())
    assert error.value.code == "source_hash_mismatch"
    assert model.calls == 0
    envelope["cameras"][0]["width"] = 900
    with pytest.raises(ReconstructionError) as error:
        await service.reconstruct(ReconstructRequest.model_validate(envelope), Operation())
    assert error.value.code == "unsupported_image_layout"
    assert len(calls) == 1
    await service.pool.shutdown()


@pytest.mark.asyncio
async def test_upload_fault_retains_only_bounded_cpu_retry_data(tmp_path, image_bytes, envelope):
    service, model, calls = make_service(tmp_path, image_bytes, fail_put=True)
    with pytest.raises(ReconstructionError) as error:
        await service.reconstruct(ReconstructRequest.model_validate(envelope), Operation())
    assert error.value.code == "artifact_transfer_failed"
    assert model.calls == 1
    assert sum(value.byte_size for value in service.cache.values()) <= service.config.cache_bytes
    service.transport = httpx.MockTransport(lambda _: httpx.Response(204))
    result = await service.reconstruct(ReconstructRequest.model_validate(envelope), Operation())
    assert result.cache_hit
    assert model.calls == 1
    await service.pool.shutdown()


@pytest.mark.asyncio
async def test_cancelled_http_keeps_borrower_and_source_files_until_gpu_thread_exits(
    tmp_path, image_bytes, envelope
):
    service, model, _ = make_service(tmp_path, image_bytes)
    entered, release = threading.Event(), threading.Event()
    source_paths = []

    def slow_predict(paths):
        source_paths.extend(paths)
        entered.set()
        assert release.wait(timeout=5)
        return FakeModel.predict(model, paths)

    model.predict = slow_predict
    operation = Operation()
    task = asyncio.create_task(
        service.reconstruct(ReconstructRequest.model_validate(envelope), operation)
    )
    assert await asyncio.wait_for(asyncio.to_thread(entered.wait), timeout=2)
    try:
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        snapshot = await service.pool.snapshot()
        assert snapshot["borrowers"] == 1
        assert source_paths[0].is_file()
        assert operation.futures and not operation.futures[0].done()
        with pytest.raises(ReconstructionError) as error:
            await service.reconstruct(ReconstructRequest.model_validate(envelope), Operation())
        assert error.value.code == "producer_busy"
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await service.pool.snapshot())["borrowers"] == 0
    assert not source_paths[0].exists()
    await service.pool.shutdown()


@pytest.fixture
def backend(monkeypatch, tmp_path, image_bytes):
    monkeypatch.setenv("VGGT_OMEGA_AUTH_TOKEN", "secret")
    monkeypatch.setenv("VGGT_OMEGA_WEIGHTS", str(tmp_path / "missing.pt"))
    monkeypatch.setenv("VGGT_OMEGA_MANAGED_LIFECYCLE_VERIFIED", "0")
    monkeypatch.delenv("AAP_GPU_PHYSICAL_DEVICE_TOKEN", raising=False)
    monkeypatch.delenv("GPU_LIFECYCLE_VERIFY_KEYS_JSON", raising=False)
    import main

    main = importlib.reload(main)
    monkeypatch.setattr(main, "strict_gpu_cleanup", lambda: None)
    with TestClient(main.app, headers={"Authorization": "Bearer secret"}) as client:
        yield main, client


def test_setup_health_auth_and_unprovisioned_warmup(backend):
    main, client = backend
    setup = client.get("/setup").json()
    assert "managed_lifecycle" not in setup
    assert setup["models"] == []
    assert setup["supported_geometric_outputs"] == []
    assert setup["point_cloud_densification"]["profiles"][0]["execution_shape"] == "B6S1"
    assert setup["point_cloud_densification"]["profiles"][0]["request_timeout_seconds"] == (
        main._config.build_timeout + 18 * main._config.transfer_timeout + 300
    )
    assert client.get("/health", headers={"Authorization": "Bearer wrong"}).status_code == 401
    health = client.get(
        "/health",
        params={"aap_gpu_health_challenge": "a" * 64},
        headers={"X-AAP-GPU-Health-Challenge": "a" * 64},
    )
    assert health.status_code == 503
    assert health.headers["X-AAP-GPU-Health-Challenge"] == "a" * 64
    assert health.json()["compute"]["cpu_fallback_supported"] is False
    assert health.json()["ready"] is False
    assert health.json()["residency"]["gpu_loaded"] is False
    assert client.post("/warmup").status_code == 503
    assert client.post("/unload").json() == {"ok": True, "unloaded": 0}
    main._config.weights.touch()
    provisioned = client.get("/health")
    assert provisioned.status_code == 200
    assert provisioned.json()["ready"] is True
    assert provisioned.json()["weights_provisioned"] is True


def test_reconstruct_lifecycle_header_validation_precedes_body(backend):
    _, client = backend
    response = client.post("/reconstruct", headers={"X-AAP-GPU-Generation": "1"}, content=b"{")
    assert response.status_code == 403
    assert response.json()["detail"]["error_code"] == "gpu_admission_denied"


def signed_token(private_key, boot_id, scope, jti, *, generation=None):
    return sign_admission_token(
        AdmissionTokenClaims(
            backend_registry_id="backend-1",
            gpu_resource_id="node/GPU-1",
            boot_id=boot_id,
            control_epoch="1",
            generation=generation,
            scope=scope,
            jti=jti,
            exp=int(time.time()) + 60,
            owner="platform-1" if scope == AdmissionScope.MODE else None,
            operation="operation-1" if scope == AdmissionScope.MODE else None,
        ),
        private_key=private_key,
        kid="current",
    )


def test_unqualified_backend_cannot_enter_enforce(backend):
    main, client = backend
    key = Ed25519PrivateKey.generate()
    main._lifecycle._verify_keyring = {"current": key.public_key()}
    boot_id = client.get("/health").json()["residency"]["boot_id"]
    response = client.post(
        "/lifecycle/mode",
        json={"gate": "enforce", "control_epoch": "1"},
        headers={
            "X-AAP-GPU-Admission-Token": signed_token(key, boot_id, AdmissionScope.MODE, "mode-1")
        },
    )
    assert response.status_code == 409
    assert "verification" in response.json()["detail"]["message"]


def test_enforce_blocks_missing_admission_and_consumes_invalid_body_jti(backend):
    main, client = backend
    key = Ed25519PrivateKey.generate()
    main._lifecycle._verify_keyring = {"current": key.public_key()}
    # This is a wire test, not real device qualification.
    main._lifecycle._evictable_verified = True
    boot_id = client.get("/health").json()["residency"]["boot_id"]
    mode = client.post(
        "/lifecycle/mode",
        json={"gate": "enforce", "control_epoch": "1"},
        headers={
            "X-AAP-GPU-Admission-Token": signed_token(key, boot_id, AdmissionScope.MODE, "mode-1")
        },
    )
    assert mode.status_code == 200, mode.text
    assert client.post("/reconstruct", json={}).status_code == 403
    headers = {
        "X-AAP-GPU-Generation": "1",
        "X-AAP-GPU-Admission-Token": signed_token(
            key, boot_id, AdmissionScope.PREDICT, "predict-once", generation="1"
        ),
    }
    assert client.post("/reconstruct", headers=headers, json={}).status_code == 422
    assert client.post("/reconstruct", headers=headers, json={}).status_code == 403


def test_real_endpoint_uses_artifact_contract(backend, tmp_path, image_bytes, envelope):
    main, client = backend
    service, model, calls = make_service(tmp_path, image_bytes)
    main._service = service
    main._pool = service.pool
    main._lifecycle._pool = service.pool
    response = client.post("/reconstruct", json=envelope)
    assert response.status_code == 200, response.text
    assert response.json()["producer"]["execution_shape"] == "B6S1"
    assert model.calls == 1
    assert len([request for request in calls if request.method == "PUT"]) == 12
