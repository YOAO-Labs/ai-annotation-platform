"""Pure architecture identity accepts official legacy layouts without aliases."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from training_contract.models import model_spec
from training_contract.native_architecture import (
    validate_architecture_yaml,
    reference_signature,
)


def canonical(model):
    return {
        "scale": model.scale,
        "yaml_file": model.yaml_filename,
        "scales": {
            "n": [0.33, 0.25, 1024],
            "m": [0.67, 0.75, 768],
            "x": [1, 1.25, 512],
        },
        "backbone": [[-1, 1, "Conv", [1024, 3, 2]], [-1, 3, "C2f", [1024, True]]],
        "head": [[-1, 1, model.head, ["nc", 32, 256]]],
    }


@pytest.mark.parametrize("base", ["yolov8n", "yolov8m-seg", "yolov8x-seg"])
def test_legacy_scale_requires_complete_capped_topology_and_preserves_metadata(base):
    model = model_spec("ultralytics", base)
    expected = canonical(model)
    legacy = deepcopy(expected)
    legacy.pop("scale")
    legacy.pop("yaml_file")
    depth, width, maximum = legacy.pop("scales")[model.scale]
    legacy.update(depth_multiple=depth, width_multiple=width)
    for layer in legacy["backbone"]:
        layer[3][0] = min(layer[3][0], maximum)
    original = deepcopy(legacy)
    validate_architecture_yaml(legacy, model, expected)
    assert legacy == original
    legacy["backbone"][0][3][0] += 1
    with pytest.raises(ValueError, match="topology"):
        validate_architecture_yaml(legacy, model, expected)


def test_current_metadata_cannot_claim_another_scale_or_use_legacy_on_other_family():
    model = model_spec("ultralytics", "yolo26n-seg")
    expected = canonical(model)
    validate_architecture_yaml(deepcopy(expected), model, expected)
    changed = {**expected, "scale": "m"}
    with pytest.raises(ValueError, match="metadata"):
        validate_architecture_yaml(changed, model, expected)
    changed = {
        key: value
        for key, value in expected.items()
        if key not in {"scale", "yaml_file"}
    }
    with pytest.raises(ValueError, match="legacy"):
        validate_architecture_yaml(changed, model, expected)


def test_reference_cache_retains_only_identity_data_and_keys_by_classes():
    calls = []

    def factory(model, classes):
        calls.append((model, classes))
        return SimpleNamespace(
            named_modules=lambda **_: [],
            state_dict=lambda: {"w": SimpleNamespace(shape=(classes, 2))},
        )

    assert reference_signature("model", 2, factory, "8.4.183") == reference_signature(
        "model", 2, factory, "8.4.183"
    )
    assert calls == [("model", 2)]
    assert reference_signature("model", 3, factory, "8.4.183") != reference_signature(
        "model", 2, factory, "8.4.183"
    )

    reference_signature("model", 2, factory, "8.4.177")
    assert calls.count(("model", 2)) == 2
