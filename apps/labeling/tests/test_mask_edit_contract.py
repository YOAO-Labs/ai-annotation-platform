from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from aap_labeling.ai_mask_accept_contract import (
    AI_MASK_ACCEPT_PROTOCOL,
    RefineAiMaskAnnotation,
    ai_mask_accept_command_json,
    decode_ai_mask_accept_command,
    AiMaskAcceptUnavailable,
)
from aap_labeling.mask_edit_contract import (
    MASK_EDIT_PROTOCOL,
    ApplyPolygonSlice,
    MaskAnnotationData,
    MaskEditUnavailable,
    decode_mask_edit_command,
    decode_mask_edit_reply,
    mask_edit_command_json,
)
from aap_labeling.mask_revision_contract import (
    MASK_REVISION_PROTOCOL,
    MaskRevisionReference,
    ResolveMaskRevisions,
    decode_mask_revision_command,
    MaskRevisionUnavailable,
    mask_revision_command_json,
)


def test_mask_edit_command_is_closed_and_reply_is_bound_to_request():
    now = datetime.now(timezone.utc)
    request = ApplyPolygonSlice(
        schema=MASK_EDIT_PROTOCOL,
        operation="apply_polygon_slice",
        task_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        actor_id=uuid.uuid4(),
        expected_batch_id=None,
        expected_status="in_progress",
        expected_review_round_id=None,
        source_annotation_id=uuid.uuid4(),
        created_annotation_id=uuid.uuid4(),
        expected_version=4,
        source_geometry={"type": "polygon", "points": [1, 2]},
        created_geometry={"type": "polygon", "points": [3, 4]},
    )
    encoded = mask_edit_command_json(request)
    assert decode_mask_edit_command(encoded) == request

    source = MaskAnnotationData(
        annotation_id=request.source_annotation_id,
        action="update",
        version=5,
        annotation_type="polygon",
        track_id=None,
        user_id=request.actor_id,
        video_segment_id=None,
        tool_unit_id="region",
        class_name="car",
        geometry=request.source_geometry,
        confidence=None,
        attributes={},
        attributes_meta={},
        z_order=0,
        parent_annotation_id=None,
        is_active=True,
        updated_at=now,
    )
    created = source.model_copy(
        update={
            "annotation_id": request.created_annotation_id,
            "action": "create",
            "version": 1,
            "geometry": request.created_geometry,
        }
    )
    data = {
        "source": source.model_dump(mode="json"),
        "created": created.model_dump(mode="json"),
    }
    reply = {
        "schema": MASK_EDIT_PROTOCOL,
        "ok": True,
        "request": request.model_dump(mode="json", by_alias=True),
        "data": data,
    }
    assert decode_mask_edit_reply(request, reply).created.annotation_id == (
        request.created_annotation_id
    )
    reply["request"]["created_annotation_id"] = str(uuid.uuid4())
    with pytest.raises(MaskEditUnavailable):
        decode_mask_edit_reply(request, reply)
    with pytest.raises(MaskEditUnavailable):
        decode_mask_edit_command(
            {**request.model_dump(mode="json", by_alias=True), "sql": ""}
        )


def test_mask_revision_request_requires_digest_bound_reference():
    request = ResolveMaskRevisions(
        schema=MASK_REVISION_PROTOCOL,
        operation="resolve",
        task_id=uuid.uuid4(),
        references=[
            MaskRevisionReference(
                annotation_id=uuid.uuid4(),
                annotation_version=2,
                geometry_digest="a" * 64,
            )
        ],
        lock=True,
    )
    assert decode_mask_revision_command(mask_revision_command_json(request)) == request
    raw = request.model_dump(mode="json", by_alias=True)
    raw["references"][0]["geometry_digest"] = "invalid"
    with pytest.raises(MaskRevisionUnavailable):
        decode_mask_revision_command(raw)


def test_ai_mask_refinement_command_rejects_unfenced_requests():
    request = RefineAiMaskAnnotation(
        schema=AI_MASK_ACCEPT_PROTOCOL,
        operation="refine_annotation",
        task_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        actor_id=uuid.uuid4(),
        expected_batch_id=None,
        expected_status="in_progress",
        expected_review_round_id=None,
        annotation_id=uuid.uuid4(),
        expected_version=3,
        video_segment_id=None,
        annotation_type="raster_mask",
        prediction_id=uuid.uuid4(),
        class_name="car",
        geometry={"type": "raster_mask", "mask": {"sha256": "b" * 64}},
        track_id=None,
        confidence=0.8,
    )
    raw = ai_mask_accept_command_json(request)
    assert decode_ai_mask_accept_command(raw) == request
    with pytest.raises(AiMaskAcceptUnavailable):
        decode_ai_mask_accept_command(
            raw.replace('"expected_version":3', '"expected_version":0')
        )
