"""Video feedback request validation and persistence boundaries."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from app.db.models.annotation_feedback import AnnotationFeedback
from app.db.models.dataset import DatasetItem
from app.schemas.annotation_feedback import (
    AnnotationFeedbackCreate,
    AnnotationFeedbackOut,
    serialize_feedback_anchor,
)
from app.services.feedback import FeedbackService
from tests.factory import create_project
from tests.test_video_feedback_context_api import (
    video_feedback_data as video_feedback_data,
)


def context():
    return {
        "schema_version": 1,
        "track_id": "car-track/left",
        "annotation_version": 2,
        "frame_range": {"from_frame": 120, "to_frame": 160},
        "viewport": {"center_x": -0.25, "center_y": 1.25, "zoom": 2.5},
        "timeline_window": {"from": 110.5, "to": 170.25},
    }


def payload():
    return {
        "kind": "issue",
        "anchor_type": "pixel",
        "project_id": uuid4(),
        "task_id": uuid4(),
        "annotation_id": uuid4(),
        "body": "Review the crossing track",
        "anchor_position": {
            "x": 0.25,
            "y": 0.5,
            "frame": 140,
            "video_context": context(),
        },
    }


def test_video_context_serializes_exact_wire_keys_and_offscreen_viewport():
    source = payload()
    parsed = AnnotationFeedbackCreate.model_validate(source)
    serialized = serialize_feedback_anchor(parsed)
    assert serialized["video_context"] == context()
    assert serialized["video_context"]["timeline_window"] == {
        "from": 110.5,
        "to": 170.25,
    }
    assert "from_" not in serialized["video_context"]["timeline_window"]


@pytest.mark.parametrize("frame", [0, 17])
def test_optional_video_context_and_source_frame_zero(frame):
    source = payload()
    source["annotation_id"] = None
    source["anchor_position"].update(frame=frame, video_context={"schema_version": 1})
    parsed = AnnotationFeedbackCreate.model_validate(source)
    assert serialize_feedback_anchor(parsed)["video_context"] == {"schema_version": 1}
    assert parsed.anchor_position.frame == frame


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("schema_version",), 2),
        (("schema_version",), True),
        (("schema_version",), 1.0),
        (("schema_version",), "1"),
        (("schema_version",), None),
        (("extra",), 1),
        (("track_id",), 123),
        (("annotation_version",), 0),
        (("annotation_version",), 1.5),
        (("annotation_version",), True),
        (("annotation_version",), "2"),
        (("frame_range", "from_frame"), -1),
        (("frame_range", "from_frame"), 120.0),
        (("frame_range", "to_frame"), True),
        (("frame_range", "to_frame"), "160"),
        (("frame_range", "extra"), 1),
        (("viewport", "center_x"), float("nan")),
        (("viewport", "center_y"), float("inf")),
        (("viewport", "center_x"), True),
        (("viewport", "center_y"), "0.5"),
        (("viewport", "zoom"), 0),
        (("viewport", "zoom"), -1),
        (("viewport", "zoom"), float("-inf")),
        (("viewport", "extra"), 1),
        (("timeline_window", "from"), -0.1),
        (("timeline_window", "from"), float("nan")),
        (("timeline_window", "to"), float("inf")),
        (("timeline_window", "to"), "170"),
        (("timeline_window", "from_"), 110),
    ],
)
def test_video_context_rejects_unknown_fields_and_non_contract_values(path, value):
    source = payload()
    target = source["anchor_position"]["video_context"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValidationError):
        AnnotationFeedbackCreate.model_validate(source)


@pytest.mark.parametrize(
    "field", ["schema_version", "frame_range", "viewport", "timeline_window"]
)
def test_video_context_requires_complete_provided_objects(field):
    source = payload()
    if field == "schema_version":
        source["anchor_position"]["video_context"].pop(field)
    else:
        source["anchor_position"]["video_context"][field] = {}
    with pytest.raises(ValidationError):
        AnnotationFeedbackCreate.model_validate(source)


@pytest.mark.parametrize("frame", [None, True, 0.0, "0", -1])
def test_video_context_requires_strict_legacy_frame(frame):
    source = payload()
    source["anchor_position"]["frame"] = frame
    with pytest.raises(ValidationError):
        AnnotationFeedbackCreate.model_validate(source)


def test_video_context_requires_pixel_anchor_and_annotation_for_version():
    source = payload()
    source["annotation_id"] = None
    with pytest.raises(ValidationError):
        AnnotationFeedbackCreate.model_validate(source)
    source = payload()
    source["anchor_type"] = "task"
    with pytest.raises(ValidationError):
        AnnotationFeedbackCreate.model_validate(source)


def test_legacy_pixel_and_mask_locator_serialization_is_unchanged():
    source = payload()
    source["anchor_position"].pop("video_context")
    source["anchor_position"]["frame"] = "17"
    source["anchor_position"].update(
        mask_qc_issue_id=uuid4(),
        region_bbox=[0.1, 0.2, 0.5, 0.8],
        region_digest="a" * 64,
        boundary_digest="b" * 64,
        compare_locator={
            "baseline_kind": "previous_version",
            "mode": "boundary",
            "current_digest": "a" * 64,
            "baseline_digest": "b" * 64,
        },
    )
    value = serialize_feedback_anchor(AnnotationFeedbackCreate.model_validate(source))
    assert "video_context" not in value
    assert "scene_id" not in value
    assert value["frame"] == 17
    assert value["compare_locator"]["mode"] == "boundary"
    assert value["region_bbox"] == [0.1, 0.2, 0.5, 0.8]


def test_unknown_context_version_is_preserved_by_response_model():
    source = payload()
    anchor = source["anchor_position"]
    anchor["video_context"] = {
        "schema_version": 99,
        "future_view": {"matrix": [1, 2, 3]},
    }
    response = AnnotationFeedbackOut.model_validate(
        {
            **source,
            "id": uuid4(),
            "status": "open",
            "author_id": uuid4(),
            "is_active": True,
            "created_at": datetime.now(timezone.utc),
        }
    )
    assert response.anchor_position == anchor


@pytest.fixture
def stored_issue(video_feedback_data):
    data = video_feedback_data
    return {
        **payload(),
        "project_id": data["project"].id,
        "task_id": data["task"].id,
        "annotation_id": data["annotation"].id,
        "author_id": data["owner"].id,
        "severity": "warn",
        "title": None,
        "attachments": [],
        "thread_parent_id": None,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("frame", 180),
        ("frame_range", {"from_frame": 160, "to_frame": 120}),
        ("frame_range", {"from_frame": 141, "to_frame": 160}),
        ("frame_range", {"from_frame": 0, "to_frame": 139}),
        ("timeline_window", {"from": 0, "to": 179.01}),
    ],
)
@pytest.mark.asyncio
async def test_service_rejects_bad_media_ranges_before_insert(
    db_session, stored_issue, field, value
):
    source = stored_issue
    if field == "frame":
        source["anchor_position"][field] = value
    else:
        source["anchor_position"]["video_context"][field] = value
    with pytest.raises(HTTPException) as error:
        await FeedbackService(db_session).create(**source)
    assert error.value.status_code == 422
    assert not await db_session.scalar(
        select(AnnotationFeedback.id).where(
            AnnotationFeedback.task_id == source["task_id"]
        )
    )


@pytest.mark.parametrize(
    ("failure", "status"),
    [
        ("task_missing", 404),
        ("task_project", 422),
        ("annotation_missing", 404),
        ("annotation_deleted", 404),
        ("annotation_cancelled", 404),
        ("annotation_project", 422),
        ("metadata_missing", 503),
    ],
)
@pytest.mark.asyncio
async def test_service_checks_real_task_object_and_media_ownership(
    db_session, video_feedback_data, stored_issue, failure, status
):
    source = stored_issue
    annotation = video_feedback_data["annotation"]
    if failure == "task_missing":
        source["task_id"] = uuid4()
    elif failure == "task_project":
        source["project_id"] = uuid4()
    elif failure == "annotation_missing":
        source["annotation_id"] = uuid4()
    elif failure == "annotation_deleted":
        annotation.is_active = False
    elif failure == "annotation_cancelled":
        annotation.was_cancelled = True
    elif failure == "annotation_project":
        other = await create_project(db_session, owner_id=source["author_id"])
        annotation.project_id = other.id
    else:
        item = await db_session.get(
            DatasetItem, video_feedback_data["task"].dataset_item_id
        )
        item.metadata_ = {}
    await db_session.flush()
    with pytest.raises(HTTPException) as error:
        await FeedbackService(db_session).create(**source)
    assert error.value.status_code == status
    assert not await db_session.scalar(
        select(AnnotationFeedback.id).where(
            AnnotationFeedback.author_id == source["author_id"]
        )
    )
