"""Tool contracts only: native GPU qualification still requires the target node."""

import copy
import hashlib
import json
import time
import uuid
from types import SimpleNamespace

import httpx
import numpy as np
import pytest
from aap_backend_runtime import exercise_lifecycle_fault_matrix
from aap_backend_runtime.backend_auth import backend_auth_headers
from aap_protocol_v2.lifecycle import (
    AdmissionScope,
    AdmissionTokenClaims,
    GPU_ADMISSION_TOKEN_HEADER,
    GPU_GENERATION_HEADER,
    LifecycleModeRequest,
    sign_admission_token,
)
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from PIL import Image

from managed_models import InstallRequest, ManagedModelStore, digest
from predictor import _emit_detection
from scripts.controlled_segmentation_validation import (
    INPUT_ENV,
    MODEL_INPUT_ENV,
    _fixture_identity,
    _read_fixture,
    load_controlled_fixture,
)


@pytest.fixture()
def controlled(tmp_path, monkeypatch):
    for name in INPUT_ENV:
        monkeypatch.delenv(name, raising=False)
    weights = tmp_path / "best.pt"
    weights.write_bytes(b"synthetic parser-isolated test checkpoint")
    image = tmp_path / "representative.png"
    Image.new("RGB", (11, 7), "white").save(image)
    fixture = {
        "schema": 3,
        "task": "detect",
        "base_model": "yolo26n",
        "framework_version": "8.4.183",
        "frozen_model_ref": {
            "model_version_id": "88123456-1234-4234-8234-123456789abc",
            "sha256": hashlib.sha256(weights.read_bytes()).hexdigest(),
            "classes_digest": digest(["shape"]),
        },
        "classes": ["shape"],
        "confidence": 0.01,
        "max_det": 2,
        "min_instances": 1,
    }
    specification = tmp_path / "controlled.json"
    specification.write_text(json.dumps(fixture))
    for name, value in zip(
        MODEL_INPUT_ENV, (weights, specification, image), strict=True
    ):
        monkeypatch.setenv(name, str(value))
    root = tmp_path / "private"
    root.mkdir()
    return load_controlled_fixture(root)


def _installed(controlled):
    controlled.receipt = {
        **controlled.reference,
        "classes": controlled.fixture["classes"],
        "framework": "ultralytics",
        "framework_version": controlled.fixture["framework_version"],
        "task": "detection",
        **_fixture_identity(controlled.fixture),
    }
    return controlled


@pytest.mark.parametrize("family", ["yolov8", "yolo11", "yolo26"])
@pytest.mark.parametrize("size", list("nsmlx"))
def test_every_detection_family_and_scale_freezes_explicit_native_identity(
    controlled, family, size
):
    controlled.fixture["base_model"] = family + size
    controlled.fixture_json.write_text(json.dumps(controlled.fixture))
    assert _read_fixture(controlled.fixture_json) == controlled.fixture
    identity = _fixture_identity(controlled.fixture)
    assert identity["model_family"] == family
    assert identity["model_size"] == size
    assert identity["runtime_qualification"]["ultralytics"] == "8.4.183"
    assert identity["runtime_qualification"]["end2end"] is (family == "yolo26")
    assert identity["runtime_qualification"]["nms"] is (
        False if family == "yolo26" else None
    )
    assert "output_contract" not in identity


@pytest.mark.parametrize(
    "change",
    [
        {"framework_version": "8.4.177", "base_model": "yolo11n"},
        {"framework_version": "8.4.184"},
        {"base_model": "yolo26n-seg"},
        {"base_model": "libreyolo9t"},
        {"task": "segment"},
        {"task": "detection"},
        {"task": []},
        {"task": None},
        {"schema": []},
        {"schema": True},
        {"model_family": "yolo11"},
    ],
)
def test_detection_fixture_rejects_unqualified_task_version_and_model(
    controlled, change
):
    controlled.fixture.update(change)
    controlled.fixture_json.write_text(json.dumps(controlled.fixture))
    with pytest.raises(ValueError):
        _read_fixture(controlled.fixture_json)


@pytest.mark.parametrize("base_model", ["yolov8x", "yolo11m", "yolo26l"])
@pytest.mark.asyncio
async def test_detection_uses_real_http_installer_and_exact_per_model_receipt(
    controlled, tmp_path, monkeypatch, base_model
):
    controlled.fixture["base_model"] = base_model
    inspected = []
    monkeypatch.setattr(
        ManagedModelStore,
        "inspect",
        staticmethod(
            lambda path, receipt: inspected.append((path.read_bytes(), receipt))
        ),
    )
    store = ManagedModelStore(tmp_path / "managed")
    app = FastAPI()

    @app.post("/managed-models")
    async def install(body: InstallRequest):
        return await store.install(body)

    @app.get("/managed-models/{version_id}")
    async def receipt(version_id: str):
        return store.receipt(version_id)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://validation"
    ) as client:
        await controlled.install(client)
    assert len(inspected) == 1
    assert inspected[0] == (controlled.weights.read_bytes(), controlled.receipt)
    assert controlled.receipt == store.receipt(controlled.reference["model_version_id"])
    assert controlled.receipt["runtime_qualification"]["ultralytics"] == "8.4.183"
    assert controlled.receipt["task"] == "detection"
    assert "download_url" not in controlled.receipt
    assert controlled.predict_body()["context"] == {
        "task": "detect",
        "model_id": f"trained:{controlled.reference['model_version_id']}",
        "frozen_model_ref": controlled.reference,
        "params": {"conf": 0.01, "iou": 0.7, "max_det": 2},
    }


def _prediction(controlled):
    _installed(controlled)

    class Boxes(SimpleNamespace):
        def __len__(self):
            return len(self.xyxy)

    native = SimpleNamespace(
        boxes=Boxes(
            xyxy=np.array([[1, 2, 10, 6]]),
            conf=np.array([0.8]),
            cls=np.array([0]),
        )
    )
    boxes = _emit_detection(native, {0: "shape"}, 11, 7)
    return {
        "results": [
            {
                "task": "controlled-detect",
                "model_version": f"trained:{controlled.reference['model_version_id']}",
                "cache_hit": False,
                "result": boxes,
                "meta": {"trained_model": controlled.reference},
            }
        ]
    }, {
        "loaded_keys": [{"key": controlled.pool_key}],
        "gpu_resident": True,
        "device": "cuda:0",
        "builders": 0,
        "borrowers": 0,
        "waiters": 0,
    }


def test_detection_requires_two_exact_cold_cycles_and_separate_box_evidence(controlled):
    response, snapshot = _prediction(controlled)
    for cycle in (1, 2):
        controlled.inspect_prediction(response, cycle=cycle, snapshot=snapshot)
        with pytest.raises(AssertionError, match="two complete"):
            controlled.evidence(
                model_approval="models:approved", fixture_approval="fixtures:approved"
            )
        controlled.unloaded(
            cycle=cycle,
            snapshot={"current_size": 0, "loaded_keys": [], "gpu_resident": False},
        )
    artifacts, evidence = controlled.evidence(
        model_approval="models:approved", fixture_approval="fixtures:approved"
    )
    assert len(artifacts) == 3
    assert evidence["task"] == "detect" and evidence["base_model"] == "yolo26n"
    assert evidence["framework_version"] == "8.4.183"
    assert "output_contract" not in evidence
    assert str(controlled.weights.parent) not in json.dumps(evidence)
    assert all(
        item["boxes"] == 1 and "mask_objects" not in item for item in evidence["cycles"]
    )
    assert controlled.checks == (
        "controlled_detect_install",
        "controlled_detect_reference_mismatch_rejected",
        "controlled_detect_receipt_mismatch_rejected",
        "controlled_detect_native_boxes",
        "controlled_detect_cold_reload",
    )


@pytest.mark.parametrize("change", [None, "missing", "version", "extra"])
def test_detection_evidence_retains_only_the_pinned_inference_runtime(
    controlled, change
):
    from training_contract.inference_runtime import inference_runtime

    response, snapshot = _prediction(controlled)
    runtime = inference_runtime(controlled.receipt, "8.4.183")
    controlled.receipt["inference_runtime"] = runtime
    snapshot["loaded_keys"] = [{"key": controlled.pool_key}]
    meta = response["results"][0]["meta"]
    meta["inference_runtime"] = copy.deepcopy(runtime)
    if change == "missing":
        del meta["inference_runtime"]
    elif change == "version":
        meta["inference_runtime"]["version"] = "8.4.184"
    elif change == "extra":
        meta["mask_objects"] = {}
    if change is None:
        controlled.inspect_prediction(response, cycle=1, snapshot=snapshot)
        assert len(controlled.cycles) == 1
    else:
        with pytest.raises(AssertionError):
            controlled.inspect_prediction(response, cycle=1, snapshot=snapshot)
        assert controlled.cycles == []


@pytest.mark.parametrize(
    "change",
    [
        "wrong_model",
        "wrong_ref",
        "mask_meta",
        "empty",
        "too_many",
        "polygon",
        "class_index",
        "class_label",
        "nan",
        "bad_score",
        "negative",
        "zero_area",
        "outside",
        "cache_hit",
        "pool_key",
        "cpu",
        "borrower",
    ],
)
def test_detection_evidence_rejects_geometry_identity_and_pool_drift(
    controlled, change
):
    response, snapshot = _prediction(controlled)
    item = response["results"][0]
    box = item["result"][0]
    if change == "wrong_model":
        item["model_version"] = "yolo26n"
    elif change == "wrong_ref":
        item["meta"]["trained_model"] = {**controlled.reference, "sha256": "0" * 64}
    elif change == "mask_meta":
        item["meta"]["segmentation"] = {}
    elif change == "empty":
        item["result"] = []
    elif change == "too_many":
        item["result"] *= 3
    elif change == "polygon":
        box["type"] = "polygonlabels"
    elif change == "class_index":
        box["native_class_index"] = 1
    elif change == "class_label":
        box["value"]["rectanglelabels"] = ["other"]
    elif change == "nan":
        box["value"]["width"] = float("nan")
    elif change == "bad_score":
        box["score"] = 0
    elif change == "negative":
        box["value"]["x"] = -1
    elif change == "zero_area":
        box["value"]["height"] = 0
    elif change == "outside":
        box["value"]["width"] = 100
    elif change == "cache_hit":
        item["cache_hit"] = True
    elif change == "pool_key":
        snapshot["loaded_keys"] = [
            {"key": controlled.pool_key.replace("/detect", "/segment")}
        ]
    elif change == "cpu":
        snapshot["device"] = "cpu"
    else:
        snapshot["borrowers"] = 1
    with pytest.raises(AssertionError):
        controlled.inspect_prediction(response, cycle=1, snapshot=snapshot)
    assert controlled.cycles == []


def test_new_inputs_do_not_mix_with_legacy_group(controlled, tmp_path, monkeypatch):
    monkeypatch.setenv(INPUT_ENV[0], str(controlled.weights))
    with pytest.raises(ValueError, match="cannot be mixed"):
        load_controlled_fixture(tmp_path)


def test_partial_model_input_group_is_rejected(controlled, tmp_path, monkeypatch):
    monkeypatch.delenv(MODEL_INPUT_ENV[2])
    with pytest.raises(ValueError, match="All three"):
        load_controlled_fixture(tmp_path)


def test_schema_three_also_supports_explicit_segmentation(controlled):
    controlled.fixture.update(task="segment", base_model="yolo26n-seg")
    controlled.fixture_json.write_text(json.dumps(controlled.fixture))
    assert _read_fixture(controlled.fixture_json) == controlled.fixture
    assert controlled.task == "segment"
    assert controlled.check_prefix == "controlled_segment"
    assert (
        _fixture_identity(controlled.fixture)["output_contract"] == "aap-instance-rle-1"
    )


def test_detection_image_budget_does_not_multiply_pixels_by_detection_count(
    controlled, tmp_path, monkeypatch
):
    specification = copy.deepcopy(controlled.fixture)
    specification["max_det"] = 300
    controlled.fixture_json.write_text(json.dumps(specification))
    Image.new("RGB", (1024, 1024)).save(controlled.image, format="PNG")
    for name, value in zip(
        MODEL_INPUT_ENV,
        (controlled.weights, controlled.fixture_json, controlled.image),
        strict=True,
    ):
        monkeypatch.setenv(name, str(value))
    assert load_controlled_fixture(tmp_path).dimensions == [1024, 1024]


@pytest.mark.asyncio
async def test_real_managed_routes_and_fault_matrix_keep_exact_detection_identity(
    controlled, tmp_path, monkeypatch
):
    # Native construction/device allocation alone are stubbed. Admission,
    # immutable installation, request parsing, pool ownership, and fault routes
    # run their production code; this remains a CPU tool contract test.
    import main
    from gpu_lifecycle import YoloGpuLifecycle
    from model_pool import ModelPool
    from predictor import model_key_for_context

    response, _ = _prediction(controlled)
    boxes = response["results"][0]["result"]
    controlled.receipt = None
    executed = []
    native = SimpleNamespace(names={0: "shape"}, device="cuda:0")
    pool = ModelPool(2, lambda *_: native, lambda: None)
    key = Ed25519PrivateKey.generate()
    lifecycle = YoloGpuLifecycle(
        pool, verify_keyring={"test": key.public_key()}, evictable_verified=True
    )

    class Predictor:
        async def predict_one(self, file_path, context):
            executed.append(
                (file_path, context.frozen_model_ref.model_dump(mode="json"))
            )
            async with pool.borrow(*model_key_for_context(context)) as lease:
                return copy.deepcopy(boxes), lease.cache_hit, 0, 1

    monkeypatch.setattr(ManagedModelStore, "inspect", staticmethod(lambda *_: None))
    monkeypatch.setattr(
        main, "_managed_models", ManagedModelStore(tmp_path / "managed")
    )
    monkeypatch.setattr(main, "_model_pool", pool)
    monkeypatch.setattr(main, "_predictor", Predictor())
    monkeypatch.setattr(main, "_gpu_lifecycle", lifecycle)

    def token(scope, *, generation=None, jti=None, operation="validation"):
        control = scope in {
            AdmissionScope.MODE,
            AdmissionScope.DRAIN,
            AdmissionScope.UNLOAD,
        }
        return sign_admission_token(
            AdmissionTokenClaims(
                backend_registry_id="test-yolo",
                gpu_resource_id="test-node/GPU-1",
                boot_id=lifecycle.boot_id,
                generation=generation,
                control_epoch="1",
                scope=scope,
                jti=jti or str(uuid.uuid4()),
                exp=int(time.time()) + 60,
                owner="validation" if control else None,
                operation=operation if control else None,
            ),
            private_key=key,
            kid="test",
        )

    def headers(scope, generation, *, operation="validation"):
        return {
            GPU_GENERATION_HEADER: generation,
            GPU_ADMISSION_TOKEN_HEADER: token(
                scope, generation=generation, operation=operation
            ),
        }

    await lifecycle.set_mode(
        LifecycleModeRequest(gate="enforce", control_epoch="1"),
        token=token(AdmissionScope.MODE),
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(main.app),
            base_url="http://validation",
            headers=backend_auth_headers("yolo"),
        ) as client:
            await controlled.install(client)
            await controlled.reject_mismatched_references(
                client, lambda: headers(AdmissionScope.PREDICT, "1")
            )
            for cycle, generation in ((1, "1"), (2, "3")):
                result = await client.post(
                    "/predict",
                    json=controlled.predict_body(),
                    headers=headers(AdmissionScope.PREDICT, generation),
                )
                assert result.status_code == 200, result.text
                controlled.inspect_prediction(
                    result.json(), cycle=cycle, snapshot=await pool.snapshot()
                )
                if cycle == 1:
                    drained = await client.post(
                        "/drain",
                        json={"generation": "2"},
                        headers=headers(AdmissionScope.DRAIN, "2"),
                    )
                    assert drained.status_code == 200
                    unloaded = await client.post(
                        "/unload",
                        json={"generation": "2"},
                        headers=headers(AdmissionScope.UNLOAD, "2"),
                    )
                    assert unloaded.status_code == 200
                    controlled.unloaded(cycle=1, snapshot=await pool.snapshot())
            checks = await exercise_lifecycle_fault_matrix(
                client=client,
                lifecycle=lifecycle,
                token=token,
                workload_scope=AdmissionScope.PREDICT,
                drain_scope=AdmissionScope.DRAIN,
                unload_scope=AdmissionScope.UNLOAD,
                workload_path="/predict",
                workload_body=controlled.predict_body(),
                current_generation="3",
                stale_generation="1",
                next_generation="4",
                drain_owner="cycle-2",
            )
            assert all(checks.values())
            unloaded = await client.post(
                "/unload",
                json={"generation": "4"},
                headers=headers(AdmissionScope.UNLOAD, "4", operation="cycle-2"),
            )
            assert unloaded.status_code == 200
            controlled.unloaded(cycle=2, snapshot=await pool.snapshot())
        _, evidence = controlled.evidence(
            model_approval="models:approved", fixture_approval="fixtures:approved"
        )
        assert evidence["task"] == "detect"
        assert len(executed) == 3  # two cold cycles plus the accepted replay probe
        assert all(
            path == str(controlled.image) and ref == controlled.reference
            for path, ref in executed
        )
        assert (await pool.snapshot())["current_size"] == 0
    finally:
        await lifecycle.shutdown()
