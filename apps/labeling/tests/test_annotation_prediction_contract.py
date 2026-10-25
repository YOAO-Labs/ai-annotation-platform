from datetime import datetime, timezone
import json
import uuid

import pytest

from aap_labeling.annotation_prediction_contract import (
    ANNOTATION_PREDICTION_PROTOCOL,
    AnnotationPredictionUnavailable,
    ApplySecondaryAttributes,
    CreateAcceptedAnnotation,
    MarkSecondaryChild,
    decode_annotation_prediction_command,
)
from aap_labeling.annotation_prediction_data import AnnotationPredictionDataClient


def _context():
    return {
        "schema": ANNOTATION_PREDICTION_PROTOCOL,
        "annotation_id": uuid.uuid4(),
        "task_id": uuid.uuid4(),
        "project_id": uuid.uuid4(),
        "actor_id": uuid.uuid4(),
        "expected_batch_id": None,
        "expected_status": "in_progress",
        "expected_review_round_id": None,
    }


@pytest.mark.parametrize(
    "operation",
    ["create_accepted", "apply_secondary_attributes", "mark_secondary_child"],
)
@pytest.mark.asyncio
async def test_prediction_annotation_reply_is_bound_to_the_operation_and_target(
    operation,
):
    context = _context()
    if operation == "create_accepted":
        request = CreateAcceptedAnnotation(
            **context,
            operation=operation,
            prediction_id=uuid.uuid4(),
            prediction_created_at=datetime.now(timezone.utc),
            annotation_type="bbox",
            tool_unit_id="bbox",
            class_name="car",
            geometry={"type": "bbox"},
            confidence=0.5,
            video_segment_id=None,
            attributes={"color": "red"},
            attributes_meta={"color": {"origin": "ai"}},
            track_id=None,
            temporal_role="sample",
        )
        version = 1
    elif operation == "apply_secondary_attributes":
        request = ApplySecondaryAttributes(
            **context,
            operation=operation,
            expected_version=3,
            attributes={"color": "red"},
            attributes_meta={"color": {"origin": "ai"}},
        )
        version = 4
    else:
        request = MarkSecondaryChild(
            **context,
            operation=operation,
            expected_version=1,
            parent_annotation_id=uuid.uuid4(),
        )
        version = 2

    wrong_id = False

    async def execute(payload):
        assert decode_annotation_prediction_command(payload) == request
        return {
            "schema": ANNOTATION_PREDICTION_PROTOCOL,
            "ok": True,
            "request": json.loads(payload),
            "data": {
                "annotation_id": str(
                    uuid.uuid4() if wrong_id else request.annotation_id
                ),
                "source": "prediction_based",
                "version": version,
                "updated_at": "2026-10-25T00:00:00Z",
            },
        }

    client = AnnotationPredictionDataClient(execute)
    assert (await client.command(request)).version == version
    wrong_id = True
    with pytest.raises(AnnotationPredictionUnavailable):
        await client.command(request)
    unknown = json.loads(request.model_dump_json(by_alias=True))
    unknown["authorized"] = True
    with pytest.raises(AnnotationPredictionUnavailable):
        decode_annotation_prediction_command(unknown)
