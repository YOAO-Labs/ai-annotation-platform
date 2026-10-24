"""Exercise compact video-bbox writes through the function-only Labeling LOGIN."""

from __future__ import annotations

import os
import uuid

import asyncpg
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_labeling.video_bbox_editing_data import VideoBBoxEditingDataClient
from aap_labeling.video_bbox_editing_contract import VideoBBoxEditingRejected
from aap_labeling.video_bbox_editing_policy import (
    VideoBBoxAnnotationFacts,
    VideoBBoxTaskFacts,
    compose_video_tracks_command,
)
from aap_owner_data.postgres import verify_function_only_role
from app.db.models.annotation import Annotation
from tests.factory import create_project, create_task, create_user
from tests.test_labeling_class_usage_reads_legacy import _cleanup_owned
from tests.workspace_project_query_helpers import _dsn

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the verified Labeling video-bbox editing capability",
    ),
]

_FUNCTIONS = {
    ("annotation_bulk_command", "jsonb"),
    ("video_bbox_editing_command", "jsonb"),
}


def _facts(annotation: Annotation) -> VideoBBoxAnnotationFacts:
    return VideoBBoxAnnotationFacts(
        id=annotation.id,
        task_id=annotation.task_id,
        project_id=annotation.project_id,
        version=annotation.version,
        is_active=annotation.is_active,
        was_cancelled=annotation.was_cancelled,
        annotation_type=annotation.annotation_type,
        class_name=annotation.class_name,
        geometry=annotation.geometry,
        video_segment_id=annotation.video_segment_id,
        source=annotation.source,
        confidence=annotation.confidence,
        attributes=annotation.attributes,
        track_id=annotation.track_id,
    )


async def test_video_bbox_owner_is_transactional_and_fences_sources(test_engine):
    dsn = _dsn(
        test_engine,
        "AAP_MODULE_DATA_LABELING_EDITING_URL",
        "labeling_editing_client",
    )
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    actor_ids: list[uuid.UUID] = []
    project_ids: list[uuid.UUID] = []
    task_ids: list[uuid.UUID] = []
    connection = None
    try:
        async with maker.begin() as db:
            actor = await create_user(
                db,
                "project_admin",
                f"video-bbox-native-{uuid.uuid4().hex}@example.test",
                "Video bbox native owner",
            )
            actor_ids.append(actor.id)
            project = await create_project(db, owner_id=actor.id)
            project_ids.append(project.id)
            task = await create_task(db, project_id=project.id, status="in_progress")
            task_ids.append(task.id)
            sources = [
                Annotation(
                    task_id=task.id,
                    project_id=project.id,
                    user_id=actor.id,
                    source="manual",
                    annotation_type="video_bbox",
                    class_name="person",
                    geometry={
                        "type": "video_bbox",
                        "frame_index": frame,
                        "x": 0.1 * frame,
                        "y": 0.2,
                        "w": 0.3,
                        "h": 0.4,
                    },
                    attributes={"color": "red"},
                )
                for frame in (0, 3)
            ]
            db.add_all(sources)
            await db.flush()
            command = compose_video_tracks_command(
                task=VideoBBoxTaskFacts(
                    id=task.id,
                    project_id=project.id,
                    batch_id=task.batch_id,
                    status=task.status,
                    review_round_id=task.review_round_id,
                ),
                annotations=[_facts(source) for source in sources],
                actor_id=actor.id,
                operation="aggregate_bboxes",
                delete_sources=True,
            )

        connection = await asyncpg.connect(dsn, timeout=5, command_timeout=10)
        assert (
            await connection.fetchval("SELECT current_user")
            == "labeling_editing_client"
        )
        await verify_function_only_role(
            connection, schema="aap_labeling_v1", functions=_FUNCTIONS
        )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.fetch("SELECT id FROM public.annotations LIMIT 1")

        async def execute(request_json):
            return await connection.fetchval(
                "SELECT aap_labeling_v1.video_bbox_editing_command($1::jsonb)",
                request_json,
            )

        client = VideoBBoxEditingDataClient(execute)
        async with connection.transaction():
            rolled_back = await client.command(command)
            assert len(rolled_back.created_annotation_ids) == 1
            assert set(rolled_back.deleted_annotation_ids) == {
                source.id for source in sources
            }

        async with maker() as db:
            for source in sources:
                assert (await db.get(Annotation, source.id)).is_active is True
            assert await db.get(Annotation, command.created_annotation_id) is None

        async with connection.transaction():
            committed = await client.command(command)
            assert committed.created_annotation_ids == [command.created_annotation_id]

        async with maker() as db:
            for source in sources:
                persisted = await db.get(Annotation, source.id)
                assert persisted is not None and persisted.is_active is False
            created = await db.get(Annotation, command.created_annotation_id)
            assert created is not None
            assert created.annotation_type == "video_track_bbox"
            assert created.user_id == actor.id
            assert created.parent_annotation_id is None
            assert [
                keyframe["frame_index"] for keyframe in created.geometry["keyframes"]
            ] == [0, 3]

        async with connection.transaction():
            with pytest.raises(VideoBBoxEditingRejected) as rejected:
                await client.command(command)
            assert getattr(rejected.value, "status_code", None) == 409
    finally:
        if connection is not None:
            await connection.close()
        await _cleanup_owned(
            maker,
            actor_ids=actor_ids,
            project_ids=project_ids,
            task_ids=task_ids,
        )
