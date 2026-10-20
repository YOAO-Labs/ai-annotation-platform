"""Training option freezing and initialization conflicts without a database."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
from fastapi import HTTPException
from training_contract import definition
from training_contract.dataset import DETECTION_CONTRACT

from app.schemas.training_run import TrainingRunCreate
from app.services import training_recipes, training_runs


def configuration(**values):
    schema = definition("ultralytics", version="8.4.177")
    return dict(
        schema_id=schema["schema_id"],
        schema_digest=schema["schema_digest"],
        values=values,
    )


@pytest.mark.parametrize("pretrained,evaluate_train", [(True, False), (False, True)])
async def test_run_and_recipe_freeze_optional_training_choices(
    monkeypatch, pretrained, evaluate_train
):
    base = dict(id="yolo11n", filename="yolo11n.pt", sha256="a" * 64)
    template = dict(
        framework="ultralytics",
        version="8.4.177",
        image_digest="sha256:" + "b" * 64,
        base_models=[base],
    )
    monkeypatch.setattr(
        training_runs, "catalog", lambda: dict(enabled=True, items=[template])
    )
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
    frozen = await training_runs.frozen_config(
        SimpleNamespace(scalar=AsyncMock(return_value=dataset)), uuid.uuid4(), request
    )
    assert frozen["configuration"]["effective_values"]["pretrained"] is pretrained
    assert (
        frozen["configuration"]["effective_values"]["evaluate_train"] is evaluate_train
    )
    monkeypatch.setattr(
        training_runs,
        "controlled_template",
        lambda *_: {
            "framework": "ultralytics",
            "framework_version": template["version"],
            "image_digest": template["image_digest"],
            "base_model": base,
        },
    )
    recipe = training_recipes.snapshot(
        "ultralytics", "yolo11n", request.configuration.model_dump()
    )
    assert recipe["configuration"] == frozen["configuration"]
    with pytest.raises(HTTPException) as error:
        training_recipes.snapshot(
            "ultralytics",
            "yolo11n",
            configuration(pretrained=False),
            {"kind": "trained_model"},
        )
    assert error.value.status_code == 422


async def test_run_rejects_random_initialization_with_project_weights(monkeypatch):
    base = dict(id="yolo11n", filename="yolo11n.pt", sha256="a" * 64)
    monkeypatch.setattr(
        training_runs,
        "catalog",
        lambda: dict(
            enabled=True,
            items=[
                dict(
                    framework="ultralytics",
                    version="8.4.177",
                    image_digest="sha256:" + "b" * 64,
                    base_models=[base],
                )
            ],
        ),
    )
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
    with pytest.raises(HTTPException) as error:
        await training_runs.frozen_config(
            SimpleNamespace(scalar=AsyncMock(return_value=dataset)),
            uuid.uuid4(),
            request,
        )
    assert error.value.status_code == 422
