"""Task metric SQL expression builders (primitive layer).

The ``pending_*`` expressions used by task view filter/sort compilation. Pure SQL
builders with no dependency on views, service or filters, so ``views`` can import them
without forming a cycle. Extracted from the legacy ``data_manager.py``.
"""

from __future__ import annotations

from typing import Any, Callable

from sqlalchemy import (
    Float,
    Integer,
    and_,
    case,
    cast,
    func,
    literal,
    not_,
    select,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import aliased

from app.db.models.annotation import Annotation
from app.db.models.prediction import (
    INTERACTIVE_ACCEPT_PREDICTION_SOURCE,
    Prediction,
)
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.user import User
from app.db.models.video_tracker_job import VideoTrackerJob, VideoTrackerJobStatus
from app.services.project_access import is_privileged_for_project

LOW_CONFIDENCE_THRESHOLD = 0.5


def pending_prediction_shapes_expr():
    pending = _pending_prediction_shape_rows(
        lambda prediction: prediction.task_id == Task.id,
        alias_name="dm_task_pending_count",
    ).correlate(Task)
    return (
        select(func.count())
        .select_from(pending.subquery("dm_task_pending_count_rows"))
        .correlate(Task)
        .scalar_subquery()
    )


def _pending_prediction_shape_rows(
    task_clause: Callable[[Any], Any],
    *,
    alias_name: str,
):
    prediction = aliased(Prediction)
    guarded_result = case(
        (func.jsonb_typeof(prediction.result) == "array", prediction.result),
        else_=literal([], type_=JSONB),
    )
    shape = (
        func.jsonb_array_elements(guarded_result)
        .table_valued("value", with_ordinality="ordinality")
        .lateral(f"{alias_name}_shape")
    )
    shape_value = cast(shape.c.value, JSONB)
    shape_index = cast(shape.c.ordinality, Integer) - 1
    raw_confidence = func.coalesce(
        shape_value["score"].astext,
        shape_value["confidence"].astext,
        "0",
    )
    numeric_confidence = raw_confidence.op("~")(
        r"^[+-]?([0-9]+([.][0-9]*)?|[.][0-9]+)([eE][+-]?[0-9]+)?$"
    )
    confidence = case(
        (numeric_confidence, cast(raw_confidence, Float)),
        else_=0.0,
    )
    rejected = case(
        (
            func.jsonb_typeof(prediction.rejected_shape_indexes) == "array",
            prediction.rejected_shape_indexes,
        ),
        else_=literal([], type_=JSONB),
    )
    accepted = (
        select(Annotation.id)
        .where(
            Annotation.parent_prediction_id == prediction.id,
            Annotation.is_active.is_(True),
            Annotation.was_cancelled.is_(False),
            Annotation.attributes.has_key("_shape_index"),  # noqa: W601
            cast(Annotation.attributes["_shape_index"].astext, Integer) == shape_index,
        )
        .correlate(prediction, shape)
        .exists()
    )
    return (
        select(
            prediction.task_id.label("task_id"),
            prediction.id.label("prediction_id"),
            prediction.created_at.label("prediction_created_at"),
            prediction.model_version.label("model_version"),
            prediction.tool_unit_id.label("tool_unit_id"),
            shape_index.label("shape_index"),
            confidence.label("confidence"),
            shape_value.label("shape_value"),
        )
        .select_from(prediction)
        .join(shape, true())
        .where(
            task_clause(prediction),
            prediction.source != INTERACTIVE_ACCEPT_PREDICTION_SOURCE,
            not_(rejected.op("@>")(func.jsonb_build_array(shape_index))),
            not_(accepted),
        )
    )


def low_confidence_pending_prediction_shapes_expr():
    pending = _pending_prediction_shape_rows(
        lambda prediction: prediction.task_id == Task.id,
        alias_name="dm_task_pending",
    ).correlate(Task)
    pending_rows = pending.subquery("dm_task_pending_rows")
    return (
        select(func.count())
        .select_from(pending_rows)
        .where(pending_rows.c.confidence < LOW_CONFIDENCE_THRESHOLD)
        .correlate(Task)
        .scalar_subquery()
    )


def pending_tracker_jobs_expr(user: User | None, project: Project):
    results = VideoTrackerJob.staged_result["results"]
    reviewable = and_(
        VideoTrackerJob.task_id == Task.id,
        VideoTrackerJob.status.in_(
            [
                VideoTrackerJobStatus.PENDING_REVIEW.value,
                VideoTrackerJobStatus.PARTIALLY_REVIEWED.value,
                VideoTrackerJobStatus.CANCELLED.value,
            ]
        ),
        VideoTrackerJob.staged_result.is_not(None),
        case(
            (func.jsonb_typeof(results) == "array", func.jsonb_array_length(results)),
            else_=0,
        )
        > 0,
    )
    if user is not None and not is_privileged_for_project(user, project):
        reviewable = and_(reviewable, VideoTrackerJob.created_by == user.id)
    return (
        select(func.count(VideoTrackerJob.id))
        .where(reviewable)
        .correlate(Task)
        .scalar_subquery()
    )
