"""Plugin catalog family projection and local assets are independent of other nodes."""

from copy import deepcopy
import hashlib

import pytest
from fastapi import HTTPException
from training_contract.trainer_plugin import TRAINER_PROTOCOL_VERSION

from app.config import settings
from app.schemas.training_hub import TrainingHubCatalog
from app.services import training_adapters as adapters
from app.services import training_recipes
from app.services import training_runs as service
from tests.trainer_plugin_fixtures import (
    ALL_LIBREYOLO_MODEL_IDS,
    ALL_ULTRALYTICS_MODEL_IDS,
    ULTRALYTICS_VERSION,
    retire_trainer_plugin,
    seed_enabled_trainer_plugin,
    ultralytics_schema,
)


async def seed_full_matrix(db_session, monkeypatch):
    monkeypatch.setattr(settings, "training_enabled", True)
    monkeypatch.setattr(settings, "training_executor_token", "a" * 32)
    monkeypatch.setattr(settings, "training_gpu_uuid", "GPU-catalog-test")
    ultra_models = {
        model_id: {
            "filename": model_id + ".pt",
            "sha256": hashlib.sha256(model_id.encode()).hexdigest(),
        }
        for model_id in ALL_ULTRALYTICS_MODEL_IDS
    }
    libre_models = {
        model_id: {
            "filename": model_id + ".pt",
            "sha256": hashlib.sha256(model_id.encode()).hexdigest(),
        }
        for model_id in ALL_LIBREYOLO_MODEL_IDS
    }
    await seed_enabled_trainer_plugin(
        db_session,
        framework_id="ultralytics",
        plugin_id="yoao.trainer.ultralytics.matrix",
        framework_version=ULTRALYTICS_VERSION,
        image_digest="sha256:" + "b" * 64,
        base_models=ultra_models,
    )
    await seed_enabled_trainer_plugin(
        db_session,
        framework_id="libreyolo",
        plugin_id="yoao.trainer.libreyolo.matrix",
        image_digest="sha256:" + "c" * 64,
        base_models=libre_models,
    )
    return ultra_models, libre_models


async def test_exact_matrix_survives_global_catalog_response_serialization(
    db_session, monkeypatch
):
    await seed_full_matrix(db_session, monkeypatch)
    result = await adapters.resolve_catalog(db_session)
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
    assert all(base["model_family"] and base["sha256"] for base in models)
    assert all(base["capabilities"]["model_registration"] for base in models)
    assert {
        schema["model_family"]
        for item in checked["items"]
        for schema in item["parameter_schemas"]
    } == {"yolov8", "yolo11", "yolo26", "libreyolo9"}


async def test_schema_readiness_matches_family_and_each_local_blob(
    db_session, monkeypatch
):
    await seed_full_matrix(db_session, monkeypatch)
    result = await adapters.resolve_catalog(db_session)
    ultra = next(item for item in result["items"] if item["framework"] == "ultralytics")
    ready = next(base for base in ultra["base_models"] if base["id"] == "yolo26x-seg")
    runtime = {
        "frameworks": {"ultralytics": ULTRALYTICS_VERSION},
        "gpu_uuid": settings.training_gpu_uuid,
        "image_digest": ultra["image_digest"],
        "executor_protocol": 3,
        "trainer_protocols": [TRAINER_PROTOCOL_VERSION],
        "execution_modes": ["container"],
        "base_asset_sha256": [ready["sha256"]],
    }
    service.annotate_readiness(result, runtime, online=True)
    assert ready["compatible"] is True
    assert sum(bool(base["compatible"]) for base in ultra["base_models"]) == 1
    assert {
        (schema["model_family"], schema["task"])
        for schema in ultra["parameter_schemas"]
        if schema["compatible"]
    } == {("yolo26", "segment")}
    other_node = deepcopy(runtime)
    other_node["base_asset_sha256"] = []
    service.annotate_readiness(result, other_node, online=True)
    assert ready["compatible"] is False
    assert "权重" in (ready["unavailable_reason"] or "")
    assert not any(schema["compatible"] for schema in ultra["parameter_schemas"])


async def test_cold_node_runtime_compatibility_requires_local_base_asset(
    db_session, monkeypatch
):
    ultra_models, _ = await seed_full_matrix(db_session, monkeypatch)
    template = await adapters.plugin_template(db_session, "ultralytics", "yolo26n-seg")
    config = {
        "adapter_revision": 3,
        "trainer_protocol": TRAINER_PROTOCOL_VERSION,
        "framework": "ultralytics",
        "framework_version": ULTRALYTICS_VERSION,
        "base_model": template["base_model"],
        "trainer_plugin": {
            "plugin_id": "yoao.trainer.ultralytics.matrix",
            "execution_mode": "container",
            "requires_gpu": True,
        },
    }
    cold_runtime = {
        "executor_protocol": 3,
        "trainer_protocols": [TRAINER_PROTOCOL_VERSION],
        "execution_modes": ["container"],
        "gpu_uuid": "GPU-catalog-test",
        "base_asset_sha256": [],
    }
    assert not service.runtime_compatible(config, cold_runtime)
    warm_runtime = {
        **cold_runtime,
        "base_asset_sha256": [ultra_models["yolo26n-seg"]["sha256"]],
    }
    assert service.runtime_compatible(config, warm_runtime)


async def test_recipe_cannot_combine_historical_schema_with_upgraded_plugin_version(
    db_session, monkeypatch
):
    monkeypatch.setattr(settings, "training_enabled", True)
    monkeypatch.setattr(settings, "training_executor_token", "a" * 32)
    monkeypatch.setattr(settings, "training_gpu_uuid", "GPU-catalog-test")
    await seed_enabled_trainer_plugin(
        db_session,
        framework_id="ultralytics",
        plugin_id="yoao.trainer.ultralytics.v177",
        framework_version="8.4.177",
        image_digest="sha256:" + "1" * 64,
    )
    old = ultralytics_schema("yolo11n", framework_version="8.4.177")
    configuration = {
        "schema_id": old["schema_id"],
        "schema_digest": old["schema_digest"],
        "values": {},
    }
    historical = await training_recipes.snapshot(
        db_session, "ultralytics", "yolo11n", configuration
    )
    assert await training_recipes.compatibility(db_session, historical) == (True, None)
    await retire_trainer_plugin(db_session, "yoao.trainer.ultralytics.v177")
    await seed_enabled_trainer_plugin(
        db_session,
        framework_id="ultralytics",
        plugin_id="yoao.trainer.ultralytics.v183",
        framework_version=ULTRALYTICS_VERSION,
        image_digest="sha256:" + "2" * 64,
    )
    with pytest.raises(HTTPException) as exc:
        await training_recipes.snapshot(
            db_session, "ultralytics", "yolo11n", configuration
        )
    assert exc.value.status_code == 422
    assert "不匹配" in str(exc.value.detail)
    compatible, reason = await training_recipes.compatibility(db_session, historical)
    assert compatible is False and bool(reason)
