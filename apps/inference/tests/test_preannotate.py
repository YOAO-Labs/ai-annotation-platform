from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from aap_inference_contract.preannotate import PipelineStage, PreannotateRequest


def test_preannotate_flat_request_defaults_and_required_backend():
    with pytest.raises(ValidationError):
        PreannotateRequest()

    request = PreannotateRequest(ml_backend_id=uuid.uuid4())
    assert request.output_mode == "mask"
    assert request.execution_scope == "bulk"
    assert request.predict_mode == "skip_predicted"
    assert request.pipeline_stages is None


def test_pipeline_stage_normalizes_task_fields_and_rejects_extra_keys():
    stage = PipelineStage.model_validate(
        {"stage": 0, "ml_backend_id": str(uuid.uuid4()), "task_type": "detection"}
    )
    assert stage.task == "detect"
    with pytest.raises(ValidationError):
        PipelineStage.model_validate(
            {
                "stage": 0,
                "ml_backend_id": str(uuid.uuid4()),
                "unrecognized": True,
            }
        )


def test_multistage_request_derives_legacy_top_level_fields_from_root():
    root_backend = uuid.uuid4()
    child_backend = uuid.uuid4()
    request = PreannotateRequest(
        ml_backend_id=uuid.uuid4(),
        model_id="ignored-top-level",
        pipeline_stages=[
            {
                "stage": 0,
                "ml_backend_id": root_backend,
                "model_id": "detector",
                "task_type": "detection",
                "write": {"target": "geometry"},
            },
            {
                "stage": 1,
                "ml_backend_id": child_backend,
                "parent_stage": 0,
                "task_type": "classification",
                "write": {"target": "attributes", "keys": ["color"]},
            },
        ],
    )

    assert request.ml_backend_id == root_backend
    assert request.model_id == "detector"
    assert request.task == "detect"
    assert [stage.task for stage in request.pipeline_stages] == [
        "detect",
        "classify",
    ]


@pytest.mark.parametrize(
    ("stages", "message"),
    [
        (
            [
                {"stage": 0, "ml_backend_id": str(uuid.uuid4())},
                {"stage": 0, "ml_backend_id": str(uuid.uuid4())},
            ],
            "pipeline_stages 的 stage 序号不可重复",
        ),
        (
            [{"stage": 0, "ml_backend_id": str(uuid.uuid4()), "parent_stage": 0}],
            "pipeline_stages 须恰有一个源阶段 (parent_stage=None)",
        ),
    ],
)
def test_preannotate_request_keeps_pipeline_error_messages(stages, message):
    with pytest.raises(ValidationError) as error:
        PreannotateRequest(ml_backend_id=uuid.uuid4(), pipeline_stages=stages)
    assert message in str(error.value)
