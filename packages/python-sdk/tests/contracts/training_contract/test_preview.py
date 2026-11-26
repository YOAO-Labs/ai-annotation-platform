import copy

import pytest

from training_contract.preview import (
    INPUT_KEYS,
    POLICY,
    POLICY_DIGEST,
    input_digest,
    validate_request,
)


def plugin_config(*, capabilities=None, epochs=10, imgsz=640, multi_scale=0.0):
    return {
        "adapter_revision": 3,
        "framework": "custom-trainer",
        "framework_version": "1.0.0",
        "base_model": {"id": "custom-n"},
        "trainer_plugin": {
            "plugin_id": "example.trainer",
            "capabilities": (
                ["train", "preview"] if capabilities is None else capabilities
            ),
        },
        "configuration": {
            "effective_values": {
                "epochs": epochs,
                "imgsz": imgsz,
                "multi_scale": multi_scale,
            }
        },
        "parameters": {"epochs": epochs, "imgsz": imgsz},
    }


def test_preview_request_validation_is_framework_agnostic_and_non_mutating():
    config = plugin_config()
    before = copy.deepcopy(config)
    validate_request(config, 123, 9, 4)
    assert config == before
    assert POLICY["purpose"] == "augmentation-preview"
    assert len(POLICY_DIGEST) == 64
    spec = {key: idx for idx, key in enumerate(INPUT_KEYS)}
    assert len(input_digest(spec)) == 64


@pytest.mark.parametrize(
    "seed,epoch,count", [(True, 1, 1), (-1, 1, 1), (1, 0, 1), (1, 11, 1), (1, 1, 5)]
)
def test_preview_request_bounds(seed, epoch, count):
    with pytest.raises(ValueError):
        validate_request(plugin_config(), seed, epoch, count)


def test_preview_rejects_plugins_without_preview_capability_or_oversized_multiscale():
    with pytest.raises(ValueError, match="增强预览"):
        validate_request(plugin_config(capabilities=["train"]), 1, 1, 1)
    with pytest.raises(ValueError, match="尺寸上限"):
        validate_request(plugin_config(imgsz=2048, multi_scale=0.5), 1, 1, 1)


def test_preview_rejects_non_detection_or_invalid_task():
    legacy = plugin_config()
    legacy["task"] = "image-detection"
    validate_request(legacy, 1, 1, 1)

    seg_top = plugin_config()
    seg_top["task"] = "segment"
    with pytest.raises(ValueError, match="图片检测"):
        validate_request(seg_top, 1, 1, 1)

    seg_nested = plugin_config()
    seg_nested["configuration"]["identity"] = {"task": "segment"}
    with pytest.raises(ValueError, match="图片检测"):
        validate_request(seg_nested, 1, 1, 1)

    bad_task = plugin_config()
    bad_task["task"] = {"invalid": True}
    with pytest.raises(ValueError, match="图片检测"):
        validate_request(bad_task, 1, 1, 1)
