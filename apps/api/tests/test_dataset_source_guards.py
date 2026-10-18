from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from app.db.models.active_learning import (
    ActiveLearningCandidate,
    ActiveLearningPoolMember,
    ActiveLearningRound,
)
from app.db.models.annotation import Annotation
from app.db.models.dataset import (
    Dataset,
    DatasetItem,
    ProjectDataset,
    Scene,
    VideoSegment,
)
from app.db.models.point_cloud_quality import (
    PointCloudQualityIssue,
    PointCloudQualityRun,
)
from app.db.models.scene_track import SceneTrack, SceneTrackOperation
from app.db.models.task_batch import TaskBatch
from app.db.models.task_creation_operation import TaskCreationOperation
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.db.models.video_tracker_job import VideoTrackerJob
from app.services.training_dataset_bridge import content_digest
from tests.factory import create_project, create_task, create_user
from tests.sample_evaluation_fixtures import Objects, seed as seed_model


pytestmark = pytest.mark.asyncio

_SQL_ARTIFACT = (
    Path(__file__).parents[1] / "alembic" / "sql" / "0206_dataset_source_guards.sql"
)
_SCHEMA_ARTIFACT = (
    Path(__file__).parents[1]
    / "alembic"
    / "sql"
    / "0206_dataset_source_guards.schema.json"
)
_GUARD_LEARNING = text(
    "SELECT aap_learning_v1.guard_dataset_source_deletion("
    "CAST(:dataset_id AS uuid), CAST(:item_id AS uuid))"
)
_GUARD_LABELING = text(
    "SELECT aap_labeling_v1.guard_dataset_source_deletion("
    "CAST(:dataset_id AS uuid), CAST(:item_id AS uuid))"
)
_DETACH_ATTRIBUTION = text(
    "SELECT aap_labeling_v1.detach_deleted_dataset_attribution("
    "CAST(:dataset_id AS uuid))"
)


async def _seed_source(db, *, with_scene: bool = False):
    suffix = uuid.uuid4().hex[:12]
    owner = await create_user(
        db,
        "project_admin",
        f"source-guard-{suffix}@example.test",
        "Source guard owner",
    )
    project = await create_project(db, owner_id=owner.id)
    dataset = Dataset(
        id=uuid.uuid4(),
        display_id=f"SG-{suffix[:8]}",
        name=f"Source guard dataset {suffix}",
        data_type="image",
        created_by=owner.id,
    )
    scene = None
    db.add(dataset)
    if with_scene:
        scene = Scene(
            id=uuid.uuid4(),
            display_id=f"SC-{suffix[:8]}",
            dataset_id=dataset.id,
            name=f"Source guard scene {suffix}",
            created_by=owner.id,
        )
        db.add(scene)
    await db.flush()

    item = DatasetItem(
        id=uuid.uuid4(),
        dataset_id=dataset.id,
        file_name="source.png",
        file_path=f"source-guard/{suffix}.png",
        file_type="image",
        scene_id=scene.id if scene else None,
        frame_index=0 if scene else None,
    )
    db.add(item)
    await db.flush()
    return owner, project, dataset, item, scene


async def _guard(db, statement, dataset_id: uuid.UUID, item_id: uuid.UUID | None):
    return await db.scalar(
        statement,
        {"dataset_id": dataset_id, "item_id": item_id},
    )


def _clear() -> dict:
    return {
        "schema": "aap-source-retention/v1",
        "ok": True,
        "data": {},
    }


def _blocked(code: str = "source_in_use") -> dict:
    return {
        "schema": "aap-source-retention/v1",
        "ok": False,
        "error": {"code": code},
    }


async def test_source_guards_reject_missing_dataset_ids(db_session):
    for statement in (_GUARD_LEARNING, _GUARD_LABELING):
        result = await db_session.scalar(
            statement,
            {"dataset_id": None, "item_id": None},
        )
        assert result == _blocked("invalid_request")


@pytest.mark.parametrize("reference_kind", ["member", "candidate"])
async def test_learning_guard_checks_item_and_whole_dataset_references(
    db_session, reference_kind: str
):
    owner, project, dataset, item, _scene = await _seed_source(db_session)
    unrelated = Dataset(
        id=uuid.uuid4(),
        display_id=f"SG-{uuid.uuid4().hex[:8]}",
        name=f"Unrelated source {uuid.uuid4().hex}",
        data_type="image",
        created_by=owner.id,
    )
    db_session.add(unrelated)
    await db_session.flush()

    task = await create_task(db_session, project_id=project.id)
    task.dataset_item_id = item.id
    db_session.add(task)
    await db_session.flush()

    learning = await seed_model(db_session, owner, Objects())
    round_id = uuid.uuid4()
    spec = {"schema": 1, "pool": [], "samples": []}
    round_record = ActiveLearningRound(
        id=round_id,
        project_id=learning.project.id,
        creator_id=owner.id,
        model_version_id=learning.model.id,
        deployment_id=learning.deployment.id,
        request_id=uuid.uuid4(),
        request_digest=content_digest({"round_id": str(round_id)}),
        name="Source guard round",
        status="failed",
        spec=spec,
        spec_digest=content_digest(spec),
        authorization={},
    )
    db_session.add(round_record)
    await db_session.flush()

    if reference_kind == "member":
        db_session.add(
            ActiveLearningPoolMember(
                round_id=round_record.id,
                task_id=task.id,
                dataset_item_id=item.id,
            )
        )
    else:
        db_session.add(
            ActiveLearningCandidate(
                round_id=round_record.id,
                ordinal=0,
                task_id=task.id,
                dataset_item_id=item.id,
                request_id=uuid.uuid4(),
                status="failed",
            )
        )
    await db_session.flush()

    assert await _guard(db_session, _GUARD_LEARNING, dataset.id, item.id) == _blocked(
        "active_learning_reference"
    )
    assert await _guard(db_session, _GUARD_LEARNING, dataset.id, None) == _blocked(
        "active_learning_reference"
    )
    assert await _guard(db_session, _GUARD_LEARNING, unrelated.id, item.id) == _clear()


@pytest.mark.parametrize("reference_kind", ["direct_task", "non_primary_link"])
async def test_labeling_guard_preserves_cross_project_task_source_references(
    db_session, reference_kind: str
):
    owner, _project, dataset, item, _scene = await _seed_source(db_session)
    unrelated_dataset = Dataset(
        id=uuid.uuid4(),
        display_id=f"SG-{uuid.uuid4().hex[:8]}",
        name=f"Unrelated source {uuid.uuid4().hex}",
        data_type="image",
        created_by=owner.id,
    )
    db_session.add(unrelated_dataset)
    await db_session.flush()
    foreign_project = await create_project(db_session, owner_id=owner.id)
    task = await create_task(db_session, project_id=foreign_project.id)
    if reference_kind == "direct_task":
        task.dataset_item_id = item.id
        db_session.add(task)
    else:
        db_session.add(
            TaskDatasetItemLink(
                id=uuid.uuid4(),
                task_id=task.id,
                dataset_item_id=item.id,
                role="camera_front",
                sensor_name="front",
            )
        )
    await db_session.flush()

    assert await _guard(db_session, _GUARD_LABELING, dataset.id, item.id) == _blocked()
    assert (
        await _guard(db_session, _GUARD_LABELING, unrelated_dataset.id, item.id)
        == _clear()
    )


async def test_labeling_guard_preserves_sensor_annotation_item_reference(db_session):
    owner, project, dataset, item, scene = await _seed_source(
        db_session, with_scene=True
    )
    assert scene is not None
    task = await create_task(db_session, project_id=project.id)
    track_id = f"trk_{uuid.uuid4().hex}"
    track = SceneTrack(
        id=uuid.uuid4(),
        project_id=project.id,
        scene_id=scene.id,
        track_id=track_id,
        class_name="car",
        created_by=owner.id,
    )
    db_session.add_all([task, track])
    await db_session.flush()
    db_session.add(
        Annotation(
            id=uuid.uuid4(),
            task_id=task.id,
            project_id=project.id,
            class_name="car",
            geometry={"type": "bbox", "x": 0, "y": 0, "width": 1, "height": 1},
            annotation_type="bbox",
            track_id=track_id,
            scene_track_id=track.id,
            sensor_dataset_item_id=item.id,
            sensor_role="camera_front",
            sensor_visibility="visible",
            calibration_revision=1,
            calibration_digest="a" * 64,
        )
    )
    await db_session.flush()

    assert await _guard(db_session, _GUARD_LABELING, dataset.id, item.id) == _blocked()


@pytest.mark.parametrize(
    "reference_kind", ["scene_track", "scene_operation", "quality_issue", "quality_run"]
)
async def test_whole_dataset_guard_preserves_scene_and_quality_history(
    db_session, reference_kind: str
):
    owner, project, dataset, _item, scene = await _seed_source(
        db_session, with_scene=True
    )
    assert scene is not None
    if reference_kind == "scene_track":
        db_session.add(
            SceneTrack(
                id=uuid.uuid4(),
                project_id=project.id,
                scene_id=scene.id,
                track_id=f"trk_{uuid.uuid4().hex}",
                class_name="car",
                created_by=owner.id,
            )
        )
    elif reference_kind == "scene_operation":
        db_session.add(
            SceneTrackOperation(
                id=uuid.uuid4(),
                scene_id=scene.id,
                actor_id=owner.id,
                kind="mark_absent",
                idempotency_key=f"source-guard-{uuid.uuid4().hex}",
                request_digest="a" * 64,
                snapshot_token="b" * 64,
                source_revisions={},
                result_revisions={},
                before_state={},
                after_state={},
                inverse_payload={},
                response_json={},
            )
        )
    elif reference_kind == "quality_issue":
        db_session.add(
            PointCloudQualityIssue(
                id=uuid.uuid4(),
                project_id=project.id,
                scene_id=scene.id,
                source_versions={},
                code="source_guard_fixture",
                severity="warning",
                severity_rank=1,
                locator={},
                dedupe_key=uuid.uuid4().hex,
            )
        )
    else:
        db_session.add(
            PointCloudQualityRun(
                id=uuid.uuid4(),
                project_id=project.id,
                status="completed",
                scope_json={"scope": "scene_ids", "scene_ids": [str(scene.id)]},
                config_revision=1,
                config_digest="c" * 64,
                config_snapshot={},
                source_snapshot=[],
                source_snapshot_digest="d" * 64,
                singleflight_key=uuid.uuid4().hex,
            )
        )
    await db_session.flush()

    assert await _guard(db_session, _GUARD_LABELING, dataset.id, None) == _blocked()
    # Scene-owned evidence does not retain a different item in the dataset.
    assert (
        await _guard(db_session, _GUARD_LABELING, dataset.id, uuid.uuid4()) == _clear()
    )


@pytest.mark.parametrize("reference_kind", ["video_segment", "video_tracker"])
async def test_labeling_guard_preserves_video_item_ledgers(
    db_session, reference_kind: str
):
    owner, project, dataset, item, _scene = await _seed_source(db_session)
    if reference_kind == "video_segment":
        db_session.add(
            VideoSegment(
                id=uuid.uuid4(),
                dataset_item_id=item.id,
                segment_index=0,
                start_frame=0,
                end_frame=10,
                status="completed",
            )
        )
    else:
        task = await create_task(db_session, project_id=project.id)
        db_session.add(
            VideoTrackerJob(
                id=uuid.uuid4(),
                task_id=task.id,
                dataset_item_id=item.id,
                status="cancelled",
                model_key="fixture-model",
                direction="forward",
                from_frame=0,
                to_frame=10,
                event_channel=f"source-guard:{uuid.uuid4().hex}",
                created_by=owner.id,
            )
        )
    await db_session.flush()

    assert await _guard(db_session, _GUARD_LABELING, dataset.id, item.id) == _blocked()


@pytest.mark.parametrize("job_id_kind", ["synchronous", "missing_job"])
async def test_item_guard_fails_closed_for_active_current_task_creation(
    db_session, job_id_kind: str
):
    _owner, project, dataset, item, _scene = await _seed_source(db_session)
    association_id = uuid.uuid4()
    association = ProjectDataset(
        id=association_id,
        dataset_id=dataset.id,
        project_id=project.id,
    )
    operation = TaskCreationOperation(
        id=uuid.uuid4(),
        actor_id=uuid.uuid4(),
        dataset_id=dataset.id,
        project_id=project.id,
        association_id=association_id,
        job_id=None if job_id_kind == "synchronous" else uuid.uuid4(),
        mode="generic",
        chunk_size=100,
        request={"mode": "generic"},
        state="accepted",
    )
    db_session.add_all([association, operation])
    await db_session.flush()

    assert await _guard(db_session, _GUARD_LABELING, dataset.id, item.id) == _blocked(
        "creation_in_progress"
    )
    # Whole-dataset deletion explicitly revokes associations/jobs in its owner
    # coordinator, so this guard only protects an individual source item.
    assert await _guard(db_session, _GUARD_LABELING, dataset.id, None) == _clear()

    await db_session.delete(association)
    await db_session.flush()
    assert await _guard(db_session, _GUARD_LABELING, dataset.id, item.id) == _clear()


async def test_detach_deleted_dataset_attribution_preserves_batch_identity_and_counts(
    db_session,
):
    owner, project, dataset, _item, _scene = await _seed_source(db_session)
    other_dataset = Dataset(
        id=uuid.uuid4(),
        display_id=f"SG-{uuid.uuid4().hex[:8]}",
        name=f"Other attribution {uuid.uuid4().hex}",
        data_type="image",
        created_by=owner.id,
    )
    db_session.add(other_dataset)
    await db_session.flush()
    target_batch_id = uuid.uuid4()
    other_batch_id = uuid.uuid4()
    db_session.add_all(
        [
            TaskBatch(
                id=target_batch_id,
                project_id=project.id,
                dataset_id=dataset.id,
                display_id=f"SG-B-{uuid.uuid4().hex[:8]}",
                name="Retained target batch",
                status="completed",
                total_tasks=9,
                completed_tasks=6,
                review_tasks=3,
                approved_tasks=2,
                rejected_tasks=1,
            ),
            TaskBatch(
                id=other_batch_id,
                project_id=project.id,
                dataset_id=other_dataset.id,
                display_id=f"SG-B-{uuid.uuid4().hex[:8]}",
                name="Other dataset batch",
                status="reviewing",
                total_tasks=4,
                completed_tasks=2,
            ),
        ]
    )
    await db_session.flush()

    savepoint = await db_session.begin_nested()
    detached = await db_session.scalar(_DETACH_ATTRIBUTION, {"dataset_id": dataset.id})
    assert detached == 1
    await savepoint.rollback()

    target_after_rollback = (
        (
            await db_session.execute(
                text(
                    "SELECT dataset_id, total_tasks, completed_tasks, review_tasks, "
                    "approved_tasks, rejected_tasks, status FROM public.task_batches "
                    "WHERE id = CAST(:id AS uuid)"
                ),
                {"id": target_batch_id},
            )
        )
        .mappings()
        .one()
    )
    assert target_after_rollback["dataset_id"] == dataset.id
    assert target_after_rollback["total_tasks"] == 9
    assert target_after_rollback["completed_tasks"] == 6

    detached = await db_session.scalar(_DETACH_ATTRIBUTION, {"dataset_id": dataset.id})
    assert detached == 1
    rows = (
        (
            await db_session.execute(
                text(
                    "SELECT id, dataset_id, total_tasks, completed_tasks, review_tasks, "
                    "approved_tasks, rejected_tasks, status FROM public.task_batches "
                    "WHERE id IN (CAST(:target AS uuid), CAST(:other AS uuid)) ORDER BY id"
                ),
                {"target": target_batch_id, "other": other_batch_id},
            )
        )
        .mappings()
        .all()
    )
    by_id = {row["id"]: row for row in rows}
    assert by_id[target_batch_id]["dataset_id"] is None
    assert (
        by_id[target_batch_id]["total_tasks"],
        by_id[target_batch_id]["completed_tasks"],
        by_id[target_batch_id]["review_tasks"],
        by_id[target_batch_id]["approved_tasks"],
        by_id[target_batch_id]["rejected_tasks"],
        by_id[target_batch_id]["status"],
    ) == (9, 6, 3, 2, 1, "completed")
    assert by_id[other_batch_id]["dataset_id"] == other_dataset.id


async def test_frozen_source_guard_schema_lists_only_versioned_capabilities():
    schema = json.loads(_SCHEMA_ARTIFACT.read_text(encoding="utf-8"))
    sql = _SQL_ARTIFACT.read_text(encoding="utf-8")
    expected_functions = {
        "aap_learning_v1.guard_dataset_source_deletion(uuid, uuid)",
        "aap_labeling_v1.guard_dataset_source_deletion(uuid, uuid)",
        "aap_labeling_v1.detach_deleted_dataset_attribution(uuid)",
    }
    assert set(schema["x-postgresql-functions"]) == expected_functions
    for function in expected_functions:
        signature = function.removesuffix("(uuid, uuid)").removesuffix("(uuid)")
        assert signature in sql
    assert schema["$defs"]["learningFailure"]["properties"]["error"]["properties"][
        "code"
    ]["enum"] == ["invalid_request", "active_learning_reference"]
    assert schema["$defs"]["labelingFailure"]["properties"]["error"]["properties"][
        "code"
    ]["enum"] == [
        "invalid_request",
        "source_in_use",
        "creation_in_progress",
    ]
