"""Production producer identity, geometry boundaries and admitted transport."""

from contextlib import asynccontextmanager
import copy
import hashlib
import io
import json
from types import SimpleNamespace
from unittest.mock import patch
import uuid

import httpx
import numpy as np
from PIL import Image
import pytest
from pydantic import ValidationError

from app.db.models.ml_backend_registry import MLBackendRegistry
from app.schemas.point_cloud_densification import (
    ArrayArtifact,
    OMEGA_SOURCE_COMMIT,
    OMEGA_WEIGHTS_SHA256,
    PointCloudDensificationRequest,
    ReconstructedCamera,
    ReconstructionResponse,
)
from app.services.ml_capabilities import extract_capabilities
from app.services.ml_client import MLBackendClient
from app.services.ml_routing.capability import (
    canonicalize_capability,
    capability_fingerprint,
    diff_capabilities,
)
from app.services.point_cloud_densification.geometry import unproject
from app.services.point_cloud_densification.kernel import (
    CameraPrediction,
    build_clouds,
    load_array,
    prepare_camera,
)


def capability():
    return {
        "protocol_version": "1",
        "endpoint": "/reconstruct",
        "profiles": [
            {
                "id": "omega-current-mono-512",
                "camera_count": 6,
                "execution_shape": "B6S1",
                "max_width": 4096,
                "max_height": 4096,
                "outputs": ["depth", "confidence"],
            }
        ],
    }


def preprocessing():
    identity = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
    return {
        "source_to_output": identity,
        "output_to_source": identity,
        "output_width": 640,
        "output_height": 352,
        "crop": [0, 0, 640, 352],
        "resize": [640, 352],
        "padding": [0, 0, 0, 0],
        "pixel_convention": "zero-based-center",
    }


def producer_response():
    descriptor = {
        "sha256": "b" * 64,
        "byte_size": 901248,
        "dtype": "<f4",
        "shape": [352, 640],
        "format": "npy",
    }
    return {
        "protocol_version": "1",
        "request_id": "request",
        "idempotency_key": "key",
        "input_digest": "a" * 64,
        "profile": "omega-current-mono-512",
        "producer": {
            "source_commit": OMEGA_SOURCE_COMMIT,
            "weights_sha256": OMEGA_WEIGHTS_SHA256,
            "preprocessing": "balanced-480",
            "execution_shape": "B6S1",
        },
        "cameras": [
            {
                "role": f"camera_{i}",
                "source_sha256": str(i) * 64,
                "source_width": 640,
                "source_height": 352,
                "depth": copy.deepcopy(descriptor),
                "confidence": copy.deepcopy(descriptor),
                "preprocessing": preprocessing(),
            }
            for i in range(6)
        ],
        "timings": {"model_load_ms": None, "inference_ms": 10, "total_ms": 20},
        "cache_hit": False,
    }


def test_producer_namespace_has_no_fake_preannotation_model():
    caps = extract_capabilities(
        {"models": [], "point_cloud_densification": capability()}
    )
    assert caps["models"] == []
    assert caps["modalities"] == ["lidar"]
    assert caps["point_cloud_densification"] == capability()


def test_legacy_service_pool_fingerprints_unchanged_and_producer_is_distinct():
    legacy = {"protocol_version": "1", "model_ids": ["yolo"], "task": "detect"}
    assert capability_fingerprint(legacy) == capability_fingerprint(
        {**legacy, "point_cloud_densification": None}
    )
    assert "point_cloud_densification" not in canonicalize_capability(legacy)
    producer = {**legacy, "point_cloud_densification": capability()}
    assert capability_fingerprint(producer) != capability_fingerprint(legacy)
    assert diff_capabilities(producer, legacy).differing_fields == (
        "point_cloud_densification",
    )
    assert capability_fingerprint(
        canonicalize_capability(producer)
    ) == capability_fingerprint(producer)


@pytest.mark.parametrize("change", ["weights", "grid", "duplicate", "nonfinite"])
def test_reject_unqualified_producer_contract(change):
    payload = producer_response()
    if change == "weights":
        payload["producer"]["weights_sha256"] = "0" * 64
    if change == "grid":
        payload["cameras"][0]["depth"]["shape"] = [1, 2]
    if change == "duplicate":
        payload["cameras"][1]["role"] = payload["cameras"][0]["role"]
    if change == "nonfinite":
        payload["timings"]["total_ms"] = float("nan")
    with pytest.raises(ValidationError):
        ReconstructionResponse.model_validate(payload)


def test_request_scope_rejects_mixed_and_duplicate_ids():
    task_id = uuid.uuid4()
    for payload in [
        {"scope": "task_ids", "task_ids": [task_id, task_id]},
        {"scope": "scene_id", "scene_id": uuid.uuid4(), "task_ids": [task_id]},
    ]:
        with pytest.raises(ValidationError):
            PointCloudDensificationRequest.model_validate(payload)


def test_array_integrity_checks_content_and_actual_dtype():
    stream = io.BytesIO()
    np.save(stream, np.ones((352, 640), dtype="<f4"), allow_pickle=False)
    payload = stream.getvalue()
    descriptor = ArrayArtifact(
        sha256=hashlib.sha256(payload).hexdigest(),
        byte_size=len(payload),
        dtype="<f4",
        shape=[352, 640],
        format="npy",
    )
    assert load_array(payload, descriptor).shape == (352, 640)
    with pytest.raises(ValueError, match="integrity"):
        load_array(payload[:-1] + b"x", descriptor)
    wrong = io.BytesIO()
    np.save(wrong, np.ones((352, 640), dtype="<f8"), allow_pickle=False)
    body = wrong.getvalue()
    descriptor = descriptor.model_copy(
        update={"sha256": hashlib.sha256(body).hexdigest(), "byte_size": len(body)}
    )
    with pytest.raises(ValueError, match="dtype"):
        load_array(body, descriptor)


def test_preprocessing_transform_and_rigid_source_frame_are_enforced():
    stream = io.BytesIO()
    Image.new("RGB", (640, 352), (30, 40, 50)).save(stream, format="PNG")
    source = stream.getvalue()
    row = producer_response()["cameras"][0]
    row["source_sha256"] = hashlib.sha256(source).hexdigest()
    result = ReconstructedCamera.model_validate(row)
    calibration = SimpleNamespace(
        extrinsic=np.eye(4).ravel().tolist(),
        intrinsic=np.eye(3).ravel().tolist(),
        rect=None,
    )
    frozen = SimpleNamespace(role=result.role, calibration=calibration)
    depth = np.full((352, 640), 10.0, dtype=np.float32)
    prepared = prepare_camera(source, frozen, result, depth, np.ones_like(depth))
    assert prepared.image[0, 0].tolist() == [30, 40, 50]
    result.preprocessing.source_to_output[2] = 1
    with pytest.raises(ValueError, match="transform"):
        prepare_camera(source, frozen, result, depth, depth)


def test_generation_preserves_measured_array_and_excludes_its_rays():
    intrinsic = np.array([[100.0, 0.0, 320.0], [0.0, 100.0, 176.0], [0.0, 0.0, 1.0]])
    uv = np.array(
        [(x, y) for x in range(100, 541, 40) for y in range(60, 301, 40)], dtype=float
    )
    measured = unproject(uv, np.full(len(uv), 10.0), intrinsic, np.eye(4))
    original = measured.copy()
    depth = np.full((352, 640), 5.0, dtype=np.float32)
    image = np.full((352, 640, 3), 80, dtype=np.uint8)
    predictions = [
        CameraPrediction(
            f"camera_{i}", image, depth, np.ones_like(depth), intrinsic, np.eye(4)
        )
        for i in range(6)
    ]
    clouds, details = build_clouds(measured, predictions)
    np.testing.assert_array_equal(measured, original)
    assert details["camera_0"]["scale"]["unique_anchor_returns"] >= 32
    for cloud in clouds.values():
        pixel = cloud["pixel"]
        generated_uv = np.column_stack((pixel % 640, pixel // 640))
        assert len(cloud["xyz"]) > 1000
        assert np.all(
            np.min(np.linalg.norm(generated_uv[:, None] - uv[None], axis=-1), axis=1)
            > 3
        )
        np.testing.assert_allclose(cloud["xyz"][:, 2], 10.0, atol=1e-6)
        assert generated_uv[:, 1].max() < int(352 * 0.95)
    with pytest.raises(ValueError, match="insufficient"):
        build_clouds(measured[:10], predictions)


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", [None, "request_id", "camera"])
@pytest.mark.parametrize("use_factory", [False, True])
async def test_reconstruction_uses_gpu_dispatch_and_rejects_stale_responses(
    mismatch, use_factory
):
    response = producer_response()
    request = {
        key: response[key]
        for key in ("request_id", "idempotency_key", "input_digest", "profile")
    }
    request["cameras"] = [
        {
            "role": c["role"],
            "sha256": c["source_sha256"],
            "width": c["source_width"],
            "height": c["source_height"],
        }
        for c in response["cameras"]
    ]
    if mismatch == "request_id":
        response["request_id"] = "older"
    if mismatch == "camera":
        response["cameras"][0]["source_sha256"] = "f" * 64
    backend = MLBackendRegistry(
        id=uuid.uuid4(), url="http://producer:8006", auth_method="none", extra_params={}
    )
    client = MLBackendClient(backend)
    events = []

    @asynccontextmanager
    async def dispatch(operation, scope):
        events.append((operation, str(scope)))
        yield None
        events.append("released")

    client._gpu_dispatch = dispatch

    async def signed_payload():
        assert len(events) == 1 and events[0][0] == "predict"
        events.append("signed_after_admission")
        return request

    payload = signed_payload if use_factory else request
    real = httpx.AsyncClient
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, content=json.dumps(response).encode())
    )
    with patch(
        "app.services.ml_client.httpx.AsyncClient",
        side_effect=lambda **kwargs: real(transport=transport, **kwargs),
    ):
        if mismatch:
            with pytest.raises(ValueError, match="identity"):
                await client.reconstruct(payload)
        else:
            assert (
                await client.reconstruct(payload)
            ).producer.source_commit == OMEGA_SOURCE_COMMIT
    assert events[0][0] == "predict"
    assert events[-1] == "released"
    assert ("signed_after_admission" in events) == use_factory
