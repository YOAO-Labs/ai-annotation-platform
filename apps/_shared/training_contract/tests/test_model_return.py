"""Model return remains independent of native training parameter qualification."""

from copy import deepcopy
import pytest

from training_contract.model_return import registration_eligibility


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
