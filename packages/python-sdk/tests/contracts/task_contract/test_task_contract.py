"""Cross-consumer task identity and conflicting legacy input boundaries."""

import sys
import subprocess
from pathlib import Path

import pytest

from yoao_task_contract import (
    legacy_inference_task,
    legacy_training_task,
    normalize_task,
    resolve_task,
)


@pytest.mark.parametrize(
    "value",
    [
        "detect",
        "segment",
        "classify",
        "pose",
        "obb",
        "ocr",
        "doc_layout",
        "tracker",
        "interactive_seg",
    ],
)
def test_canonical_tasks_are_stable(value):
    assert normalize_task(value) == value


@pytest.mark.parametrize(
    ("alias", "expected"),
    [
        ("detection", "detect"),
        ("object_detection", "detect"),
        ("image-detection", "detect"),
        ("image_detection", "detect"),
        ("segmentation", "segment"),
        ("instance-segmentation", "segment"),
        ("instance_segmentation", "segment"),
        ("image-instance-segmentation", "segment"),
        ("image_instance_segmentation", "segment"),
        ("classification", "classify"),
        ("image-classification", "classify"),
        ("image_classification", "classify"),
        ("cls", "classify"),
        ("keypoint", "pose"),
        ("keypoints", "pose"),
        ("image-keypoints", "pose"),
    ],
)
def test_legacy_aliases_have_one_canonical_identity(alias, expected):
    assert normalize_task(alias) == expected
    assert normalize_task(normalize_task(alias)) == expected


@pytest.mark.parametrize("value", ["", None, 1, "aap", "image", "text", "future-task"])
def test_format_media_prompt_and_unknown_values_are_not_tasks(value):
    with pytest.raises(ValueError):
        normalize_task(value)


def test_capability_readers_can_retain_unknown_values_for_diagnostics():
    assert normalize_task("future-task", allow_unknown=True) == "future-task"


def test_equivalent_keys_merge_and_conflicting_keys_fail():
    assert resolve_task("detect", "detection") == "detect"
    assert resolve_task(task_type="image-instance-segmentation") == "segment"
    assert resolve_task() is None
    with pytest.raises(ValueError, match="different"):
        resolve_task("detect", "segment")


def test_legacy_serialization_is_explicit_and_separate():
    assert legacy_training_task("detect") == "image-detection"
    assert legacy_inference_task("pose") == "keypoint"
    with pytest.raises(ValueError):
        legacy_training_task("pose")


def test_import_has_no_service_or_native_framework_dependency():
    import yoao_task_contract

    source = Path(yoao_task_contract.__file__).resolve().parents[1]
    code = (
        f"import sys; sys.path.insert(0, {str(source)!r}); import yoao_task_contract; "
        "assert not {'yoao_protocol_v2', 'fastapi', 'pydantic', 'torch'} & sys.modules.keys()"
    )
    subprocess.run([sys.executable, "-I", "-B", "-c", code], check=True)
