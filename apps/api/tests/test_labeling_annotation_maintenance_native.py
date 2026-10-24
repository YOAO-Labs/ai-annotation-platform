"""Saved function-only draft/cleanup proof for the owned module database."""

import json
import os
import uuid

import asyncpg
import pytest
from sqlalchemy import delete

from aap_labeling.annotation_maintenance_contract import (
    ANNOTATION_MAINTENANCE_PROTOCOL,
    CleanupAnnotationReference,
    CleanupSchema,
    ObserveCleanupTasks,
    ReadCleanupCandidates,
    ReadDraft,
    SaveDraft,
)
from aap_labeling.annotation_maintenance_data import AnnotationMaintenanceDataClient
from app.db.models.annotation import Annotation
from app.db.models.task import Task
from app.db.models.task_lock import AnnotationDraft
from tests.factory import create_task
from tests.test_workspace_project_access_step_native import _cleanup, _seed
from tests.workspace_project_query_helpers import _dsn

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the verified disposable Labeling maintenance role",
    ),
]


async def test_maintenance_owner_keeps_cleanup_json_and_outer_rollback(test_engine):
    maker, world = await _seed(test_engine)
    task_id = None
    connection = transaction = None
    try:
        async with maker.begin() as db:
            task = await create_task(
                db, project_id=world["project_id"], status="in_progress"
            )
            task_id = task.id
            orphan = Annotation(
                task_id=task_id,
                project_id=task.project_id,
                user_id=world["member_id"],
                class_name="old",
                geometry={"type": "bbox"},
                attributes={"keep": True, "legacy": 0},
            )
            retained = Annotation(
                task_id=task_id,
                project_id=task.project_id,
                user_id=world["member_id"],
                class_name="car",
                geometry={"type": "bbox"},
                attributes={"keep": True, "legacy": 0, "_system": {"id": 1}},
            )
            db.add_all([orphan, retained])
            await db.flush()
            orphan_id, retained_id = orphan.id, retained.id
        connection = await asyncpg.connect(
            _dsn(
                test_engine,
                "AAP_MODULE_DATA_LABELING_ANNOTATION_MAINTENANCE_URL",
                "labeling_annotation_maintenance_client",
            ),
            timeout=5,
            command_timeout=10,
        )
        assert (
            await connection.fetchval("SELECT current_user")
            == "labeling_annotation_maintenance_client"
        )

        async def execute(payload):
            request = json.loads(payload)
            if request["operation"] == "cleanup_schema":
                # Native clients may send valid uppercase UUIDs without Python normalization.
                request["project_id"] = request["project_id"].upper()
                for row in request["annotations"] + request["tasks"]:
                    for field in ("id", "task_id", "project_id", "batch_id"):
                        if isinstance(row.get(field), str):
                            row[field] = row[field].upper()
                payload = json.dumps(request)
            return await connection.fetchval(
                "SELECT aap_labeling_v1.annotation_maintenance_command($1::jsonb)",
                payload,
            )

        client = AnnotationMaintenanceDataClient(execute)
        transaction = connection.transaction()
        await transaction.start()
        scope = {"schema": ANNOTATION_MAINTENANCE_PROTOCOL}
        draft_id = uuid.uuid4()
        await client.command(
            SaveDraft(
                **scope,
                operation="save_draft",
                task_id=task_id,
                user_id=world["member_id"],
                draft_id=draft_id,
                existing=False,
                write_result=True,
                result={"nested": [True, 1, None]},
            )
        )
        draft = await client.command(
            ReadDraft(
                **scope,
                operation="read_draft",
                task_id=task_id,
                user_id=world["member_id"],
            )
        )
        assert draft.draft.result == {"nested": [True, 1, None]}
        candidates = await client.command(
            ReadCleanupCandidates(
                **scope,
                operation="read_cleanup_candidates",
                project_id=world["project_id"],
            )
        )
        candidates = [row for row in candidates.annotations if row.task_id == task_id]
        observed = await client.command(
            ObserveCleanupTasks(
                **scope, operation="observe_cleanup_tasks", task_ids=[task_id]
            )
        )
        result = await client.command(
            CleanupSchema(
                **scope,
                operation="cleanup_schema",
                project_id=world["project_id"],
                class_names=["car"],
                attribute_keys=["keep"],
                annotations=[
                    CleanupAnnotationReference(id=row.id, task_id=row.task_id)
                    for row in candidates
                ],
                tasks=observed.tasks,
            )
        )
        assert result.report.model_dump() == {
            "orphan_annotations": 1,
            "orphan_attribute_keys": {"legacy": 1},
        }
        states = {row.id: row for row in result.annotations}
        assert not states[orphan_id].is_active
        assert states[retained_id].attributes == {"keep": True, "_system": {"id": 1}}
        assert states[retained_id].version == 1
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with connection.transaction():
                await connection.fetch("SELECT id FROM public.annotations LIMIT 1")
        await transaction.rollback()
        transaction = None
        async with maker() as db:
            assert await db.get(AnnotationDraft, draft_id) is None
            assert (await db.get(Annotation, orphan_id)).is_active
            assert (await db.get(Annotation, retained_id)).attributes == {
                "keep": True,
                "legacy": 0,
                "_system": {"id": 1},
            }
    finally:
        if transaction is not None:
            await transaction.rollback()
        if connection is not None:
            await connection.close()
        if task_id is not None:
            async with maker.begin() as db:
                await db.execute(
                    delete(AnnotationDraft).where(AnnotationDraft.task_id == task_id)
                )
                await db.execute(
                    delete(Annotation).where(Annotation.task_id == task_id)
                )
                await db.execute(delete(Task).where(Task.id == task_id))
        await _cleanup(maker, world)
