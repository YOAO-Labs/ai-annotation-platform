"""Exercise Labeling scene reads through its function-only LOGIN."""

from __future__ import annotations

import json
import os
import uuid

import asyncpg
import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_labeling.scene_read_contract import scene_read_request
from aap_labeling.scene_read_data import SceneReadDataClient
from aap_owner_data.postgres import verify_function_only_role
from app.db.models.annotation import Annotation
from app.db.models.dataset import Dataset, DatasetItem, Scene
from app.db.models.project import Project
from app.db.models.scene_track import SceneTrack
from app.db.models.task import Task
from app.db.models.user import User
from tests.factory import create_user
from tests.test_track_operations import _add_box, _seed_scene
from tests.workspace_project_query_helpers import _dsn

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the verified Labeling scene read role",
    ),
]

_ROLE = "labeling_scene_read_client"


async def test_scene_reads_use_the_single_function_only_owner_capability(test_engine):
    dsn = _dsn(test_engine, "AAP_MODULE_DATA_LABELING_SCENE_READ_URL", _ROLE)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    actor_ids: list[uuid.UUID] = []
    project_ids: list[uuid.UUID] = []
    dataset_ids: list[uuid.UUID] = []
    scene_ids: list[uuid.UUID] = []
    task_ids: list[uuid.UUID] = []
    annotation_ids: list[uuid.UUID] = []
    track_ids: list[uuid.UUID] = []
    connection = None
    try:
        async with maker.begin() as db:
            actor = await create_user(
                db,
                "project_admin",
                f"scene-read-native-{uuid.uuid4().hex}@example.test",
                "Labeling scene read native owner",
            )
            actor_ids.append(actor.id)
            project, scene, tasks = await _seed_scene(
                db, owner_id=actor.id, frame_count=3
            )
            project_ids.append(project.id)
            dataset_ids.append(scene.dataset_id)
            scene_ids.append(scene.id)
            task_ids.extend(task.id for task in tasks.values())
            track_id = f"trk-scene-read-native-{uuid.uuid4().hex}"
            annotations = [
                await _add_box(
                    db,
                    task=tasks[frame],
                    project=project,
                    user_id=actor.id,
                    track_id=track_id,
                )
                for frame in range(3)
            ]
            annotation_ids.extend(row.id for row in annotations)
            track = (
                await db.execute(
                    select(SceneTrack).where(SceneTrack.track_id == track_id)
                )
            ).scalar_one()
            track_ids.append(track.id)

        connection = await asyncpg.connect(dsn, timeout=5, command_timeout=10)
        assert await connection.fetchval("SELECT current_user") == _ROLE
        await verify_function_only_role(
            connection,
            schema="aap_labeling_v1",
            functions={("scene_read_query", "jsonb")},
            role=_ROLE,
        )
        for malformed in (
            {"schema": "aap-labeling-scene-read/v1", "action": "resolve_task_frames"},
            {
                "schema": "aap-labeling-scene-read/v1",
                "action": "resolve_task_frames",
                "task_ids": [],
                "extra": True,
            },
        ):
            with pytest.raises(asyncpg.PostgresError) as invalid_request:
                await connection.fetchval(
                    "SELECT aap_labeling_v1.scene_read_query($1::jsonb)",
                    json.dumps(malformed),
                )
            assert invalid_request.value.sqlstate == "22023"
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.fetch("SELECT id FROM public.annotations LIMIT 1")

        async def execute(request_json: str):
            return await connection.fetchval(
                "SELECT aap_labeling_v1.scene_read_query($1::jsonb)",
                request_json,
            )

        client = SceneReadDataClient(execute)
        scene_frames = await client.query(
            scene_read_request("scene_frame_tasks", scene_id=scene_ids[0])
        )
        assert [row.frame_index for row in scene_frames.frame_tasks] == [0, 1, 2]
        assert [row.task_id for row in scene_frames.frame_tasks] == [
            tasks[index].id for index in range(3)
        ]

        detail = await client.query(
            scene_read_request(
                "load_track_detail",
                project_id=project_ids[0],
                scene_id=scene_ids[0],
                track_id=track_id,
            )
        )
        assert detail.track is not None
        assert [row.id for row in detail.members] == [
            row.id for row in sorted(annotations, key=lambda row: (row.task_id, row.id))
        ]

        timeline = await client.query(
            scene_read_request(
                "timeline_annotation_summaries",
                task_ids=task_ids,
                track_id=track_id,
            )
        )
        assert [row.annotation_count for row in timeline.annotation_summaries] == [
            1,
            1,
            1,
        ]

        transaction = connection.transaction()
        await transaction.start()
        locked = await client.query(
            scene_read_request(
                "lock_track_members",
                project_id=project_ids[0],
                track_ids=[track_id],
                limit=5001,
            )
        )
        assert [row.id for row in locked.members] == sorted(annotation_ids)
        await transaction.rollback()
    finally:
        if connection is not None:
            await connection.close()
        async with maker.begin() as db:
            if annotation_ids:
                await db.execute(
                    delete(Annotation).where(Annotation.id.in_(annotation_ids))
                )
            if task_ids:
                await db.execute(delete(Task).where(Task.id.in_(task_ids)))
            if track_ids:
                await db.execute(delete(SceneTrack).where(SceneTrack.id.in_(track_ids)))
            if scene_ids:
                await db.execute(delete(Scene).where(Scene.id.in_(scene_ids)))
            if dataset_ids:
                await db.execute(
                    delete(DatasetItem).where(DatasetItem.dataset_id.in_(dataset_ids))
                )
                await db.execute(delete(Dataset).where(Dataset.id.in_(dataset_ids)))
            if project_ids:
                await db.execute(delete(Project).where(Project.id.in_(project_ids)))
            if actor_ids:
                await db.execute(delete(User).where(User.id.in_(actor_ids)))
