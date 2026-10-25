"""Function-only Labeling role, caller transaction, and version-fence proof."""

from __future__ import annotations

import json
import os
import uuid

import asyncpg
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_labeling.cross_frame_write_contract import (
    CrossFrameSourceFence,
    CrossFrameTargetWrite,
    CrossFrameTaskFence,
    cross_frame_write_command,
    cross_frame_write_command_json,
)
from aap_labeling.cross_frame_write_data import CrossFrameWriteDataClient
from aap_owner_data.postgres import verify_function_only_role
from app.db.models.annotation import Annotation
from app.db.models.task import Task
from tests.test_annotation_cross_frame_write_fences import (
    _cleanup_world,
    _seed_world,
)
from tests.workspace_project_query_helpers import _dsn

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]

_FUNCTIONS = {("cross_frame_write_command", "jsonb")}


async def _request(test_engine, world, *, target_annotation_id: uuid.UUID):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as db:
        source = await db.get(Annotation, world["source_annotation_id"])
        source_task = await db.get(Task, world["task_ids"][0])
        target_task = await db.get(Task, world["task_ids"][2])
        assert (
            source is not None and source_task is not None and target_task is not None
        )
        project_id = world["project_id"]
        track_id = "trk_native_cross_frame"
        return cross_frame_write_command(
            "propagate",
            actor_id=world["actor_id"],
            task_fences=[
                CrossFrameTaskFence(
                    task_id=source_task.id,
                    project_id=project_id,
                    expected_version=source_task.version,
                ),
                CrossFrameTaskFence(
                    task_id=target_task.id,
                    project_id=project_id,
                    expected_version=target_task.version,
                ),
            ],
            source_annotations=[
                CrossFrameSourceFence(
                    annotation_id=source.id,
                    task_id=source.task_id,
                    project_id=source.project_id,
                    expected_version=source.version,
                    geometry_type=source.geometry["type"],
                    class_name=source.class_name,
                    expected_track_id=source.track_id,
                    target_track_id=track_id,
                    expected_scene_track_id=source.scene_track_id,
                    expected_temporal_role=source.temporal_role,
                )
            ],
            target_annotations=[
                CrossFrameTargetWrite(
                    annotation_id=target_annotation_id,
                    source_annotation_id=source.id,
                    task_id=target_task.id,
                    project_id=project_id,
                    video_segment_id=source.video_segment_id,
                    source="manual",
                    annotation_type=source.annotation_type,
                    tool_unit_id=source.tool_unit_id,
                    class_name=source.class_name,
                    geometry=source.geometry,
                    track_id=track_id,
                    attributes=source.attributes,
                    temporal_role="sample",
                )
            ],
            scene_tracks=[],
        )


async def test_function_only_cross_frame_owner_fences_versions_and_uses_outer_transaction(
    test_engine,
):
    world = await _seed_world(test_engine, operation="propagate")
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    connection = None
    target_annotation_id = uuid.uuid4()
    try:
        request = await _request(
            test_engine, world, target_annotation_id=target_annotation_id
        )
        dsn = _dsn(
            test_engine,
            "AAP_MODULE_DATA_LABELING_CROSS_FRAME_URL",
            "labeling_cross_frame_client",
        )
        connection = await asyncpg.connect(dsn, timeout=5, command_timeout=10)
        assert await connection.fetchval("SELECT current_user") == (
            "labeling_cross_frame_client"
        )
        await verify_function_only_role(
            connection,
            schema="aap_labeling_v1",
            functions=_FUNCTIONS,
            role="labeling_cross_frame_client",
        )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.fetchval("SELECT id FROM public.annotations LIMIT 1")

        async def call(request_json: str):
            return await connection.fetchval(
                "SELECT aap_labeling_v1.cross_frame_write_command($1::jsonb)",
                request_json,
            )

        client = CrossFrameWriteDataClient(call)
        transaction = connection.transaction()
        await transaction.start()
        staged = await client.command(request)
        assert staged.annotations[0].id == target_annotation_id
        assert staged.sources[0].version == 2
        assert staged.sources[0].track_id == "trk_native_cross_frame"
        await transaction.rollback()

        async with maker() as db:
            source = await db.get(Annotation, world["source_annotation_id"])
            assert (
                source is not None and source.version == 1 and source.track_id is None
            )
            assert await db.get(Annotation, target_annotation_id) is None

        transaction = connection.transaction()
        await transaction.start()
        await client.command(request)
        await transaction.commit()

        async with maker() as db:
            source = await db.get(Annotation, world["source_annotation_id"])
            created = await db.get(Annotation, target_annotation_id)
            assert source is not None and source.version == 2
            assert source.track_id == "trk_native_cross_frame"
            assert created is not None and created.task_id == world["task_ids"][2]

        stale = json.loads(cross_frame_write_command_json(request))
        stale["target_annotations"][0]["annotation_id"] = str(uuid.uuid4())
        async with connection.transaction():
            with pytest.raises(
                asyncpg.RaiseError, match="aap_labeling_cross_frame_write_error"
            ):
                await call(json.dumps(stale))

        async with maker() as db:
            source = await db.get(Annotation, world["source_annotation_id"])
            assert source is not None and source.version == 2
            assert (
                await db.scalar(
                    select(Annotation.id).where(
                        Annotation.task_id == world["task_ids"][2],
                        Annotation.id != target_annotation_id,
                    )
                )
                is None
            )
    finally:
        if connection is not None:
            await connection.close()
        await _cleanup_world(test_engine, world)
