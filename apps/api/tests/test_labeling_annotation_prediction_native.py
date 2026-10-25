"""Saved owned-role proof for prediction Annotation writes; run in module acceptance."""

import os
import uuid

import asyncpg
import pytest
from sqlalchemy import delete

from aap_labeling.annotation_prediction_contract import (
    ANNOTATION_PREDICTION_PROTOCOL,
    ApplySecondaryAttributes,
    CreateAcceptedAnnotation,
    MarkSecondaryChild,
)
from aap_labeling.annotation_prediction_data import AnnotationPredictionDataClient
from app.db.models.annotation import Annotation
from app.db.models.prediction import Prediction
from app.db.models.task import Task
from tests.factory import create_task
from tests.test_workspace_project_access_step_native import _cleanup, _seed
from tests.workspace_project_query_helpers import _dsn

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable Labeling prediction capabilities",
    ),
]


async def test_annotation_prediction_owner_keeps_provenance_and_outer_rollback(
    test_engine,
):
    maker, world = await _seed(test_engine)
    task_id = None
    connection = None
    transaction = None
    created_id = uuid.uuid4()
    try:
        async with maker.begin() as db:
            task = await create_task(
                db, project_id=world["project_id"], status="in_progress"
            )
            task_id = task.id
            prediction = Prediction(
                task_id=task.id,
                project_id=task.project_id,
                result=[],
                tool_unit_id="bbox",
            )
            parent = Annotation(
                task_id=task.id,
                project_id=task.project_id,
                user_id=world["member_id"],
                class_name="car",
                geometry={"type": "bbox"},
                attributes={"human": "kept"},
            )
            db.add_all([prediction, parent])
            await db.flush()
            child = Annotation(
                task_id=task.id,
                project_id=task.project_id,
                user_id=world["member_id"],
                class_name="car",
                geometry={"type": "bbox"},
                parent_annotation_id=parent.id,
            )
            db.add(child)
            await db.flush()
            parent_id, child_id = parent.id, child.id
            context = {
                "schema": ANNOTATION_PREDICTION_PROTOCOL,
                "task_id": task.id,
                "project_id": task.project_id,
                "actor_id": world["member_id"],
                "expected_batch_id": task.batch_id,
                "expected_status": task.status,
                "expected_review_round_id": task.review_round_id,
            }
            create = CreateAcceptedAnnotation(
                **context,
                operation="create_accepted",
                annotation_id=created_id,
                prediction_id=prediction.id,
                prediction_created_at=prediction.created_at,
                annotation_type="bbox",
                tool_unit_id="bbox",
                class_name="car",
                geometry={"type": "bbox"},
                confidence=None,
                video_segment_id=None,
                attributes={"color": "red"},
                attributes_meta={"color": {"origin": "ai"}},
                track_id=None,
                temporal_role="sample",
            )

        connection = await asyncpg.connect(
            _dsn(
                test_engine,
                "AAP_MODULE_DATA_LABELING_PREDICTION_URL",
                "labeling_prediction_client",
            ),
            timeout=5,
            command_timeout=10,
        )
        assert (
            await connection.fetchval("SELECT current_user")
            == "labeling_prediction_client"
        )

        async def execute(payload):
            return await connection.fetchval(
                "SELECT aap_labeling_v1.annotation_prediction_command($1::jsonb)",
                payload,
            )

        data = AnnotationPredictionDataClient(execute)
        transaction = connection.transaction()
        await transaction.start()
        assert (await data.command(create)).version == 1
        attributes = ApplySecondaryAttributes(
            **context,
            operation="apply_secondary_attributes",
            annotation_id=parent_id,
            expected_version=1,
            attributes={"human": "kept", "color": "red"},
            attributes_meta={"color": {"origin": "ai"}},
        )
        assert (await data.command(attributes)).version == 2
        child_result = await data.command(
            MarkSecondaryChild(
                **context,
                operation="mark_secondary_child",
                annotation_id=child_id,
                parent_annotation_id=parent_id,
                expected_version=1,
            )
        )
        assert (child_result.source, child_result.version) == ("prediction_based", 1)
        with pytest.raises(asyncpg.PostgresError) as stale:
            async with connection.transaction():
                await data.command(attributes)
        assert stale.value.sqlstate == "P0001"
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with connection.transaction():
                await connection.fetch("SELECT id FROM public.annotations LIMIT 1")
        await transaction.rollback()
        transaction = None

        async with maker() as db:
            assert await db.get(Annotation, created_id) is None
            parent = await db.get(Annotation, parent_id)
            child = await db.get(Annotation, child_id)
            assert (parent.attributes, parent.version) == ({"human": "kept"}, 1)
            assert (child.source, child.version) == ("manual", 1)
    finally:
        if transaction is not None:
            await transaction.rollback()
        if connection is not None:
            await connection.close()
        if task_id is not None:
            async with maker.begin() as db:
                await db.execute(
                    delete(Annotation).where(Annotation.task_id == task_id)
                )
                await db.execute(
                    delete(Prediction).where(Prediction.task_id == task_id)
                )
                await db.execute(delete(Task).where(Task.id == task_id))
        await _cleanup(maker, world)
