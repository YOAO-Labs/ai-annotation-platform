"""Exercise SceneTrack command apply, replay and revert through its function-only LOGIN."""

from __future__ import annotations

import os
import uuid

import asyncpg
import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_labeling.scene_track_command_contract import scene_track_command
from aap_labeling.scene_track_command_data import SceneTrackCommandDataClient
from aap_owner_data.postgres import verify_function_only_role
from app.db.models.annotation import Annotation
from app.db.models.dataset import Dataset, DatasetItem, Scene
from app.db.models.project import Project
from app.db.models.scene_track import (
    SceneTrack,
    SceneTrackInterval,
    SceneTrackOperation,
)
from app.db.models.task import Task
from app.db.models.user import User
from app.services.scene_track_command import (
    _apply_native_request,
    prepare_scene_track_command,
    request_digest,
)
from app.schemas.scene_track import SceneTrackCommandRequest
from tests.factory import create_user
from tests.test_track_operations import _add_box, _seed_scene
from tests.workspace_project_query_helpers import _dsn

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the verified Labeling SceneTrack command role",
    ),
]

_ROLE = "labeling_scene_track_command_client"


async def test_split_transaction_replay_and_revert_use_the_single_owner_function(
    test_engine,
):
    dsn = _dsn(
        test_engine,
        "AAP_MODULE_DATA_LABELING_SCENE_TRACK_COMMAND_URL",
        _ROLE,
    )
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
                f"scene-command-native-{uuid.uuid4().hex}@example.test",
                "SceneTrack command native owner",
            )
            actor_ids.append(actor.id)
            project, scene, tasks = await _seed_scene(
                db, owner_id=actor.id, frame_count=3
            )
            project_ids.append(project.id)
            dataset_ids.append(scene.dataset_id)
            scene_ids.append(scene.id)
            task_ids.extend(task.id for task in tasks.values())
            track_id = f"trk-scene-command-native-{uuid.uuid4().hex}"
            rows = [
                await _add_box(
                    db,
                    task=tasks[frame],
                    project=project,
                    user_id=actor.id,
                    track_id=track_id,
                )
                for frame in range(3)
            ]
            annotation_ids.extend(row.id for row in rows)
            track = (
                await db.execute(
                    select(SceneTrack).where(SceneTrack.track_id == track_id)
                )
            ).scalar_one()
            track_ids.append(track.id)
            command = SceneTrackCommandRequest(
                kind="split", track_id=track_id, frame_index=1
            )
            prepared = await prepare_scene_track_command(
                db,
                anchor_task=tasks[1],
                request=command,
            )
            native_request = _apply_native_request(
                prepared,
                expected_snapshot_token=prepared.snapshot_token,
                actor_id=actor.id,
                idempotency_key="native-split-scene-command",
            )
            digest = request_digest(command)

        connection = await asyncpg.connect(dsn, timeout=5, command_timeout=10)
        assert await connection.fetchval("SELECT current_user") == _ROLE
        await verify_function_only_role(
            connection,
            schema="aap_labeling_v1",
            functions={("scene_track_command", "jsonb")},
            role=_ROLE,
        )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.fetchval(
                "SELECT track_id FROM public.scene_tracks LIMIT 1"
            )

        async def call(request_json: str):
            return await connection.fetchval(
                "SELECT aap_labeling_v1.scene_track_command($1::jsonb)",
                request_json,
            )

        client = SceneTrackCommandDataClient(call)
        transaction = connection.transaction()
        await transaction.start()
        rolled_back = await client.command(native_request)
        assert rolled_back.response_json is not None
        assert rolled_back.response_json["created_track_id"].startswith("trk_")
        await transaction.rollback()

        async with maker() as db:
            assert (
                await db.scalar(
                    select(SceneTrackOperation.id).where(
                        SceneTrackOperation.idempotency_key
                        == "native-split-scene-command"
                    )
                )
                is None
            )
            for annotation_id in annotation_ids:
                annotation = await db.get(Annotation, annotation_id)
                assert annotation.track_id == track_id
            intervals = list(
                (
                    await db.execute(
                        select(SceneTrackInterval).where(
                            SceneTrackInterval.scene_track_id == track_ids[0]
                        )
                    )
                ).scalars()
            )
            assert [(row.start_frame, row.end_frame) for row in intervals] == [(0, 2)]

        transaction = connection.transaction()
        await transaction.start()
        committed = await client.command(native_request)
        await transaction.commit()
        assert committed.operation is not None
        assert committed.operation.kind == "split"
        operation_id = committed.operation.id
        created_track_id = committed.response_json["created_track_id"]

        transaction = connection.transaction()
        await transaction.start()
        replay = await client.command(
            scene_track_command(
                "replay",
                scene_id=scene_ids[0],
                actor_id=actor_ids[0],
                idempotency_key="native-split-scene-command",
                request_digest=digest,
            )
        )
        assert replay.found is True
        assert replay.response_json["operation_id"] == str(operation_id)
        await transaction.commit()

        transaction = connection.transaction()
        await transaction.start()
        reverted = await client.command(
            scene_track_command(
                "revert",
                scene_id=scene_ids[0],
                operation_id=operation_id,
                actor_id=actor_ids[0],
                idempotency_key="native-revert-scene-command",
            )
        )
        await transaction.commit()
        assert reverted.operation is not None
        assert reverted.operation.kind == "revert"

        async with maker() as db:
            original = await db.get(SceneTrackOperation, operation_id)
            created = (
                await db.execute(
                    select(SceneTrack).where(SceneTrack.track_id == created_track_id)
                )
            ).scalar_one()
            assert original.status == "reverted"
            assert created.retired_at is not None
            intervals = list(
                (
                    await db.execute(
                        select(SceneTrackInterval).where(
                            SceneTrackInterval.scene_track_id == track_ids[0]
                        )
                    )
                ).scalars()
            )
            assert [(row.start_frame, row.end_frame) for row in intervals] == [(0, 2)]
            for annotation_id in annotation_ids:
                annotation = await db.get(Annotation, annotation_id)
                assert annotation.track_id == track_id
                assert annotation.is_active is True
    finally:
        if connection is not None:
            await connection.close()
        async with maker.begin() as db:
            if annotation_ids:
                await db.execute(
                    delete(Annotation).where(Annotation.id.in_(annotation_ids))
                )
            if scene_ids:
                await db.execute(
                    delete(SceneTrackOperation).where(
                        SceneTrackOperation.scene_id.in_(scene_ids)
                    )
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
