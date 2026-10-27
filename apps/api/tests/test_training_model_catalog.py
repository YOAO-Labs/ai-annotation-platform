"""Catalog family projection and local assets are independent of other nodes."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
import hashlib
import uuid

import pytest
from fastapi import HTTPException
from training_contract import definition
from training_contract.catalog import supported_identities, instance_rle_capability
from training_contract.models import MODEL_SPECS

from app.config import settings
from app.schemas.training_hub import TrainingHubCatalog
from app.schemas.training_run import TrainingRunCreate
from app.services import training_runs as service
from app.services import training_adapters as adapters
from app.services import training_recipes


def configured(monkeypatch):
    monkeypatch.setattr(settings, "training_enabled", True)
    monkeypatch.setattr(settings, "training_executor_token", "a" * 32)
    monkeypatch.setattr(settings, "training_gpu_uuid", "GPU-catalog-test")
    entries = {}
    for spec in MODEL_SPECS:
        entry = entries.setdefault(
            spec.framework,
            {
                "version": "8.4.183" if spec.framework == "ultralytics" else "1.6.0",
                "image_digest": "sha256:" + "b" * 64,
                "base_models": {},
            },
        )
        checksum = hashlib.sha256(spec.id.encode()).hexdigest()
        entry["base_models"][spec.id] = {
            "filename": spec.id + ".pt",
            "sha256": checksum,
            "size_bytes": 100,
            "object_key": f"model-assets/sha256/{checksum}/weights.pt",
        }
    monkeypatch.setattr(settings, "training_catalog_json", entries)
    return entries


def test_exact_matrix_survives_global_catalog_response_serialization(monkeypatch):
    entries = configured(monkeypatch)
    entries["libreyolo"]["base_models"]["libreyolo9e"] = entries["libreyolo"][
        "base_models"
    ]["libreyolo9c"]
    result = adapters.catalog()
    checked = TrainingHubCatalog.model_validate(
        {
            **result,
            "online": False,
            "busy": False,
            "executor_id": None,
            "last_seen_at": None,
        }
    ).model_dump()
    models = [base for item in checked["items"] for base in item["base_models"]]
    assert len(models) == 34
    assert all(
        base["model_family"]
        and base["model_size"]
        and base["object_key"]
        and base["size_bytes"] == 100
        for base in models
    )
    assert all(base["capabilities"]["model_registration"] for base in models)
    assert {
        schema["model_family"]
        for item in checked["items"]
        for schema in item["parameter_schemas"]
    } == {"yolov8", "yolo11", "yolo26", "yolo9"}


def test_schema_readiness_matches_family_and_each_local_blob(monkeypatch):
    configured(monkeypatch)
    result = adapters.catalog()
    ultra = next(item for item in result["items"] if item["framework"] == "ultralytics")
    ready = next(base for base in ultra["base_models"] if base["id"] == "yolo26x-seg")
    runtime = {
        "frameworks": {"ultralytics": "8.4.183"},
        "gpu_uuid": settings.training_gpu_uuid,
        "image_digest": ultra["image_digest"],
        "executor_protocol": 2,
        "supported_configs": supported_identities({"ultralytics": "8.4.183"}),
        "instance_rle_consumer": instance_rle_capability("8.4.183"),
        "base_asset_sha256": [ready["sha256"]],
    }
    service.annotate_readiness(result, runtime, online=True)
    assert ready["compatible"] is True
    assert sum(bool(base["compatible"]) for base in ultra["base_models"]) == 1
    assert {
        schema["model_family"]
        for schema in ultra["parameter_schemas"]
        if schema["compatible"]
    } == {"yolo26"}
    assert all(
        schema["task"] == "segment"
        for schema in ultra["parameter_schemas"]
        if schema["compatible"]
    )
    other_node = deepcopy(runtime)
    other_node["base_asset_sha256"] = []
    service.annotate_readiness(result, other_node, online=True)
    assert ready["compatible"] is False
    assert "权重" in ready["unavailable_reason"]


async def test_cold_node_cannot_preflight_a_bucket_only_asset(monkeypatch):
    configured(monkeypatch)
    schema = definition("ultralytics", "yolo26n-seg")
    body = TrainingRunCreate(
        name="segment-cold",
        task="segment",
        base_model="yolo26n-seg",
        dataset_id=uuid.uuid4(),
        request_id=uuid.uuid4(),
        configuration={
            "schema_id": schema["schema_id"],
            "schema_digest": schema["schema_digest"],
            "values": {},
        },
    )
    db = SimpleNamespace(
        get=AsyncMock(
            return_value=SimpleNamespace(
                last_seen_at=service.now(), runtime={"base_asset_sha256": []}
            )
        )
    )
    with pytest.raises(HTTPException) as exc:
        await service.frozen_config(db, uuid.uuid4(), body)
    assert exc.value.status_code == 409 and "权重" in exc.value.detail


async def test_current_runtime_requires_preparation_even_with_legacy_asset_shape(
    monkeypatch,
):
    entries = configured(monkeypatch)
    base = entries["ultralytics"]["base_models"]["yolo26n"]
    base.pop("object_key")
    base.pop("size_bytes")
    result = adapters.catalog()
    item = next(item for item in result["items"] if item["framework"] == "ultralytics")
    runtime = {
        "frameworks": {"ultralytics": "8.4.183"},
        "image_digest": item["image_digest"],
        "gpu_uuid": settings.training_gpu_uuid,
        "executor_protocol": 2,
        "supported_configs": supported_identities({"ultralytics": "8.4.183"}),
        "base_asset_sha256": [],
    }
    service.annotate_readiness(result, runtime, online=True)
    model = next(model for model in item["base_models"] if model["id"] == "yolo26n")
    assert model["compatible"] is False and "权重" in model["unavailable_reason"]
    schema = definition("ultralytics", "yolo26n")
    db = SimpleNamespace(
        get=AsyncMock(
            return_value=SimpleNamespace(last_seen_at=service.now(), runtime=runtime)
        )
    )
    body = TrainingRunCreate(
        name="detect-cold",
        dataset_id=uuid.uuid4(),
        request_id=uuid.uuid4(),
        base_model="yolo26n",
        configuration={
            "schema_id": schema["schema_id"],
            "schema_digest": schema["schema_digest"],
            "values": {},
        },
    )
    with pytest.raises(HTTPException) as exc:
        await service.frozen_config(db, uuid.uuid4(), body)
    assert exc.value.status_code == 409 and "权重" in exc.value.detail


def test_old_runtime_cannot_advertise_new_models_and_bad_assets(monkeypatch):
    entries = configured(monkeypatch)
    entries["ultralytics"]["version"] = "8.4.177"
    entries["ultralytics"]["base_models"]["yolo11n"]["filename"] = "unsafe.yaml"
    entries["ultralytics"]["base_models"]["yolo11s"]["object_key"] = (
        "different/asset.pt"
    )
    result = adapters.catalog()
    ids = {
        base["id"]
        for item in result["items"]
        if item["framework"] == "ultralytics"
        for base in item["base_models"]
    }
    assert ids == {"yolo11m", "yolo11l", "yolo11x", "yolo11n-seg"}


def test_recipe_cannot_combine_historical_schema_with_current_runtime(monkeypatch):
    entries = configured(monkeypatch)
    old = definition("ultralytics", "yolo11n", version="8.4.177")
    configuration = {
        "schema_id": old["schema_id"],
        "schema_digest": old["schema_digest"],
        "values": {},
    }
    entries["ultralytics"]["version"] = "8.4.177"
    historical = training_recipes.snapshot("ultralytics", "yolo11n", configuration)
    assert training_recipes.compatibility(historical) == (True, None)
    entries["ultralytics"]["version"] = "8.4.183"
    with pytest.raises(HTTPException) as exc:
        training_recipes.snapshot("ultralytics", "yolo11n", configuration)
    assert exc.value.status_code == 409
    compatible, reason = training_recipes.compatibility(historical)
    assert compatible is False and "版本不一致" in reason
