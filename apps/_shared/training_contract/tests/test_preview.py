import copy

import pytest

from training_contract import definition, resolve
from training_contract.preview import closed_mosaic, native_values, validate_request


def configuration(framework="ultralytics", values=None):
    model = "yolo11n" if framework == "ultralytics" else "LibreYOLO9t"
    schema = definition(framework, model)
    frozen = resolve(
        framework,
        model,
        {
            "schema_id": schema["schema_id"],
            "schema_digest": schema["schema_digest"],
            "values": values or {"epochs": 10, "close_mosaic": 2},
        },
    )
    return {
        "adapter_revision": 2,
        "framework": framework,
        "framework_version": schema["framework_version"],
        "base_model": {"id": model},
        "configuration": frozen,
        "parameters": {
            key: frozen["effective_values"][key]
            for key in ("epochs", "batch", "imgsz", "seed", "lr0", "patience")
        },
    }


def test_preview_materialization_never_changes_the_frozen_training_contract():
    config = configuration()
    before = copy.deepcopy(config)
    validate_request(config, 123, 9, 4)
    actual = native_values(config)
    assert actual["device"] == "cpu" and actual["workers"] == 0
    assert actual["seed"] == config["parameters"]["seed"]
    assert config == before
    assert not closed_mosaic(config, 8)
    assert closed_mosaic(config, 9) and closed_mosaic(config, 10)


@pytest.mark.parametrize(
    "seed,epoch,count", [(True, 1, 1), (-1, 1, 1), (1, 0, 1), (1, 11, 1), (1, 1, 5)]
)
def test_preview_request_bounds(seed, epoch, count):
    with pytest.raises(ValueError):
        validate_request(configuration(), seed, epoch, count)


def test_unqualified_native_framework_is_explicit():
    config = configuration("libreyolo", {"epochs": 10})
    with pytest.raises(ValueError, match="Ultralytics"):
        validate_request(config, 1, 1, 1)
