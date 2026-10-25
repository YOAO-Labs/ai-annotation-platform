from __future__ import annotations

import pytest

from aap_protocol_v2 import vocab
from aap_protocol_v2.lifecycle import ManagedLifecycleCapabilities

from aap_inference_contract.capabilities import CapabilityLimits
from aap_inference import (
    ModelCapability,
    derive_modalities,
    extract_capabilities,
    resolve_model_task,
)
from aap_inference.capability_registry import (
    GEOMETRY_VALUES,
    INFRA_VALUES,
    INPUT_VALUES,
    MODALITY_VALUES,
    PROMPTS_INTERACTIVE_ROUTE,
    PROMPTS_REQUIRES_INPUT,
    TASK_DEFAULT_GEOMETRY,
    TASK_VALUES,
    TASKS,
)


def test_capability_limits_keep_app_defaults_and_are_explicitly_overridable():
    assert CapabilityLimits() == CapabilityLimits(
        video_tracker_window_size_frames=300,
        video_tracker_sam3_window_size_frames=16,
    )
    caps = extract_capabilities(
        {
            "models": [
                {
                    "id": "sam3",
                    "task": "tracker",
                    "supported_trackers": ["sam3_video"],
                    "max_window_frames": 64,
                },
                {
                    "id": "generic",
                    "task": "tracker",
                    "supported_trackers": ["bytetrack"],
                    "max_window_frames": 64,
                },
            ]
        },
        limits=CapabilityLimits(
            video_tracker_window_size_frames=40,
            video_tracker_sam3_window_size_frames=6,
        ),
    )
    assert [model["max_window_frames"] for model in caps["models"]] == [6, 40]


@pytest.mark.parametrize(
    "identity",
    [
        {"weights_version": "legacy", "model_version": "label"},
        {"weights_release_id": "release"},
        {"weights_release_id": "release", "weights_sha256": "A" * 64},
        {"weights_release_id": " release", "weights_sha256": "a" * 64},
        {"weights_release_id": "release", "weights_sha256": 1},
        {"weights_release_id": "release", "weights_sha256": "a" * 64},
    ],
)
def test_exact_per_model_weights_identity_survives_normalization_only_as_a_pair(
    identity,
):
    caps = extract_capabilities(
        {
            "models": [
                {
                    "id": "tracker",
                    "task": "tracker",
                    "supported_trackers": ["sam2_video"],
                    **identity,
                }
            ]
        }
    )
    result = caps["models"][0]
    expected = (
        identity.get("weights_release_id") == "release"
        and identity.get("weights_sha256") == "a" * 64
    )
    assert ("weights_release_id" in result) is expected
    assert ("weights_sha256" in result) is expected
    if expected:
        assert (
            ModelCapability.model_validate(result).model_dump()["weights_sha256"]
            == "a" * 64
        )


def test_capability_snapshot_handles_empty_and_legacy_backends():
    assert extract_capabilities(None) is None
    assert extract_capabilities({}) is None

    caps = extract_capabilities(
        {
            "name": "legacy",
            "supported_prompts": ["text"],
            "supported_geometric_outputs": ["bbox"],
        }
    )
    assert caps["models"][0]["task"] == "detect"
    assert caps["models"][0]["supported_inputs"] == ["full_image"]
    assert caps["modalities"] == ["image"]


def test_top_level_text_outputs_keep_each_models_first_seen_union():
    caps = extract_capabilities(
        {
            "models": [
                {
                    "id": "first",
                    "task": "detect",
                    "supported_text_outputs": ["box", "mask"],
                },
                {
                    "id": "second",
                    "task": "detect",
                    "supported_text_outputs": ["mask", "both"],
                },
            ]
        }
    )
    assert caps["supported_text_outputs"] == ["box", "mask", "both"]
    assert [model["supported_text_outputs"] for model in caps["models"]] == [
        ["box", "mask"],
        ["mask", "both"],
    ]


def test_tracker_and_lidar_modalities_are_derived_in_canonical_order():
    caps = extract_capabilities(
        {
            "supported_prompts": ["none"],
            "supported_trackers": ["bytetrack"],
            "models": [
                {
                    "id": "image",
                    "task": "detect",
                    "supported_prompts": ["none"],
                },
                {
                    "id": "video",
                    "task": "tracker",
                    "supported_trackers": ["bytetrack"],
                },
                {
                    "id": "lidar",
                    "task": "detect",
                    "supported_geometric_outputs": ["lidar_box_3d"],
                },
            ],
        }
    )
    assert caps["modalities"] == ["image", "video", "lidar"]
    assert derive_modalities(None) == []


def test_capability_warning_diagnostics_keep_unknown_values():
    caps = extract_capabilities(
        {
            "models": [
                {
                    "id": "m1",
                    "task": "seg",
                    "supported_prompts": ["boxes"],
                    "supported_geometric_outputs": ["circle"],
                }
            ]
        }
    )
    model = caps["models"][0]
    assert model["task"] == "seg"
    assert model["supported_prompts"] == ["boxes"]
    warnings = {(item["field"], item["value"]) for item in caps["warnings"]}
    assert {("task", "seg"), ("supported_prompts", "boxes")} <= warnings
    assert ("supported_geometric_outputs", "circle") in warnings


def test_model_capability_fields_allow_future_setup_values_without_snapshot_leak():
    value = ModelCapability.model_validate(
        {"id": "x", "task": "detection", "future_value": "preserved"}
    )
    assert value.task == "detect"
    assert value.model_extra == {"future_value": "preserved"}
    caps = extract_capabilities(
        {"models": [{"id": "x", "task": "detect", "future_value": "preserved"}]}
    )
    assert not any(item["field"] == "id" for item in caps["warnings"])
    assert "future_value" not in caps["models"][0]


def test_managed_lifecycle_is_only_persisted_when_canonical():
    canonical = ManagedLifecycleCapabilities().model_dump(mode="json")
    valid = extract_capabilities({"managed_lifecycle": canonical})
    assert valid["managed_lifecycle"] == canonical
    assert not any(item["field"] == "managed_lifecycle" for item in valid["warnings"])

    invalid = extract_capabilities(
        {"managed_lifecycle": {**canonical, "unexpected": True}}
    )
    assert invalid["managed_lifecycle"] is None
    assert any(
        item["field"] == "managed_lifecycle" and item["value"] == "invalid"
        for item in invalid["warnings"]
    )


def test_model_task_resolution_keeps_opaque_ids_and_rejects_conflicts():
    backend = type(
        "Backend",
        (),
        {
            "health_meta": {
                "capabilities": {
                    "models": [{"id": "opaque/model:v1", "task": "detect"}]
                }
            }
        },
    )()
    assert resolve_model_task(backend, "opaque/model:v1") == "detect"
    assert resolve_model_task(backend, "missing", "classification") == "classify"
    try:
        resolve_model_task(backend, "opaque/model:v1", "classification")
    except ValueError as error:
        assert str(error) == "task 与所选模型的任务不一致"
    else:
        raise AssertionError("task mismatch must fail")


def test_registry_vocabularies_preserve_order_and_prompt_semantics():
    assert TASK_VALUES == tuple(task.id for task in TASKS)
    assert TASK_DEFAULT_GEOMETRY["tracker"] == []
    assert INFRA_VALUES
    assert GEOMETRY_VALUES
    assert INPUT_VALUES
    assert MODALITY_VALUES == ("image", "video", "lidar")
    assert "text" in PROMPTS_REQUIRES_INPUT
    assert "text" not in PROMPTS_INTERACTIVE_ROUTE


def test_registry_protocol_mirrors_stay_in_sync():
    assert INFRA_VALUES == vocab.INFRA_VALUES
    assert TASK_VALUES == vocab.TASK_VALUES
    assert GEOMETRY_VALUES == vocab.GEOMETRY_VALUES
    assert INPUT_VALUES == vocab.INPUT_VALUES
    from aap_inference.capability_registry import PROMPT_VALUES

    assert PROMPT_VALUES == vocab.PROMPT_VALUES
    assert {
        key: tuple(value) for key, value in TASK_DEFAULT_GEOMETRY.items()
    } == vocab.TASK_DEFAULT_GEOMETRY
