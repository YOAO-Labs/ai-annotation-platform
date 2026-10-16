"""Video feedback input and ownership checks without a database or media runtime."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.v1 import annotation_feedbacks as routes
from app.db.models.annotation import Annotation
from app.db.models.annotation_feedback import AnnotationFeedback
from app.db.models.dataset import DatasetItem
from app.db.models.task import Task
from app.schemas.annotation_feedback import (
    AnnotationFeedbackCreate,
    AnnotationFeedbackOut,
)
from app.services.feedback import FeedbackService


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
    serialized = routes._serialize_anchor(parsed)
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
    assert routes._serialize_anchor(parsed)["video_context"] == {"schema_version": 1}
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
    with pytest.raises(
        ValidationError, match="annotation_version requires annotation_id"
    ):
        AnnotationFeedbackCreate.model_validate(source)
    source = payload()
    source["anchor_type"] = "task"
    with pytest.raises(ValidationError, match="video_context requires a pixel anchor"):
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
    value = routes._serialize_anchor(AnnotationFeedbackCreate.model_validate(source))
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


def session_with_video(source):
    task = Task(
        id=source["task_id"],
        project_id=source["project_id"],
        dataset_item_id=uuid4(),
        file_type="video",
    )
    annotation = Annotation(
        id=source["annotation_id"],
        project_id=task.project_id,
        task_id=task.id,
        version=7,
        is_active=True,
        was_cancelled=False,
    )
    item = DatasetItem(
        id=task.dataset_item_id,
        file_type="video",
        metadata_={
            "video": {"frame_count": 180, "fps": 30, "width": 160, "height": 120}
        },
    )
    rows = {
        (Task, task.id): task,
        (Annotation, annotation.id): annotation,
        (DatasetItem, item.id): item,
    }
    db = SimpleNamespace(
        get=AsyncMock(side_effect=lambda model, key: rows.get((model, key))),
        add=Mock(),
        flush=AsyncMock(),
    )
    return db, rows, task, annotation, item


async def create(service, source, **overrides):
    return await service.create(
        **{
            **source,
            "author_id": uuid4(),
            "severity": "warn",
            "title": None,
            "attachments": [],
            "thread_parent_id": None,
            **overrides,
        }
    )


@pytest.mark.asyncio
async def test_service_persists_captured_version_and_fractional_window_without_current_version_cas():
    source = payload()
    db, _, _, annotation, _ = session_with_video(source)
    entry = await create(FeedbackService(db), source)
    assert annotation.version == 7
    assert entry.anchor_position["video_context"]["annotation_version"] == 2
    assert entry.anchor_position == source["anchor_position"]
    db.add.assert_called_once_with(entry)
    db.flush.assert_awaited_once()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("frame", 180),
        ("frame_range", {"from_frame": 160, "to_frame": 120}),
        ("frame_range", {"from_frame": 141, "to_frame": 160}),
        ("frame_range", {"from_frame": 0, "to_frame": 139}),
        ("frame_range", {"from_frame": 120, "to_frame": 180}),
        ("timeline_window", {"from": 170.5, "to": 110.25}),
        ("timeline_window", {"from": 0, "to": 179.01}),
        ("schema_version", 99),
    ],
)
@pytest.mark.asyncio
async def test_service_rejects_bad_media_ranges_before_insert(field, value):
    source = payload()
    if field == "frame":
        source["anchor_position"][field] = value
    else:
        source["anchor_position"]["video_context"][field] = value
    db, *_ = session_with_video(source)
    with pytest.raises(HTTPException) as error:
        await create(FeedbackService(db), source)
    assert error.value.status_code == 422
    db.add.assert_not_called()
    db.flush.assert_not_awaited()


@pytest.mark.parametrize(
    ("failure", "status"),
    [
        ("task_missing", 404),
        ("task_project", 422),
        ("image", 422),
        ("annotation_missing", 404),
        ("annotation_deleted", 404),
        ("annotation_cancelled", 404),
        ("annotation_task", 422),
        ("annotation_project", 422),
        ("metadata_missing", 503),
    ],
)
@pytest.mark.asyncio
async def test_service_checks_real_task_object_and_media_ownership(failure, status):
    source = payload()
    db, rows, task, annotation, item = session_with_video(source)
    if failure == "task_missing":
        rows.pop((Task, task.id))
    elif failure == "task_project":
        task.project_id = uuid4()
    elif failure == "image":
        task.file_type = "image"
    elif failure == "annotation_missing":
        rows.pop((Annotation, annotation.id))
    elif failure == "annotation_deleted":
        annotation.is_active = False
    elif failure == "annotation_cancelled":
        annotation.was_cancelled = True
    elif failure == "annotation_task":
        annotation.task_id = uuid4()
    elif failure == "annotation_project":
        annotation.project_id = uuid4()
    else:
        item.metadata_ = {}
    with pytest.raises(HTTPException) as error:
        await create(FeedbackService(db), source)
    assert error.value.status_code == status
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_reply_inherits_unknown_context_but_cannot_change_the_parent_anchor():
    source = payload()
    source["anchor_position"]["video_context"] = {
        "schema_version": 99,
        "future": [1, 2],
    }
    db, rows, _, annotation, _ = session_with_video(source)
    annotation.is_active = False
    parent = AnnotationFeedback(id=uuid4(), **deepcopy(source), is_active=True)
    rows[AnnotationFeedback, parent.id] = parent
    reply = await create(
        FeedbackService(db), source, kind="comment", thread_parent_id=parent.id
    )
    assert reply.anchor_position == parent.anchor_position
    source["anchor_position"]["x"] = 0.75
    with pytest.raises(HTTPException) as error:
        await create(
            FeedbackService(db), source, kind="comment", thread_parent_id=parent.id
        )
    assert error.value.status_code == 422
    assert db.add.call_count == 1


@pytest.mark.parametrize("with_context", [True, False])
@pytest.mark.asyncio
async def test_create_route_rejects_invisible_task_before_writing(
    monkeypatch, with_context
):
    source = payload()
    if not with_context:
        source["anchor_position"].pop("video_context")
    db, _, task, *_ = session_with_video(source)
    user = SimpleNamespace(id=uuid4())
    visible = AsyncMock(
        side_effect=HTTPException(status_code=404, detail="Task not found")
    )
    create_feedback = AsyncMock()
    monkeypatch.setattr(routes, "assert_project_visible", AsyncMock())
    monkeypatch.setattr(routes.task_access, "assert_task_visible", visible)
    monkeypatch.setattr(FeedbackService, "create", create_feedback)
    with pytest.raises(HTTPException) as error:
        await routes.create_feedback(
            AnnotationFeedbackCreate.model_validate(source), Mock(), db=db, user=user
        )
    assert error.value.status_code == 404
    visible.assert_awaited_once_with(db, task, user)
    create_feedback.assert_not_awaited()
    db.add.assert_not_called()
