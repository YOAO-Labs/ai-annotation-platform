"""Exercise bulk Annotation edits through the function-only editing LOGIN."""

from __future__ import annotations

import os
import uuid

import asyncpg
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_labeling.annotation_bulk_contract import (
    ANNOTATION_BULK_PROTOCOL,
    AnnotationBulkPatch,
    UpdateAnnotations,
)
from aap_labeling.annotation_bulk_data import AnnotationBulkDataClient
from aap_owner_data.postgres import verify_function_only_role
from app.db.models.annotation import Annotation
from app.services.annotation import _sync_attributes_meta
from tests.factory import create_project, create_task, create_user
from tests.test_labeling_class_usage_reads_legacy import (
    _cleanup_owned,
    _create_annotation,
)
from tests.workspace_project_query_helpers import _dsn

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the disposable module-data boundary verifier",
    ),
]

_EDITING_FUNCTIONS = {
    ("annotation_bulk_command", "jsonb"),
    ("video_bbox_editing_command", "jsonb"),
}


async def test_function_only_bulk_edit_preserves_transaction_and_attribute_evidence(
    test_engine,
):
    dsn = _dsn(
        test_engine,
        "AAP_MODULE_DATA_LABELING_EDITING_URL",
        "labeling_editing_client",
    )
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    actor_ids, project_ids, task_ids = [], [], []
    connection = None
    annotation_id = uuid.uuid4()
    old_attributes = {
        "color": "blue",
        "flag": True,
        "nested": [True, {"enabled": False}],
    }
    old_metadata = {
        "color": {"origin": "ai", "model_ref": {"model_id": "test"}},
        "flag": {"origin": "ai", "model_ref": {"model_id": "test"}},
        "nested": {"origin": "ai", "model_ref": {"model_id": "test"}},
    }
    new_attributes = {
        "color": "green",
        "flag": 1,
        "nested": [1, {"enabled": 0}],
    }
    expected_metadata = _sync_attributes_meta(
        old_attributes, old_metadata, new_attributes
    )
    assert expected_metadata == {
        "flag": old_metadata["flag"],
        "nested": old_metadata["nested"],
    }
    try:
        async with maker.begin() as db:
            actor = await create_user(
                db,
                "project_admin",
                f"annotation-bulk-{uuid.uuid4().hex}@example.test",
                "Annotation bulk owner",
            )
            actor_ids.append(actor.id)
            project = await create_project(db, owner_id=actor.id)
            project_ids.append(project.id)
            task = await create_task(db, project_id=project.id)
            task_ids.append(task.id)
            annotation = await _create_annotation(
                db,
                project_id=project.id,
                task_id=task.id,
                user_id=actor.id,
                class_name="vehicle",
                attributes=old_attributes,
            )
            annotation_id = annotation.id
            annotation.attributes_meta = old_metadata
            await db.flush()

        connection = await asyncpg.connect(dsn, timeout=5, command_timeout=10)
        assert (
            await connection.fetchval("SELECT current_user")
            == "labeling_editing_client"
        )
        await verify_function_only_role(
            connection,
            schema="aap_labeling_v1",
            functions=_EDITING_FUNCTIONS,
        )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.fetch("SELECT id FROM public.annotations LIMIT 1")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.execute(
                "UPDATE public.annotations SET is_hidden = true WHERE id = $1",
                annotation_id,
            )

        async def execute(request_json):
            return await connection.fetchval(
                "SELECT aap_labeling_v1.annotation_bulk_command($1::jsonb)",
                request_json,
            )

        request = UpdateAnnotations(
            schema=ANNOTATION_BULK_PROTOCOL,
            operation="update",
            actor_id=actor.id,
            task_id=task.id,
            project_id=project.id,
            ids=[annotation_id],
            patch=AnnotationBulkPatch(attributes=new_attributes),
        )
        data = AnnotationBulkDataClient(execute)

        transaction = connection.transaction()
        await transaction.start()
        rolled_back = await data.command(request)
        assert rolled_back.annotations[0].version == 2
        assert rolled_back.annotations[0].attributes_meta == expected_metadata
        await transaction.rollback()
        async with maker() as db:
            unchanged = await db.get(Annotation, annotation_id)
            assert unchanged is not None
            assert unchanged.attributes == old_attributes
            assert unchanged.version == 1
            assert unchanged.attributes_meta == old_metadata

        async with connection.transaction():
            committed = await data.command(request)
            assert committed.annotations[0].version == 2
            assert committed.annotations[0].attributes_meta == expected_metadata

        async with maker() as db:
            updated = await db.scalar(
                select(Annotation).where(Annotation.id == annotation_id)
            )
            assert updated is not None
            assert updated.attributes == new_attributes
            assert updated.attributes_meta == expected_metadata
            assert updated.version == 2
    finally:
        if connection is not None:
            await connection.close()
        await _cleanup_owned(
            maker, actor_ids=actor_ids, project_ids=project_ids, task_ids=task_ids
        )
