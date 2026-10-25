"""Exercise SceneTrack writes through the function-only Labeling LOGIN."""

import os
import uuid

import asyncpg
import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_labeling.scene_track_domain_contract import scene_track_command
from aap_labeling.scene_track_domain_data import SceneTrackDomainDataClient
from aap_owner_data.postgres import verify_function_only_role
from app.db.models.annotation import Annotation
from app.db.models.dataset import Dataset, DatasetItem, Scene
from app.db.models.scene_track import SceneTrack, SceneTrackInterval
from app.db.models.task import Task
from app.db.models.project import Project
from app.db.models.user import User
from tests.factory import create_user
from tests.test_scene_track_domain import _annotation, _seed_scene
from tests.workspace_project_query_helpers import _dsn

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the verified Labeling annotation-write role",
    ),
]

_FUNCTIONS = {
    ("annotation_statistics_command", "jsonb"),
    ("scene_track_domain_command", "jsonb"),
    ("annotation_write_command", "jsonb"),
}


async def test_reclassification_updates_camera_members_in_the_outer_transaction(
    test_engine,
):
    dsn = _dsn(
        test_engine,
        "AAP_MODULE_DATA_LABELING_ANNOTATION_WRITE_URL",
        "labeling_annotation_write_client",
    )
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    actor_ids: list[uuid.UUID] = []
    project_ids: list[uuid.UUID] = []
    dataset_ids: list[uuid.UUID] = []
    scene_ids: list[uuid.UUID] = []
    task_ids: list[uuid.UUID] = []
    track_ids: list[uuid.UUID] = []
    annotation_ids: list[uuid.UUID] = []
    connection = None
    try:
        async with maker.begin() as db:
            actor = await create_user(
                db,
                "project_admin",
                f"scene-track-native-{uuid.uuid4().hex}@example.test",
                "SceneTrack native owner",
            )
            actor_ids.append(actor.id)
            project, scene, tasks = await _seed_scene(
                db, owner_id=actor.id, frame_count=1
            )
            task = tasks[0]
            project_ids.append(project.id)
            dataset_ids.append(scene.dataset_id)
            scene_ids.append(scene.id)
            camera_item = DatasetItem(
                dataset_id=scene.dataset_id,
                file_name="camera.jpg",
                file_path="scene-track-native/camera.jpg",
                file_type="image",
                scene_id=scene.id,
                frame_index=0,
            )
            db.add(camera_item)
            await db.flush()
            task_ids.append(task.id)
            track = SceneTrack(
                project_id=project.id,
                scene_id=scene.id,
                track_id=f"trk_{uuid.uuid4().hex}",
                class_name="car",
                presence_mode="inferred",
                attributes={},
                attributes_meta={},
                revision=1,
                created_by=actor.id,
            )
            db.add(track)
            await db.flush()
            track_ids.append(track.id)
            db.add(
                SceneTrackInterval(
                    scene_track_id=track.id,
                    start_frame=0,
                    end_frame=0,
                    source="manual",
                    version=1,
                    created_by=actor.id,
                )
            )
            primary = _annotation(
                task=task,
                project_id=project.id,
                user_id=actor.id,
            )
            primary.track_id = track.track_id
            primary.scene_track_id = track.id
            camera = Annotation(
                task_id=task.id,
                project_id=project.id,
                user_id=actor.id,
                source="manual",
                annotation_type="bbox",
                tool_unit_id="bbox",
                class_name="car",
                geometry={"type": "bbox", "x": 1, "y": 1, "width": 10, "height": 10},
                track_id=track.track_id,
                scene_track_id=track.id,
                sensor_dataset_item_id=camera_item.id,
                sensor_role="camera_front",
                sensor_visibility="visible",
                calibration_revision=1,
                calibration_digest="a" * 64,
            )
            db.add_all([primary, camera])
            await db.flush()
            annotation_ids.extend([primary.id, camera.id])

        connection = await asyncpg.connect(dsn, timeout=5, command_timeout=10)
        await verify_function_only_role(
            connection,
            schema="aap_labeling_v1",
            functions=_FUNCTIONS,
            role="labeling_annotation_write_client",
        )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.fetchval(
                "SELECT class_name FROM public.scene_tracks LIMIT 1"
            )

        async def call(request_json: str):
            return await connection.fetchval(
                "SELECT aap_labeling_v1.scene_track_domain_command($1::jsonb)",
                request_json,
            )

        client = SceneTrackDomainDataClient(call)
        ensure = scene_track_command(
            "ensure",
            project_id=project_ids[0],
            scene_id=scene_ids[0],
            track_id=track.track_id,
            class_name="car",
            frames=[0, 1],
            actor_id=actor_ids[0],
            interval_source="derived",
            member_change=False,
        )
        transaction = connection.transaction()
        await transaction.start()
        expanded = await client.command(ensure)
        assert expanded.track is not None and expanded.track.revision == 2
        assert expanded.changed is True
        await transaction.rollback()

        async with maker() as db:
            track_after_rollback = await db.get(SceneTrack, track_ids[0])
            interval_after_rollback = await db.scalar(
                select(SceneTrackInterval).where(
                    SceneTrackInterval.scene_track_id == track_ids[0]
                )
            )
            assert track_after_rollback.revision == 1
            assert interval_after_rollback.end_frame == 0
            assert interval_after_rollback.version == 1

        transaction = connection.transaction()
        await transaction.start()
        expanded = await client.command(ensure)
        assert expanded.track is not None and expanded.track.revision == 2
        assert expanded.changed is True
        await transaction.commit()

        request = scene_track_command(
            "reclassify",
            annotation_id=annotation_ids[0],
            scene_track_id=track_ids[0],
            track_id=track.track_id,
            expected_class_name="car",
            class_name="truck",
        )
        transaction = connection.transaction()
        await transaction.start()
        rolled_back = await client.command(request)
        assert rolled_back.changed is True
        assert rolled_back.track is not None and rolled_back.track.revision == 3
        assert len(rolled_back.camera_members) == 1
        assert rolled_back.camera_members[0].class_name == "truck"
        assert rolled_back.camera_members[0].version == 2
        await transaction.rollback()

        async with maker() as db:
            track_after_rollback = await db.get(SceneTrack, track_ids[0])
            camera_after_rollback = await db.get(Annotation, annotation_ids[1])
            assert track_after_rollback.class_name == "car"
            assert track_after_rollback.revision == 2
            assert camera_after_rollback.class_name == "car"
            assert camera_after_rollback.version == 1

        transaction = connection.transaction()
        await transaction.start()
        committed = await client.command(request)
        assert committed.changed is True
        await transaction.commit()

        async with maker() as db:
            track_after_commit = await db.get(SceneTrack, track_ids[0])
            camera_after_commit = await db.get(Annotation, annotation_ids[1])
            assert track_after_commit.class_name == "truck"
            assert track_after_commit.revision == 3
            assert camera_after_commit.class_name == "truck"
            assert camera_after_commit.version == 2

        async with maker.begin() as db:
            primary_after_commit = await db.get(Annotation, annotation_ids[0])
            primary_after_commit.class_name = "truck"
            await db.flush()

        touch = scene_track_command(
            "touch",
            annotation_id=annotation_ids[0],
            scene_track_id=track_ids[0],
            track_id=track.track_id,
            class_name="truck",
        )
        async with connection.transaction():
            touched = await client.command(touch)
            assert touched.touched is True
            assert touched.track is not None and touched.track.revision == 4

        async with maker() as db:
            track_after_touch = await db.get(SceneTrack, track_ids[0])
            assert track_after_touch.revision == 4
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
            if dataset_ids:
                await db.execute(delete(Dataset).where(Dataset.id.in_(dataset_ids)))
            if project_ids:
                await db.execute(delete(Project).where(Project.id.in_(project_ids)))
            if actor_ids:
                await db.execute(delete(User).where(User.id.in_(actor_ids)))
