"""Video snapshot scope and lock fencing through the ordinary runtime role."""

import os
import uuid

import pytest
from sqlalchemy import delete, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.v1._test_seed import _register_fixture_original
from app.config import settings
from app.db.models.annotation import Annotation
from app.db.models.dataset import Dataset, DatasetItem, VideoSegment
from app.db.models.prediction import Prediction
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.user import User
from app.db.models.video_track_quality import VideoTrackQualityRun
from app.services.content_snapshot import ContentError
from app.services.exporting import video_inputs
from app.services.identity_e2e_fixtures import IdentityE2EFixtures
from app.services.labeling_e2e_fixture_writes import LabelingE2EFixtureWrites
from app.services.workspace_e2e_fixtures import create_fixture_project
from app.services.mask_formats.catalog import registry
from app.services.raster_mask_storage import build_rle_reference
from tests.factory import build_tool_bindings

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the verified disposable owner and restricted runtime roles",
    ),
]


async def test_video_snapshot_scope_and_shared_fences(test_engine):
    assert make_url(settings.database_url).database == test_engine.url.database
    owner = async_sessionmaker(test_engine, expire_on_commit=False)
    runtime_engine = create_async_engine(settings.database_url, hide_parameters=True)
    runtime = async_sessionmaker(runtime_engine, expire_on_commit=False)
    actor_id = project_id = dataset_id = None
    task_ids = []
    try:
        async with owner.begin() as db:
            actor = await IdentityE2EFixtures(db).create_user(
                "project_admin",
                f"video-export-{uuid.uuid4().hex}@e2e.test",
                "Video export",
            )
            actor_id = actor.id
            project = await create_fixture_project(
                db,
                owner_id=actor.id,
                name="Native video snapshot",
                type_key="video-track",
                type_label="Video tracking",
                data_type="video",
                tool_bindings=build_tool_bindings(["car"]),
            )
            project_id = project.id
            dataset = Dataset(
                display_id=f"DS-E2E-{uuid.uuid4().hex[:12]}",
                name="Video snapshot metadata",
                data_type="video",
                created_by=actor.id,
            )
            db.add(dataset)
            await db.flush()
            dataset_id = dataset.id
            item = DatasetItem(
                dataset_id=dataset.id,
                file_name="snapshot.mp4",
                file_path=f"e2e/video-snapshot/{uuid.uuid4()}.mp4",
                file_type="video",
                file_size=5,
            )
            db.add(item)
            await db.flush()
            # Collection freezes owner metadata only; it must not probe source bytes.
            await _register_fixture_original(db, item, b"video")
            writes = LabelingE2EFixtureWrites(db)
            tasks = await writes.insert_tasks(
                [
                    Task(
                        project_id=project.id,
                        dataset_item_id=item.id,
                        display_id=f"T-E2E-{uuid.uuid4().hex[:12]}",
                        file_name=item.file_name,
                        file_path=item.file_path,
                        file_type="video",
                        status="pending",
                    )
                    for _ in range(2)
                ]
            )
            task_ids = [task.id for task in tasks]
            annotation = (
                await writes.insert_annotations(
                    [
                        Annotation(
                            task_id=tasks[0].id,
                            project_id=project.id,
                            user_id=actor.id,
                            source="manual",
                            class_name="car",
                            annotation_type="video_track_bbox",
                            tool_unit_id="bbox",
                            geometry={
                                "type": "video_track_bbox",
                                "track_id": "native-read",
                                "keyframes": [
                                    {
                                        "frame_index": 0,
                                        "bbox": {
                                            "x": 0.1,
                                            "y": 0.2,
                                            "w": 0.3,
                                            "h": 0.4,
                                        },
                                    }
                                ],
                                "outside": [],
                            },
                        )
                    ]
                )
            )[0]
            segments = [
                VideoSegment(
                    dataset_item_id=item.id,
                    segment_index=index,
                    start_frame=index * 5,
                    end_frame=index * 5 + 4,
                )
                for index in range(2)
            ]
            db.add_all(segments)
            await db.flush()
            runs = [
                VideoTrackQualityRun(
                    task_id=task.id,
                    left_segment_id=segments[0].id,
                    right_segment_id=segments[1].id,
                    status="completed",
                    input_snapshot=[],
                    input_digest="a" * 64,
                    sampling_digest="b" * 64,
                    metrics={
                        "sampling_frames": [4, 5],
                        "unused": "not part of the frozen report",
                    },
                    pairs=[],
                )
                for task in tasks
            ]
            db.add_all(runs)
            await db.flush()
            selected_run_id = runs[0].id
            segment_ids = {segment.id for segment in segments}
            annotation_id = annotation.id

        options = {
            "video_frame_mode": "keyframes",
            "axis_frame": "iso",
            "include_attributes": True,
            "video_overlap_policy": "error",
            "mots_frame_base": 0,
        }
        async with runtime.begin() as db:
            for table in (
                "projects",
                "tasks",
                "annotations",
                "video_segments",
                "video_track_quality_runs",
            ):
                assert not await db.scalar(
                    text("SELECT has_table_privilege(current_user, :table, 'SELECT')"),
                    {"table": f"public.{table}"},
                )
            captured = await video_inputs.collect(
                db,
                project_id,
                None,
                ["aap_json"],
                options,
                task_ids=[task_ids[0]],
                lock=True,
            )
            snapshot = captured.snapshot
            assert {row["id"] for row in snapshot["tasks"]} == {str(task_ids[0])}
            assert {row["id"] for row in snapshot["annotations"]} == {
                str(annotation_id)
            }
            assert {row["id"] for row in snapshot["segments"]} == {
                str(value) for value in segment_ids
            }
            assert [row["id"] for row in snapshot["quality_runs"]] == [
                str(selected_run_id)
            ]
            assert snapshot["quality_runs"][0]["metrics"] == {"sampling_frames": [4, 5]}
            for table, identifier in (
                ("video_segments", segments[0].id),
                ("video_track_quality_runs", selected_run_id),
            ):
                async with owner.begin() as writer:
                    assert (
                        await writer.scalar(
                            text(
                                f"SELECT id FROM public.{table} WHERE id=:id FOR SHARE NOWAIT"
                            ),
                            {"id": identifier},
                        )
                        == identifier
                    )
                with pytest.raises(DBAPIError) as conflict:
                    async with owner.begin() as writer:
                        await writer.execute(
                            text(
                                f"SELECT id FROM public.{table} WHERE id=:id FOR UPDATE NOWAIT"
                            ),
                            {"id": identifier},
                        )
                assert getattr(conflict.value.orig, "sqlstate", None) == "55P03"

        async with owner.begin() as writer:
            for table, identifier in (
                ("video_segments", segments[0].id),
                ("video_track_quality_runs", selected_run_id),
            ):
                assert (
                    await writer.scalar(
                        text(
                            f"SELECT id FROM public.{table} WHERE id=:id FOR UPDATE NOWAIT"
                        ),
                        {"id": identifier},
                    )
                    == identifier
                )
            with pytest.raises(ContentError) as busy:
                async with runtime.begin() as db:
                    await video_inputs.collect(
                        db,
                        project_id,
                        None,
                        ["aap_json"],
                        options,
                        task_ids=[task_ids[0]],
                        lock=True,
                    )
            assert (busy.value.code, busy.value.status) == (
                "video_export_inputs_busy",
                409,
            )

        async with runtime.begin() as db:
            predicate = video_inputs.VideoTrackQualityRun.task_id == task_ids[0]
            with pytest.raises(ContentError) as oversized:
                await video_inputs._quality_rows(
                    db,
                    predicate,
                    budget=video_inputs._Budget(
                        read_bytes=video_inputs.MAX_JSON_BYTES - 1
                    ),
                    lock=True,
                )
            assert (oversized.value.code, oversized.value.status) == (
                "video_export_input_budget_exceeded",
                422,
            )
    finally:
        await runtime_engine.dispose()
        async with owner.begin() as db:
            if task_ids:
                await db.execute(
                    delete(VideoTrackQualityRun).where(
                        VideoTrackQualityRun.task_id.in_(task_ids)
                    )
                )
                await db.execute(
                    delete(Annotation).where(Annotation.task_id.in_(task_ids))
                )
                await db.execute(
                    delete(Prediction).where(Prediction.task_id.in_(task_ids))
                )
                await db.execute(delete(Task).where(Task.id.in_(task_ids)))
            if project_id:
                await db.execute(delete(Project).where(Project.id == project_id))
            if dataset_id:
                await db.execute(delete(Dataset).where(Dataset.id == dataset_id))
            if actor_id:
                await db.execute(delete(User).where(User.id == actor_id))


async def test_video_preflight_shapes_and_loss_boundaries(test_engine):
    assert make_url(settings.database_url).database == test_engine.url.database
    owner = async_sessionmaker(test_engine, expire_on_commit=False)
    runtime_engine = create_async_engine(settings.database_url, hide_parameters=True)
    runtime = async_sessionmaker(runtime_engine, expire_on_commit=False)
    actor_id = project_id = dataset_id = None
    task_ids = []
    points = [{"x": 0.1, "y": 0.1}, {"x": 0.8, "y": 0.1}, {"x": 0.4, "y": 0.8}]
    geometries = [
        {
            "type": "video_track_bbox",
            "track_id": "box",
            "keyframes": [
                {"frame_index": 0, "bbox": {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}}
            ],
            "outside": [],
        },
        {"type": "video_polygon", "frame_index": 0, "points": points},
        {
            "type": "video_track_polygon",
            "track_id": "polygon",
            "keyframes": [{"frame_index": 0, "points": points}],
            "outside": [],
        },
        {
            "type": "video_track_mask",
            "track_id": "mask",
            "keyframes": [
                {
                    "frame_index": 0,
                    "mask": build_rle_reference(
                        {"encoding": "coco_rle", "size": [3, 4], "counts": [0, 1, 11]}
                    ),
                }
            ],
            "outside": [],
        },
    ]
    try:
        async with owner.begin() as db:
            actor = await IdentityE2EFixtures(db).create_user(
                "project_admin",
                f"video-preflight-{uuid.uuid4().hex}@e2e.test",
                "Video preflight",
            )
            actor_id = actor.id
            project = await create_fixture_project(
                db,
                owner_id=actor.id,
                name="Video preflight",
                type_key="video-track",
                type_label="Video tracking",
                data_type="video",
                tool_bindings=build_tool_bindings(["car"]),
            )
            project_id = project.id
            dataset = Dataset(
                display_id=f"DS-E2E-{uuid.uuid4().hex[:12]}",
                name="Video preflight metadata",
                data_type="video",
                created_by=actor.id,
            )
            db.add(dataset)
            await db.flush()
            dataset_id = dataset.id
            item = DatasetItem(
                dataset_id=dataset.id,
                file_name="preflight.mp4",
                file_path=f"e2e/video-preflight/{uuid.uuid4()}.mp4",
                file_type="video",
                file_size=5,
            )
            db.add(item)
            await db.flush()
            await _register_fixture_original(db, item, b"video")
            writes = LabelingE2EFixtureWrites(db)
            tasks = await writes.insert_tasks(
                [
                    Task(
                        project_id=project.id,
                        dataset_item_id=item.id,
                        display_id=f"T-E2E-{uuid.uuid4().hex[:12]}",
                        file_name=item.file_name,
                        file_path=item.file_path,
                        file_type="video",
                        status="pending",
                    )
                    for _ in geometries
                ]
            )
            task_ids = [task.id for task in tasks]
            await writes.insert_annotations(
                [
                    Annotation(
                        task_id=task.id,
                        project_id=project.id,
                        user_id=actor.id,
                        source="manual",
                        class_name="car",
                        annotation_type=geometry["type"],
                        tool_unit_id="region",
                        geometry=geometry,
                    )
                    for task, geometry in zip(tasks, geometries, strict=True)
                ]
            )

        supported_tasks = {
            "yolo-frames-det": set(task_ids),
            "mot": set(task_ids),
            "yolo-frames-seg": set(task_ids[1:3]),
            "coco-frames-seg": set(task_ids[1:]),
        }
        async with runtime.begin() as db:
            for format_id, supported in supported_tasks.items():
                plan = await registry.get(format_id).preflight_export(
                    db,
                    project=project,
                    scope={"project_id": str(project.id)},
                    options={},
                )
                assert {
                    row.task_id for row in plan.items if row.loss_class == "lossy"
                } == supported
                assert {
                    row.task_id for row in plan.items if row.loss_class == "unsupported"
                } == set(task_ids) - supported
                assert {row.code for row in plan.losses} >= {"frame_base_changed"}
                for row in plan.items:
                    assert row.estimated_objects == 1
                    assert {warning.code for warning in row.warnings} == (
                        set() if row.task_id in supported else {"unsupported_geometry"}
                    )
    finally:
        await runtime_engine.dispose()
        async with owner.begin() as db:
            if task_ids:
                await db.execute(
                    delete(Annotation).where(Annotation.task_id.in_(task_ids))
                )
                await db.execute(delete(Task).where(Task.id.in_(task_ids)))
            if project_id:
                await db.execute(delete(Project).where(Project.id == project_id))
            if dataset_id:
                await db.execute(delete(Dataset).where(Dataset.id == dataset_id))
            if actor_id:
                await db.execute(delete(User).where(User.id == actor_id))
