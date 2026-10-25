"""Native outer-transaction proof for the independent Labeling Annotation owner."""

import os
import uuid

import asyncpg
import pytest
from sqlalchemy import delete, select

from aap_labeling.annotation_write_contract import (
    ANNOTATION_WRITE_PROTOCOL,
    CreateAnnotation,
    DeleteAnnotation,
    ReadCreateReceipt,
    UpdateAnnotation,
    WriteCreateReceipt,
)
from aap_labeling.annotation_write_data import AnnotationWriteDataClient
from app.db.models.annotation import Annotation
from app.db.models.annotation_operation import AnnotationOperation
from app.db.models.task import Task
from tests.factory import create_task
from tests.test_workspace_project_access_step_native import _cleanup, _seed
from tests.workspace_project_query_helpers import _dsn

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned Labeling Annotation write capability",
    ),
]


def _create(*, annotation_id, task_id, project_id, actor_id):
    return CreateAnnotation(
        schema=ANNOTATION_WRITE_PROTOCOL,
        operation="create",
        annotation_id=annotation_id,
        task_id=task_id,
        project_id=project_id,
        actor_id=actor_id,
        annotation_type="bbox",
        tool_unit_id="bbox",
        class_name="person",
        geometry={"type": "bbox", "x": 1, "y": 2, "width": 3, "height": 4},
        confidence=None,
        parent_prediction_id=None,
        parent_annotation_id=None,
        video_segment_id=None,
        lead_time=None,
        attributes={},
        track_id=None,
    )


async def test_annotation_writes_and_create_receipts_follow_the_callers_transaction(
    test_engine,
):
    maker, world = await _seed(test_engine)
    task_id = None
    connection = None
    rolled_back_id = uuid.uuid4()
    committed_id = uuid.uuid4()
    try:
        async with maker.begin() as db:
            task = await create_task(
                db, project_id=world["project_id"], status="in_progress"
            )
            task_id = task.id

        dsn = _dsn(
            test_engine,
            "AAP_MODULE_DATA_LABELING_ANNOTATION_WRITE_URL",
            "labeling_annotation_write_client",
        )
        connection = await asyncpg.connect(dsn, timeout=5, command_timeout=10)
        assert await connection.fetchval("SELECT current_user") == (
            "labeling_annotation_write_client"
        )

        async def call(request_json):
            return await connection.fetchval(
                "SELECT aap_labeling_v1.annotation_write_command($1::jsonb)",
                request_json,
            )

        data = AnnotationWriteDataClient(call)
        transaction = connection.transaction()
        await transaction.start()
        await data.command(
            _create(
                annotation_id=rolled_back_id,
                task_id=task_id,
                project_id=world["project_id"],
                actor_id=world["member_id"],
            )
        )
        update = await data.command(
            UpdateAnnotation(
                schema=ANNOTATION_WRITE_PROTOCOL,
                operation="update",
                annotation_id=rolled_back_id,
                task_id=task_id,
                project_id=world["project_id"],
                actor_id=world["member_id"],
                expected_version=1,
                geometry={"type": "bbox", "x": 2, "y": 2, "width": 3, "height": 4},
                track_id=None,
            )
        )
        assert update.mutation.version == 2
        deleted = await data.command(
            DeleteAnnotation(
                schema=ANNOTATION_WRITE_PROTOCOL,
                operation="delete",
                annotation_id=rolled_back_id,
                task_id=task_id,
                project_id=world["project_id"],
                actor_id=world["member_id"],
                expected_version=2,
            )
        )
        assert deleted.mutation.changed is True
        await transaction.rollback()

        async with maker() as db:
            assert await db.get(Annotation, rolled_back_id) is None

        transaction = connection.transaction()
        await transaction.start()
        await data.command(
            _create(
                annotation_id=committed_id,
                task_id=task_id,
                project_id=world["project_id"],
                actor_id=world["member_id"],
            )
        )
        response_json = {"annotation_id": str(committed_id), "status": "created"}
        await data.command(
            WriteCreateReceipt(
                schema=ANNOTATION_WRITE_PROTOCOL,
                operation="write_create_receipt",
                operation_id=uuid.uuid4(),
                task_id=task_id,
                project_id=world["project_id"],
                actor_id=world["member_id"],
                idempotency_key="annotation-owner-native",
                request_digest="a" * 64,
                response_json=response_json,
            )
        )
        receipt = await data.command(
            ReadCreateReceipt(
                schema=ANNOTATION_WRITE_PROTOCOL,
                operation="read_create_receipt",
                task_id=task_id,
                project_id=world["project_id"],
                actor_id=world["member_id"],
                idempotency_key="annotation-owner-native",
                request_digest="a" * 64,
            )
        )
        assert receipt.receipt.found is True
        assert receipt.receipt.response_json == response_json
        await transaction.commit()

        async with maker() as db:
            annotation = await db.get(Annotation, committed_id)
            assert annotation is not None and annotation.version == 1
            operation = await db.scalar(
                select(AnnotationOperation).where(
                    AnnotationOperation.task_id == task_id,
                    AnnotationOperation.actor_id == world["member_id"],
                    AnnotationOperation.idempotency_key == "annotation-owner-native",
                )
            )
            assert operation is not None
            assert operation.response_json == response_json
    finally:
        if connection is not None:
            await connection.close()
        async with maker.begin() as db:
            if task_id is not None:
                await db.execute(
                    delete(Annotation).where(Annotation.task_id == task_id)
                )
                await db.execute(delete(Task).where(Task.id == task_id))
        await _cleanup(maker, world)
