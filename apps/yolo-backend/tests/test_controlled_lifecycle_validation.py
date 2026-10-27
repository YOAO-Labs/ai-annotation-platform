"""Fail-closed offline inputs, actual HTTP installation and native mask evidence."""

import copy
import hashlib
import json
from types import SimpleNamespace

import httpx
import numpy as np
import pytest
from fastapi import FastAPI
from PIL import Image

from managed_models import InstallRequest, ManagedModelStore, digest
from managed_segmentation import emit_instances
from scripts.controlled_segmentation_validation import (
    INPUT_ENV,
    load_controlled_fixture,
)


@pytest.fixture()
def controlled(tmp_path, monkeypatch):
    weights = tmp_path / "best.pt"
    weights.write_bytes(b"native checkpoint bytes; inspect is isolated in unit tests")
    image = tmp_path / "representative.png"
    Image.new("RGB", (11, 7), "white").save(image)
    fixture = {
        "schema": 1,
        "frozen_model_ref": {
            "model_version_id": "77123456-1234-4234-8234-123456789abc",
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
    for name, value in zip(INPUT_ENV, (weights, specification, image), strict=True):
        monkeypatch.setenv(name, str(value))
    root = tmp_path / "private"
    root.mkdir()
    return load_controlled_fixture(root)


@pytest.mark.asyncio
async def test_actual_http_install_and_immutable_receipt_guard(
    controlled, tmp_path, monkeypatch
):
    inspected = []
    monkeypatch.setattr(
        ManagedModelStore,
        "inspect",
        staticmethod(
            lambda path, expected: inspected.append((path.read_bytes(), expected))
        ),
    )
    store = ManagedModelStore(tmp_path / "managed")
    app = FastAPI()

    @app.post("/managed-models")
    async def install(body: InstallRequest):
        from managed_models import runtime_receipt

        return runtime_receipt(await store.install(body))

    @app.get("/managed-models/{version_id}")
    async def receipt(version_id: str):
        from managed_models import runtime_receipt

        return runtime_receipt(store.receipt(version_id))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://validation"
    ) as client:
        await controlled.install(client)
    assert len(inspected) == 1
    assert inspected[0][0] == controlled.weights.read_bytes()
    assert {
        k: v for k, v in controlled.receipt.items() if k != "inference_runtime"
    } == store.receipt(controlled.reference["model_version_id"])
    assert controlled.receipt["framework_version"] == "8.4.177"
    assert controlled.receipt["inference_runtime"]["version"] == "8.4.183"
    assert (
        controlled.receipt["classes_digest"] == controlled.reference["classes_digest"]
    )
    assert "download_url" not in controlled.receipt


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["install", "conflict", "receipt"])
async def test_installation_cannot_pass_on_receipt_drift_or_missing_conflict_guard(
    controlled, stage
):
    expected = _installed(controlled).receipt
    controlled.receipt = None
    calls = []

    class Client:
        async def post(self, _path, *, json):
            calls.append(json)
            if len(calls) == 1:
                value = (
                    {**expected, "classes_digest": "0" * 64}
                    if stage == "install"
                    else expected
                )
                return SimpleNamespace(status_code=200, json=lambda: value)
            return SimpleNamespace(status_code=200 if stage == "conflict" else 409)

        async def get(self, _path):
            value = {**expected, "classes_digest": "0" * 64}
            return SimpleNamespace(status_code=200, json=lambda: value)

    with pytest.raises(AssertionError):
        await controlled.install(Client())
    assert controlled.receipt is None


def _installed(controlled):
    from training_contract.model_return import segmentation_identity

    controlled.receipt = {
        **controlled.reference,
        "classes": controlled.fixture["classes"],
        "framework": "ultralytics",
        "framework_version": "8.4.177",
        "task": "segmentation",
        **segmentation_identity("yolo11n-seg"),
    }
    return controlled


@pytest.mark.parametrize("base_model", ["yolov8x-seg", "yolo11m-seg", "yolo26l-seg"])
def test_new_fixture_freezes_each_model_and_runtime_without_changing_legacy(
    controlled, base_model
):
    from scripts.controlled_segmentation_validation import _read_fixture
    from training_contract.model_return import segmentation_identity

    controlled.fixture.update(
        schema=2, base_model=base_model, framework_version="8.4.183"
    )
    controlled.fixture_json.write_text(json.dumps(controlled.fixture))
    assert _read_fixture(controlled.fixture_json) == controlled.fixture
    controlled.receipt = {
        **controlled.reference,
        "classes": controlled.fixture["classes"],
        "framework": "ultralytics",
        "framework_version": "8.4.183",
        "task": "segmentation",
        **segmentation_identity(base_model, framework_version="8.4.183"),
    }
    assert controlled.receipt["runtime_qualification"]["nms"] is (
        False if base_model.startswith("yolo26") else None
    )
    controlled.fixture["framework_version"] = "8.4.177"
    controlled.fixture_json.write_text(json.dumps(controlled.fixture))
    with pytest.raises(ValueError):
        _read_fixture(controlled.fixture_json)


def _prediction(controlled):
    ref = _installed(controlled).receipt
    mask = np.zeros((1, 7, 11), dtype=np.uint8)
    mask[0, 1:6, 1:7] = 1
    mask[0, 2:5, 2:6] = 0  # same instance retains both a hole and island
    mask[0, 0, 10] = 1
    native = SimpleNamespace(
        names={0: "shape"},
        orig_shape=(7, 11),
        masks=SimpleNamespace(data=mask),
        boxes=SimpleNamespace(
            xyxy=np.array([[0, 0, 11, 7]]), conf=np.array([0.8]), cls=np.array([0])
        ),
    )
    instances, meta = emit_instances(native, ref, 11, 7, 2)
    return {
        "results": [
            {
                "task": "controlled-segment",
                "model_version": f"trained:{ref['model_version_id']}",
                "cache_hit": False,
                "result": instances,
                "meta": {**meta, "trained_model": controlled.reference},
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


def test_native_original_rle_two_cold_cycles_required_for_evidence(controlled):
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
    assert len(artifacts) == 3 and all(item["approval_ref"] for item in artifacts)
    assert evidence["dimensions"] == [7, 11]
    assert all(
        item["cold_load"] and item["unloaded"] and item["instances"] == 1
        for item in evidence["cycles"]
    )
    assert str(controlled.weights.parent) not in json.dumps(evidence)


@pytest.mark.parametrize(
    "change",
    [
        "wrong_reference",
        "wrong_dimensions",
        "missing_mask",
        "wrong_mask_hash",
        "wrong_native_class",
        "wrong_confidence",
        "polygon",
        "no_instances",
        "cache_hit",
        "wrong_pool_receipt",
        "cpu_fallback",
        "borrower",
        "orphan_mask",
    ],
)
def test_native_evidence_rejects_identity_geometry_and_pool_drift(controlled, change):
    response, snapshot = _prediction(controlled)
    item = response["results"][0]
    instance = item["result"][0]
    if change == "wrong_reference":
        item["meta"]["trained_model"] = {**controlled.reference, "sha256": "0" * 64}
    elif change == "wrong_dimensions":
        item["meta"]["segmentation"]["dimensions"] = [11, 7]
    elif change == "missing_mask":
        item["meta"]["mask_objects"] = {}
    elif change == "wrong_mask_hash":
        rle = next(iter(item["meta"]["mask_objects"].values()))
        rle["counts"][0] += 1
        rle["counts"][-1] -= 1
    elif change == "wrong_native_class":
        instance["native_class_index"] = 1
    elif change == "wrong_confidence":
        instance["confidence"] = 0.9
    elif change == "polygon":
        instance["geometry"]["type"] = "polygon"
    elif change == "no_instances":
        item["result"] = []
    elif change == "cache_hit":
        item["cache_hit"] = True
    elif change == "wrong_pool_receipt":
        snapshot["loaded_keys"] = [{"key": "trained:wrong-receipt/segment"}]
    elif change == "cpu_fallback":
        snapshot["device"] = "cpu"
    elif change == "borrower":
        snapshot["borrowers"] = 1
    elif change == "orphan_mask":
        item["meta"]["mask_objects"]["0" * 64] = next(
            iter(item["meta"]["mask_objects"].values())
        )
    with pytest.raises((ValueError, AssertionError)):
        controlled.inspect_prediction(response, cycle=1, snapshot=snapshot)
    assert controlled.cycles == []


@pytest.mark.asyncio
async def test_both_frozen_reference_digest_mismatches_must_be_rejected(controlled):
    seen = []

    class Client:
        async def post(self, path, *, json, headers):
            seen.append((path, json, headers))
            return SimpleNamespace(status_code=409)

    await controlled.reject_mismatched_references(
        Client(), lambda: {"fresh": str(len(seen))}
    )
    assert len(seen) == 2 and seen[0][2] != seen[1][2]
    first = seen[0][1]["context"]["frozen_model_ref"]
    second = seen[1][1]["context"]["frozen_model_ref"]
    assert first["sha256"] != controlled.reference["sha256"]
    assert first["classes_digest"] == controlled.reference["classes_digest"]
    assert second["sha256"] == controlled.reference["sha256"]
    assert second["classes_digest"] != controlled.reference["classes_digest"]
    assert all(item[1]["context"]["task"] == "segment" for item in seen)


@pytest.mark.asyncio
async def test_negative_probe_cannot_pass_on_valid_response(controlled):
    class Client:
        async def post(self, *_args, **_kwargs):
            return SimpleNamespace(status_code=200)

    with pytest.raises(AssertionError, match="not rejected"):
        await controlled.reject_mismatched_references(Client(), lambda: {})


def test_absent_and_partial_controlled_inputs(monkeypatch, tmp_path):
    for name in INPUT_ENV:
        monkeypatch.delenv(name, raising=False)
    assert load_controlled_fixture(tmp_path) is None
    monkeypatch.setenv(INPUT_ENV[0], "unused")
    with pytest.raises(ValueError, match="All three"):
        load_controlled_fixture(tmp_path)


@pytest.mark.parametrize(
    "change", ["sha", "classes", "extra", "zero_instances", "nan", "oversized_image"]
)
def test_frozen_inputs_reject_drift_and_vacuous_or_unbounded_fixtures(
    controlled, tmp_path, monkeypatch, change
):
    fixture = copy.deepcopy(controlled.fixture)
    image = controlled.image
    if change == "sha":
        fixture["frozen_model_ref"]["sha256"] = "0" * 64
    elif change == "classes":
        fixture["classes"] = ["wrong-class"]
    elif change == "extra":
        fixture["unrecognized"] = True
    elif change == "zero_instances":
        fixture["min_instances"] = 0
    elif change == "nan":
        fixture["confidence"] = float("nan")
    elif change == "oversized_image":
        image = tmp_path / "oversized.png"
        Image.new("RGB", (4097, 1)).save(image)
    specification = tmp_path / "drift.json"
    specification.write_text(json.dumps(fixture))
    monkeypatch.setenv(INPUT_ENV[0], str(controlled.weights))
    monkeypatch.setenv(INPUT_ENV[1], str(specification))
    monkeypatch.setenv(INPUT_ENV[2], str(image))
    root = tmp_path / "retry"
    root.mkdir()
    with pytest.raises(ValueError):
        load_controlled_fixture(root)
