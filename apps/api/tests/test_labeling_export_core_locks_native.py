"""Exercise export locks through the restricted Labeling core-read role."""

import os
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy import delete, event, func, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models.dataset import Dataset, DatasetItem, Scene
from app.db.models.prediction import Prediction
from app.db.models.project import Project
from app.db.models.scene_track import SceneTrack, SceneTrackInterval
from app.db.models.task import Task
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.db.models.user import User
from app.services.content_snapshot import ContentError
from app.services.exporting import core_reads, lidar_inputs
from app.services.labeling_core_read_relations import prediction_row
from tests.factory import create_project, create_task, create_user
from tests.workspace_project_query_helpers import _dsn

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the verified Labeling core-read role",
    ),
]


async def test_export_locks_scope_limit_modes_and_rollback(test_engine):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    actor_id = dataset_id = None
    project_ids, task_ids, track_ids = [], [], []
    client = None
    try:
        async with maker.begin() as db:
            actor = await create_user(
                db,
                "project_admin",
                f"export-lock-{uuid.uuid4().hex}@example.test",
                "Export locks",
            )
            actor_id = actor.id
            projects = [await create_project(db, owner_id=actor.id) for _ in range(2)]
            project_ids = [p.id for p in projects]
            tasks = [
                await create_task(db, project_id=projects[0].id, status="pending")
                for _ in range(2)
            ]
            tasks.append(
                await create_task(db, project_id=projects[1].id, status="pending")
            )
            task_ids = [t.id for t in tasks]
            dataset = Dataset(
                display_id=f"DS-{uuid.uuid4().hex[:10]}",
                name="Export locks",
                data_type="pointcloud",
                created_by=actor.id,
            )
            db.add(dataset)
            await db.flush()
            dataset_id = dataset.id
            item = DatasetItem(
                dataset_id=dataset.id,
                file_name="frame.pcd",
                file_path="export-lock/frame.pcd",
                file_type="pointcloud",
            )
            db.add(item)
            await db.flush()
            link_ids = sorted([uuid.uuid4(), uuid.uuid4(), uuid.uuid4()])
            db.add_all(
                [
                    TaskDatasetItemLink(
                        id=link_ids[0],
                        task_id=task_ids[1],
                        dataset_item_id=item.id,
                        role="primary_lidar",
                    ),
                    TaskDatasetItemLink(
                        id=link_ids[1],
                        task_id=task_ids[0],
                        dataset_item_id=item.id,
                        role="primary_lidar",
                    ),
                    TaskDatasetItemLink(
                        id=link_ids[2],
                        task_id=task_ids[2],
                        dataset_item_id=item.id,
                        role="primary_lidar",
                    ),
                ]
            )
            scene = Scene(
                display_id=f"S-{uuid.uuid4().hex[:10]}",
                dataset_id=dataset.id,
                name="Export locks",
            )
            db.add(scene)
            await db.flush()
            track_ids = sorted([uuid.uuid4(), uuid.uuid4(), uuid.uuid4()])
            db.add_all(
                [
                    SceneTrack(
                        id=track_id,
                        project_id=projects[0].id,
                        scene_id=scene.id,
                        track_id=str(track_id),
                        class_name="car",
                    )
                    for track_id in track_ids
                ]
            )
            await db.flush()
            interval_ids = sorted([uuid.uuid4(), uuid.uuid4(), uuid.uuid4()])
            db.add_all(
                [
                    SceneTrackInterval(
                        id=interval_ids[0],
                        scene_track_id=track_ids[0],
                        start_frame=0,
                        end_frame=1,
                        source="manual",
                    ),
                    SceneTrackInterval(
                        id=interval_ids[1],
                        scene_track_id=track_ids[0],
                        start_frame=2,
                        end_frame=3,
                        source="manual",
                    ),
                    SceneTrackInterval(
                        id=interval_ids[2],
                        scene_track_id=track_ids[1],
                        start_frame=0,
                        end_frame=1,
                        source="manual",
                    ),
                ]
            )
            prediction_id = uuid.uuid4()
            now = datetime.now(timezone.utc).replace(microsecond=0)
            times = [now + timedelta(seconds=n) for n in range(4)]
            db.add_all(
                [
                    Prediction(
                        id=prediction_id,
                        task_id=tasks[0].id,
                        project_id=projects[0].id,
                        created_at=times[0],
                        result={"n": 0},
                    ),
                    Prediction(
                        id=prediction_id,
                        task_id=tasks[0].id,
                        project_id=projects[0].id,
                        created_at=times[1],
                        result={"n": 1},
                    ),
                    Prediction(
                        id=prediction_id,
                        task_id=tasks[1].id,
                        project_id=projects[0].id,
                        created_at=times[2],
                        result={"n": 2},
                    ),
                    Prediction(
                        id=prediction_id,
                        task_id=tasks[2].id,
                        project_id=projects[1].id,
                        created_at=times[3],
                        result={"n": 3},
                    ),
                ]
            )

        client = await asyncpg.connect(
            _dsn(
                test_engine,
                "AAP_MODULE_DATA_LABELING_CORE_READ_URL",
                "yoao_labeling_core_read_client",
            )
        )
        pred = "SELECT * FROM yoao_labeling_v1.lock_prediction_rows_for_core_read_share($1,$2,$3)"
        tracks = (
            "SELECT * FROM yoao_labeling_v1.lock_scene_track_rows_for_core_read($1,$2)"
        )
        intervals = "SELECT * FROM yoao_labeling_v1.lock_scene_track_interval_rows_for_core_read_share($1,$2)"
        links = "SELECT * FROM yoao_labeling_v1.lock_task_dataset_item_link_rows_for_core_read_share($1,$2)"
        rows = await client.fetch(pred, project_ids[0], [task_ids[0], task_ids[2]], 10)
        assert [(r["id"], r["created_at"], r["task_id"]) for r in rows] == [
            (prediction_id, times[0], task_ids[0]),
            (prediction_id, times[1], task_ids[0]),
        ]
        assert len(await client.fetch(pred, project_ids[0], task_ids[:2], 2)) == 2
        assert len(await client.fetch(pred, project_ids[1], task_ids, 10)) == 1
        assert await client.fetch(pred, project_ids[0], [], 10) == []
        assert [
            r["id"]
            for r in await client.fetch(tracks, list(reversed(track_ids[:2])), 1)
        ] == track_ids[:1]
        assert [
            r["id"] for r in await client.fetch(intervals, [track_ids[0]], 10)
        ] == interval_ids[:2]
        assert [
            r["id"] for r in await client.fetch(intervals, track_ids[:2], 2)
        ] == interval_ids[:2]
        assert [
            r["id"] for r in await client.fetch(links, link_ids[:2], 10)
        ] == link_ids[:2]
        assert [
            r["id"] for r in await client.fetch(links, link_ids[:2], 1)
        ] == link_ids[:1]
        assert [r["id"] for r in await client.fetch(links, [link_ids[1]], 10)] == [
            link_ids[1]
        ]
        assert await client.fetch(links, [], 10) == []

        async with test_engine.connect() as owner:

            async def exercise(
                statement, args, owner_sql, owner_args, share_compatible
            ):
                transaction = client.transaction()
                await transaction.start()
                try:
                    assert await client.fetch(statement, *args)
                    if share_compatible:
                        async with owner.begin():
                            await owner.execute(
                                text(owner_sql + " FOR SHARE NOWAIT"), owner_args
                            )
                    else:
                        with pytest.raises(DBAPIError) as conflict:
                            async with owner.begin():
                                await owner.execute(
                                    text(owner_sql + " FOR SHARE NOWAIT"), owner_args
                                )
                        assert getattr(conflict.value.orig, "sqlstate", None) == "55P03"
                    with pytest.raises(DBAPIError) as conflict:
                        async with owner.begin():
                            await owner.execute(
                                text(owner_sql + " FOR UPDATE NOWAIT"), owner_args
                            )
                    assert getattr(conflict.value.orig, "sqlstate", None) == "55P03"
                finally:
                    await transaction.rollback()
                async with owner.begin():
                    assert (
                        await owner.execute(
                            text(owner_sql + " FOR UPDATE NOWAIT"), owner_args
                        )
                    ).first() is not None

            await exercise(
                pred,
                (project_ids[0], [task_ids[0]], 2),
                "SELECT id FROM public.predictions WHERE id=:id AND created_at=:created",
                {"id": prediction_id, "created": times[1]},
                True,
            )
            await exercise(
                tracks,
                ([track_ids[0]], 1),
                "SELECT id FROM public.scene_tracks WHERE id=:id",
                {"id": track_ids[0]},
                False,
            )
            await exercise(
                intervals,
                ([track_ids[0]], 2),
                "SELECT id FROM public.scene_track_intervals WHERE id=:id",
                {"id": interval_ids[1]},
                True,
            )
            await exercise(
                links,
                (link_ids[:2], 2),
                "SELECT id FROM public.task_dataset_item_links WHERE id=:id",
                {"id": link_ids[1]},
                True,
            )

            for statement, args, owner_sql, owner_args in (
                (
                    pred,
                    (project_ids[0], [task_ids[0]], 2),
                    "SELECT id FROM public.predictions WHERE id=:id AND created_at=:created",
                    {"id": prediction_id, "created": times[0]},
                ),
                (
                    tracks,
                    ([track_ids[0]], 1),
                    "SELECT id FROM public.scene_tracks WHERE id=:id",
                    {"id": track_ids[0]},
                ),
                (
                    intervals,
                    ([track_ids[0]], 2),
                    "SELECT id FROM public.scene_track_intervals WHERE id=:id",
                    {"id": interval_ids[0]},
                ),
                (
                    links,
                    (link_ids[:2], 2),
                    "SELECT id FROM public.task_dataset_item_links WHERE id=:id",
                    {"id": link_ids[0]},
                ),
            ):
                async with owner.begin():
                    await owner.execute(
                        text(owner_sql + " FOR UPDATE NOWAIT"), owner_args
                    )
                    with pytest.raises(asyncpg.LockNotAvailableError):
                        await client.fetch(statement, *args)
                assert await client.fetch(statement, *args)

            for statement, args, owner_sql, owner_args in (
                (
                    pred,
                    (project_ids[0], [task_ids[0]], 1),
                    "SELECT id FROM public.predictions WHERE id=:id AND created_at=:created",
                    {"id": prediction_id, "created": times[1]},
                ),
                (
                    pred,
                    (project_ids[0], [task_ids[0]], 10),
                    "SELECT id FROM public.predictions WHERE id=:id AND created_at=:created",
                    {"id": prediction_id, "created": times[2]},
                ),
                (
                    pred,
                    (project_ids[0], task_ids, 10),
                    "SELECT id FROM public.predictions WHERE id=:id AND created_at=:created",
                    {"id": prediction_id, "created": times[3]},
                ),
                (
                    tracks,
                    (track_ids[:2], 1),
                    "SELECT id FROM public.scene_tracks WHERE id=:id",
                    {"id": track_ids[1]},
                ),
                (
                    intervals,
                    (track_ids[:2], 2),
                    "SELECT id FROM public.scene_track_intervals WHERE id=:id",
                    {"id": interval_ids[2]},
                ),
                (
                    links,
                    (link_ids[:2], 1),
                    "SELECT id FROM public.task_dataset_item_links WHERE id=:id",
                    {"id": link_ids[1]},
                ),
                (
                    links,
                    (link_ids[:2], 10),
                    "SELECT id FROM public.task_dataset_item_links WHERE id=:id",
                    {"id": link_ids[2]},
                ),
            ):
                async with owner.begin():
                    await owner.execute(
                        text(owner_sql + " FOR UPDATE NOWAIT"), owner_args
                    )
                    assert await client.fetch(statement, *args)

        reader_engine = create_async_engine(
            os.environ["AAP_MODULE_DATA_LABELING_CORE_READ_URL"], hide_parameters=True
        )
        reader = async_sessionmaker(reader_engine, expire_on_commit=False)
        predicate = (prediction_row.project_id == project_ids[0]) & (
            prediction_row.task_id == task_ids[0]
        )
        locked = core_reads.lock_ids(
            func.yoao_labeling_v1.lock_prediction_rows_for_core_read_share(
                project_ids[0], [task_ids[0]], 3
            )
        )
        try:
            async with reader.begin() as db:
                rows = await lidar_inputs._rows(
                    db,
                    prediction_row,
                    predicate,
                    budget=lidar_inputs._Budget(),
                    max_rows=2,
                    lock=True,
                    lock_query=locked,
                    order=[prediction_row.created_at, prediction_row.id],
                )
                assert [(row.id, row.created_at, row.result) for row in rows] == [
                    (prediction_id, times[0], {"n": 0}),
                    (prediction_id, times[1], {"n": 1}),
                ]
            with pytest.raises(ContentError) as overflow:
                async with reader.begin() as db:
                    await lidar_inputs._rows(
                        db,
                        prediction_row,
                        predicate,
                        budget=lidar_inputs._Budget(),
                        max_rows=1,
                        lock=True,
                        lock_query=locked,
                    )
            assert (overflow.value.code, overflow.value.status) == (
                "lidar_export_input_budget_exceeded",
                422,
            )

            loaded = []

            def observe_load(row, context):
                loaded.append((row.id, row.created_at))

            event.listen(Prediction, "load", observe_load)
            try:
                with pytest.raises(ContentError) as overflow:
                    async with reader.begin() as db:
                        await lidar_inputs._rows(
                            db,
                            prediction_row,
                            predicate,
                            budget=lidar_inputs._Budget(
                                read_bytes=lidar_inputs.MAX_JSON_BYTES - 1
                            ),
                            max_rows=2,
                            lock=True,
                            lock_query=locked,
                        )
                assert (overflow.value.code, overflow.value.status) == (
                    "lidar_export_input_budget_exceeded",
                    422,
                )
                assert loaded == []
            finally:
                event.remove(Prediction, "load", observe_load)
        finally:
            await reader_engine.dispose()
    finally:
        if client is not None:
            await client.close()
        async with maker.begin() as db:
            if task_ids:
                await db.execute(
                    delete(Prediction).where(Prediction.task_id.in_(task_ids))
                )
            if track_ids:
                await db.execute(
                    delete(SceneTrackInterval).where(
                        SceneTrackInterval.scene_track_id.in_(track_ids)
                    )
                )
                await db.execute(delete(SceneTrack).where(SceneTrack.id.in_(track_ids)))
            if task_ids:
                await db.execute(delete(Task).where(Task.id.in_(task_ids)))
            if project_ids:
                await db.execute(delete(Project).where(Project.id.in_(project_ids)))
            if dataset_id:
                await db.execute(delete(Dataset).where(Dataset.id == dataset_id))
            if actor_id:
                await db.execute(delete(User).where(User.id == actor_id))
