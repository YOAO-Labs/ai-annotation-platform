from __future__ import annotations

import uuid

import pytest

from aap_inference import PipelineValidationError, validate_saved_pipeline


def test_empty_pipeline_values_pass_through():
    assert validate_saved_pipeline(None) is None
    empty: list[dict] = []
    assert validate_saved_pipeline(empty) is empty


@pytest.mark.parametrize("value", ["not-a-list", {"stage": 0}, [None]])
def test_saved_pipeline_rejects_non_stage_arrays_with_legacy_detail(value):
    with pytest.raises(PipelineValidationError) as error:
        validate_saved_pipeline(value)
    assert error.value.detail == "preannotate_pipeline 须为阶段对象数组"


def test_saved_pipeline_rejects_multiple_or_missing_roots_with_legacy_detail():
    with pytest.raises(PipelineValidationError) as error:
        validate_saved_pipeline(
            [
                {"stage": 0, "ml_backend_id": str(uuid.uuid4())},
                {"stage": 1, "ml_backend_id": str(uuid.uuid4())},
            ]
        )
    assert error.value.detail == (
        "preannotate_pipeline 须恰有一个源阶段 (parent_stage=None)"
    )


def test_saved_pipeline_normalizes_values_without_backend_lookup():
    backend_id = uuid.uuid4()
    result = validate_saved_pipeline(
        [
            {
                "stage": 0,
                "ml_backend_id": str(backend_id).upper(),
                "task_type": "detection",
            }
        ]
    )
    assert result == [{"stage": 0, "ml_backend_id": str(backend_id), "task": "detect"}]


def test_saved_pipeline_wraps_schema_validation_in_portable_error():
    with pytest.raises(PipelineValidationError) as error:
        validate_saved_pipeline(
            [{"stage": 0, "ml_backend_id": "not-a-uuid", "unexpected": True}]
        )
    assert error.value.detail.startswith("preannotate_pipeline 校验失败: ")
    assert "validation error" in error.value.detail
