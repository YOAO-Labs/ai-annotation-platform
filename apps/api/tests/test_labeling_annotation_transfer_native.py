"""Saved function-only proof for import owner writes and transaction rollback."""

from __future__ import annotations

import os
import uuid

import asyncpg
import pytest
from sqlalchemy import delete

from aap_labeling.annotation_transfer_contract import (
    ANNOTATION_TRANSFER_PROTOCOL,
    CreateImportedAnnotation,
    PurgeImportedAnnotations,
    ResolveTask,
)
from aap_labeling.annotation_transfer_data import AnnotationTransferDataClient
from aap_owner_data.postgres import verify_function_only_role
from app.db.models.annotation import Annotation
from app.db.models.task import Task
from tests.factory import create_task
from tests.test_workspace_project_access_step_native import _cleanup, _seed
from tests.workspace_project_query_helpers import _dsn

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the verified disposable Labeling annotation transfer role",
    ),
]

_ROLE = "labeling_annotation_transfer_client"


async def test_import_owner_preserves_scope_partial_writes_and_rollback(test_engine):
    maker, world = await _seed(test_engine)
    task_id = None
    imported_id = uuid.uuid4()
    manual_id = uuid.uuid4()
    connection = None
    try:
        async with maker.begin() as db:
            task = await create_task(db, project_id=world["project_id"])
            task_id = task.id
            db.add(
                Annotation(
                    id=manual_id,
                    task_id=task.id,
                    project_id=task.project_id,
                    user_id=world["member_id"],
                    class_name="manual",
                    geometry={"type": "bbox", "x": 0.1},
                    attributes={},
                )
            )

        connection = await asyncpg.connect(
            _dsn(
                test_engine,
                "AAP_MODULE_DATA_LABELING_ANNOTATION_TRANSFER_URL",
                _ROLE,
            ),
            timeout=5,
            command_timeout=10,
        )
        assert await connection.fetchval("SELECT current_user") == _ROLE
        await verify_function_only_role(
            connection,
            schema="aap_labeling_v1",
            functions={("annotation_transfer_command", "jsonb")},
            role=_ROLE,
        )

        async def execute(payload: str):
            return await connection.fetchval(
                "SELECT aap_labeling_v1.annotation_transfer_command($1::jsonb)",
                payload,
            )

        client = AnnotationTransferDataClient(execute)
        request_scope = {"schema": ANNOTATION_TRANSFER_PROTOCOL}
        transaction = connection.transaction()
        await transaction.start()
        match = await client.command(
            ResolveTask(
                **request_scope,
                operation="resolve_task",
                project_id=world["project_id"],
                display_id=None,
                file_path="e2e/unmatched.jpg",
            )
        )
        assert match.task is None
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with connection.transaction():
                await connection.fetchval("SELECT id FROM public.annotations LIMIT 1")
        await client.command(
            CreateImportedAnnotation(
                **request_scope,
                operation="create_imported_annotation",
                annotation_id=imported_id,
                task_id=task_id,
                project_id=world["project_id"],
                actor_id=world["member_id"],
                source="manual",
                annotation_type="bbox",
                tool_unit_id="bbox",
                class_name="imported",
                geometry={"type": "bbox", "x": 0.2},
                track_id=None,
                confidence=None,
                attributes={"_imported": True},
                temporal_role="sample",
                created_at=None,
                scene_track_id=None,
                sensor_dataset_item_id=None,
                sensor_role=None,
                sensor_visibility=None,
                calibration_revision=None,
                calibration_digest=None,
                scene_id=None,
                frame_index=None,
            )
        )
        await transaction.rollback()

        async with maker() as db:
            assert await db.get(Annotation, imported_id) is None

        transaction = connection.transaction()
        await transaction.start()
        created = await client.command(
            CreateImportedAnnotation(
                **request_scope,
                operation="create_imported_annotation",
                annotation_id=imported_id,
                task_id=task_id,
                project_id=world["project_id"],
                actor_id=world["member_id"],
                source="manual",
                annotation_type="bbox",
                tool_unit_id="bbox",
                class_name="imported",
                geometry={"type": "bbox", "x": 0.2},
                track_id=None,
                confidence=None,
                attributes={"_imported": True},
                temporal_role="sample",
                created_at=None,
                scene_track_id=None,
                sensor_dataset_item_id=None,
                sensor_role=None,
                sensor_visibility=None,
                calibration_revision=None,
                calibration_digest=None,
                scene_id=None,
                frame_index=None,
            )
        )
        assert created.imported_annotation.annotation_id == imported_id
        await transaction.commit()

        async with maker() as db:
            imported = await db.get(Annotation, imported_id)
            assert imported is not None
            assert imported.user_id == world["member_id"]
            assert imported.attributes == {"_imported": True}

        transaction = connection.transaction()
        await transaction.start()
        purged = await client.command(
            PurgeImportedAnnotations(
                **request_scope,
                operation="purge_imported_annotations",
                task_id=task_id,
            )
        )
        assert purged.purged_count == 1
        await transaction.commit()

        async with maker() as db:
            assert await db.get(Annotation, imported_id) is None
            assert (await db.get(Annotation, manual_id)).is_active
    finally:
        if connection is not None:
            await connection.close()
        if task_id is not None:
            async with maker.begin() as db:
                await db.execute(
                    delete(Annotation).where(Annotation.task_id == task_id)
                )
                await db.execute(delete(Task).where(Task.id == task_id))
        await _cleanup(maker, world)
