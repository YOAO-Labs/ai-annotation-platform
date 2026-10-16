from uuid import uuid4

import pytest
from pydantic import ValidationError as PydanticValidationError

from ai_annotation.training import TrainingRuns
from ai_annotation.training_models import (
    TrainingConfiguration,
    TrainingDatasetCreate,
    TrainingDatasetSelection,
    TrainingInitialization,
    TrainingOrigin,
    TrainingParameters,
    TrainingRunCreate,
    TrainingPreviewCreate,
)


class _Response:
    def json(self):
        return {"config": {}, "config_digest": "a" * 64, "origin": None}


def test_training_request_models_keep_frozen_ids_and_strict_bounds():
    version_id = uuid4()
    body = TrainingRunCreate(
        name="  baseline  ", dataset_id=version_id, request_id=uuid4()
    )
    assert body.name == "baseline"
    assert body.dataset_id == version_id
    assert body.model_fields_set == {"name", "dataset_id", "request_id"}
    assert TrainingParameters(imgsz=672).imgsz == 672

    with pytest.raises(PydanticValidationError):
        TrainingParameters(imgsz=640, epochs=1001)
    with pytest.raises(PydanticValidationError):
        TrainingConfiguration(schema_id="s", schema_digest="a" * 64, extra=True)
    with pytest.raises(PydanticValidationError):
        TrainingConfiguration(
            schema_id="s", schema_digest="a" * 64, values={"lr0": float("nan")}
        )
    with pytest.raises(PydanticValidationError):
        TrainingDatasetCreate(
            name="v1", request_id=uuid4(), preflight_digest="not-a-digest"
        )


def test_dataset_selection_and_initialization_cross_field_guards():
    task_id = uuid4()
    with pytest.raises(PydanticValidationError, match="manager_confirmed"):
        TrainingDatasetSelection(mode="manager_confirmed")
    with pytest.raises(PydanticValidationError, match="duplicates"):
        TrainingDatasetSelection(task_ids=[task_id, task_id])
    with pytest.raises(PydanticValidationError, match="confirmed negatives"):
        TrainingDatasetSelection(confirmed_negative_ids=[task_id])
    assert (
        TrainingDatasetSelection(
            task_ids=[task_id],
            mode="manager_confirmed",
            confirmed_negative_ids=[task_id],
        ).task
        == "detect"
    )

    assert TrainingInitialization(kind="catalog").model_dump(exclude_unset=True) == {
        "kind": "catalog"
    }
    with pytest.raises(PydanticValidationError, match="trained_model"):
        TrainingInitialization(kind="trained_model")
    assert (
        TrainingInitialization(
            kind="trained_model", model_version_id=uuid4(), source_digest="b" * 64
        ).kind
        == "trained_model"
    )


def test_reuse_origin_and_parameter_source_are_exclusive():
    with pytest.raises(PydanticValidationError, match="recipe origin"):
        TrainingOrigin(kind="recipe", digest="a" * 64, recipe_id=uuid4())
    assert TrainingOrigin(kind="clone", digest="a" * 64, run_id=uuid4()).kind == "clone"
    with pytest.raises(PydanticValidationError, match="cannot be sent together"):
        TrainingRunCreate(
            name="run",
            dataset_id=uuid4(),
            request_id=uuid4(),
            configuration=TrainingConfiguration(schema_id="s", schema_digest="a" * 64),
            parameters=TrainingParameters(epochs=10),
        )


def test_preflight_serialization_omits_defaults_and_preserves_explicit_null(
    client, monkeypatch
):
    calls = []

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return _Response()

    monkeypatch.setattr(client._http, "request", request)
    dataset_id = uuid4()
    body = TrainingRunCreate(
        name="run",
        dataset_id=dataset_id,
        request_id=uuid4(),
        configuration=TrainingConfiguration(
            schema_id="schema", schema_digest="c" * 64, values={"epochs": 20}
        ),
        initialization=None,
    )
    TrainingRuns(client._http).preflight(uuid4(), body)
    sent = calls[0][2]["json"]
    assert sent["configuration"] == {
        "schema_id": "schema",
        "schema_digest": "c" * 64,
        "values": {"epochs": 20},
    }
    assert "parameters" not in sent
    assert "initialization" in sent and sent["initialization"] is None
    assert "preflight_digest" not in sent


def test_preview_budget_fields_require_bounded_integers():
    with pytest.raises(PydanticValidationError):
        TrainingPreviewCreate(
            dataset_id=uuid4(),
            framework="ultralytics",
            base_model="yolo11n",
            configuration=TrainingConfiguration(schema_id="s", schema_digest="a" * 64),
            request_id=uuid4(),
            count=5,
        )
    with pytest.raises(PydanticValidationError):
        TrainingPreviewCreate(
            dataset_id=uuid4(),
            framework="ultralytics",
            base_model="yolo11n",
            configuration=TrainingConfiguration(schema_id="s", schema_digest="a" * 64),
            request_id=uuid4(),
            epoch=True,
        )
