"""Training option freezing and initialization conflicts without a database."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
from fastapi import HTTPException
from training_contract.dataset import DETECTION_CONTRACT

from app.schemas.training_run import TrainingRunCreate
from app.services import training_adapters, training_recipes, training_runs
from tests.trainer_plugin_fixtures import ULTRALYTICS_VERSION, ultralytics_schema


def configuration(**values):
    schema = ultralytics_schema("yolo11n")
    return dict(
        schema_id=schema["schema_id"],
        schema_digest=schema["schema_digest"],
        values=values,
    )


def _patch_plugin_adapters(monkeypatch, base):
    from datetime import datetime, timezone
    from app.config import settings
    from app.schemas.trainer_plugin import (
        RegisterTrainerPluginRequest,
        TrainerPluginRecord,
    )
    from app.services.trainer_plugins import validate_registration_payload
    from tests.trainer_plugin_fixtures import (
        ultralytics_plugin_capabilities,
        ultralytics_plugin_manifest,
    )

    monkeypatch.setattr(settings, "training_enabled", True)
    monkeypatch.setattr(settings, "training_executor_token", "t" * 40)
    monkeypatch.setattr(settings, "training_gpu_uuid", "GPU-options-test")
    monkeypatch.setattr(settings, "training_node_id", "test")
    manifest = ultralytics_plugin_manifest(framework_version=ULTRALYTICS_VERSION)
    caps = ultralytics_plugin_capabilities(
        framework_version=ULTRALYTICS_VERSION,
        base_models={
            base["id"]: {"sha256": base["sha256"], "filename": base["filename"]}
        },
    )
    reg = validate_registration_payload(
        RegisterTrainerPluginRequest(
            execution_mode="container",
            source_tier="official",
            artifact="ghcr.io/yoao-labs/trainer-ultralytics@sha256:" + "b" * 64,
            manifest=manifest,
            capabilities_snapshot=caps,
        )
    )
    now = datetime.now(timezone.utc)
    record = TrainerPluginRecord(
        id=uuid.uuid4(),
        extension_point="trainer",
        status="enabled",
        revision=3,
        created_at=now,
        updated_at=now,
        **reg,
    )
    fake_store = SimpleNamespace(
        get_enabled_by_framework=AsyncMock(return_value=record),
        list_plugins=AsyncMock(return_value=[record]),
    )
    monkeypatch.setattr(
        training_runs, "TrainerPluginsPostgresData", lambda _db: fake_store
    )
    monkeypatch.setattr(
        training_adapters, "TrainerPluginsPostgresData", lambda _db: fake_store
    )
    monkeypatch.setattr(
        training_runs.data,
        "list_leases",
        AsyncMock(
            return_value=[
                SimpleNamespace(
                    resource_id="test/GPU-options-test",
                    last_seen_at=now,
                    runtime={"base_asset_sha256": [base["sha256"]]},
                )
            ]
        ),
    )
    return record


@pytest.mark.parametrize("pretrained,evaluate_train", [(True, False), (False, True)])
async def test_run_and_recipe_freeze_optional_training_choices(
    monkeypatch, pretrained, evaluate_train
):
    base = dict(id="yolo11n", filename="yolo11n.pt", sha256="a" * 64)
    _patch_plugin_adapters(monkeypatch, base)
    monkeypatch.setattr(
        training_runs.initialization, "resolve", AsyncMock(return_value=None)
    )
    dataset = SimpleNamespace(
        id=uuid.uuid4(),
        status="ready",
        package_sha256="c" * 64,
        manifest={
            **DETECTION_CONTRACT,
            "classes": ["car"],
            "content_digest": "d" * 64,
            "validation_digest": "e" * 64,
        },
    )
    request = TrainingRunCreate(
        name="options",
        dataset_id=dataset.id,
        request_id=uuid.uuid4(),
        configuration=configuration(
            pretrained=pretrained, evaluate_train=evaluate_train
        ),
    )
    fake_db = SimpleNamespace(
        scalar=AsyncMock(return_value=dataset),
        execute=AsyncMock(),
    )
    monkeypatch.setattr(
        training_runs.datasets, "hold_version", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        training_runs.datasets, "read_version", AsyncMock(return_value=dataset)
    )
    frozen = await training_runs.frozen_config(fake_db, uuid.uuid4(), request)
    assert frozen["configuration"]["effective_values"]["pretrained"] is pretrained
    assert (
        frozen["configuration"]["effective_values"]["evaluate_train"] is evaluate_train
    )
    recipe = await training_recipes.snapshot(
        fake_db, "ultralytics", "yolo11n", request.configuration.model_dump()
    )
    assert recipe["configuration"] == frozen["configuration"]
    with pytest.raises(HTTPException) as error:
        await training_recipes.snapshot(
            fake_db,
            "ultralytics",
            "yolo11n",
            configuration(pretrained=False),
            {"kind": "trained_model"},
        )
    assert error.value.status_code == 422


async def test_run_rejects_random_initialization_with_project_weights(monkeypatch):
    base = dict(id="yolo11n", filename="yolo11n.pt", sha256="a" * 64)
    _patch_plugin_adapters(monkeypatch, base)
    monkeypatch.setattr(
        training_runs.initialization,
        "resolve",
        AsyncMock(return_value={"kind": "trained_model"}),
    )
    dataset = SimpleNamespace(
        id=uuid.uuid4(),
        status="ready",
        package_sha256="c" * 64,
        manifest={
            **DETECTION_CONTRACT,
            "classes": ["car"],
            "content_digest": "d" * 64,
            "validation_digest": "e" * 64,
        },
    )
    request = TrainingRunCreate(
        name="conflict",
        dataset_id=dataset.id,
        request_id=uuid.uuid4(),
        configuration=configuration(pretrained=False),
        initialization={
            "kind": "trained_model",
            "model_version_id": str(uuid.uuid4()),
            "source_digest": "f" * 64,
        },
    )
    monkeypatch.setattr(
        training_runs.datasets, "hold_version", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        training_runs.datasets, "read_version", AsyncMock(return_value=dataset)
    )
    with pytest.raises(HTTPException) as error:
        await training_runs.frozen_config(
            SimpleNamespace(
                scalar=AsyncMock(return_value=dataset), execute=AsyncMock()
            ),
            uuid.uuid4(),
            request,
        )
    assert error.value.status_code == 422
