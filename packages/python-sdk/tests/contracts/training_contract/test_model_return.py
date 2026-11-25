"""Model return remains independent of native training parameter qualification."""

from copy import deepcopy
import pytest

from training_contract.model_return import registration_eligibility
from training_contract.dataset import SEGMENTATION_CONTRACT


def config(size="t"):
    return {
        "framework": "libreyolo",
        "framework_version": "1.6.0",
        "base_model": {"id": f"LibreYOLO9{size}"},
    }


@pytest.mark.parametrize("size", list("tsmc"))
def test_native_family_identity_includes_pinned_runtime_and_size(size):
    value = config(size)
    original = deepcopy(value)
    result = registration_eligibility(value)
    assert result["qualified"] and result["reason"] is None
    identity = result["qualification"]
    assert identity["model_family"] == "yolo9" and identity["model_size"] == size
    assert identity["runtime_qualification"]["device"] == "cpu"
    identity["runtime_qualification"]["revision"] = 99
    assert (
        registration_eligibility(value)["qualification"]["runtime_qualification"][
            "revision"
        ]
        == 1
    )
    assert value == original


@pytest.mark.parametrize(
    "change",
    [
        {"framework": "other"},
        {"framework_version": "1.5.0"},
        {"base_model": {"id": "LibreYOLO9e"}},
        {"base_model": {"id": "LibreYOLO26n"}},
        {"configuration": {"identity": {"task": "image-segmentation"}}},
    ],
)
def test_training_support_never_implies_inference_qualification(change):
    result = registration_eligibility({**config(), **change})
    assert not result["qualified"] and result["reason"]
    assert result["qualification"] is None


def segmentation_config():
    return {
        "task": "segment",
        "framework": "ultralytics",
        "framework_version": "8.4.177",
        "adapter_revision": 2,
        "base_model": {"id": "yolo11n-seg"},
        "data_contract": dict(SEGMENTATION_CONTRACT),
        "configuration": {
            "identity": {
                "task": "segment",
                "model_family": "yolo11",
                "framework": "ultralytics",
                "framework_version": "8.4.177",
            }
        },
    }


def test_qualified_segmentation_return_has_an_independent_immutable_identity():
    config = segmentation_config()
    original = deepcopy(config)
    result = registration_eligibility(config)
    assert (
        result["qualified"]
        and result["qualification"]["output_contract"] == "aap-instance-rle-1"
    )
    assert result["qualification"]["runtime_qualification"]["retina_masks"] is True
    result["qualification"]["runtime_qualification"]["source_sha256"].clear()
    assert registration_eligibility(config)["qualification"]["runtime_qualification"][
        "source_sha256"
    ]
    assert config == original


@pytest.mark.parametrize(
    "change",
    [
        {"framework": "libreyolo"},
        {"framework_version": "8.4.61"},
        {"adapter_revision": 1},
        {"base_model": {"id": "yolo11s-seg"}},
        {"data_contract": {**SEGMENTATION_CONTRACT, "bridge": "unknown-raster"}},
        {"configuration": {}},
        {"task": "detect"},
    ],
)
def test_segmentation_qualification_fails_closed_for_unreviewed_inputs(change):
    assert not registration_eligibility({**segmentation_config(), **change})[
        "qualified"
    ]


def test_detection_registration_response_remains_byte_compatible():
    assert registration_eligibility({"framework": "ultralytics"}) == {
        "qualified": True,
        "reason": None,
        "qualification": None,
    }
