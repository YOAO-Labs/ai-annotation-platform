"""Owned-Postgres regressions for auxiliary source-reference retention."""

from __future__ import annotations

from datetime import UTC, datetime
import os
import uuid

import pytest
from sqlalchemy import text

from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.scene_track import SceneTrack
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from tests.factory import create_task
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_dataset_source_deletion import _count, _delete as _delete_source
from tests.test_dataset_source_guards import _seed_source
from tests.test_video_execution_checkpoints_native import _claimed_running_job

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the verifier-owned module-data database",
    ),
]

_LABELING_GUARD = text(
    "SELECT aap_labeling_v1.guard_dataset_source_deletion("
    "CAST(:dataset_id AS uuid), CAST(:item_id AS uuid))"
)
_VIDEO_SOURCE_RETAINED = text(
    "SELECT aap_jobs_v1.video_execution_source_item_retained(CAST(:item_id AS uuid))"
)
_VIDEO_INPUT_RETAINED = text(
    "SELECT aap_jobs_v1.video_execution_input_retained(CAST(:input_id AS uuid))"
)


def _clear() -> dict:
    return {"schema": "aap-source-retention/v1", "ok": True, "data": {}}


def _blocked() -> dict:
    return {
        "schema": "aap-source-retention/v1",
        "ok": False,
        "error": {"code": "source_in_use"},
    }


def _deletion_fixture(actor_id, dataset_id) -> dict[str, object]:
    return {"actor_id": actor_id, "dataset_id": dataset_id, "operation_ids": []}


async def _assert_no_delete_effects(db, *, dataset_id, item_id, operation_id) -> None:
    assert (
        await _count(
            db,
            "SELECT count(*) FROM public.dataset_items WHERE id=:id AND dataset_id=:dataset",
            id=item_id,
            dataset=dataset_id,
        )
        == 1
    )
    assert (
        await _count(
            db,
            "SELECT count(*) FROM public.dataset_source_deletion_operations "
            "WHERE operation_id=:id",
            id=operation_id,
        )
        == 0
    )
    assert (
        await _count(
            db,
            "SELECT count(*) FROM public.asset_storage_deletions WHERE operation_id=:id",
            id=operation_id,
        )
        == 0
    )
    assert (
        await _count(
            db,
            "SELECT count(*) FROM public.audit_logs "
            "WHERE detail_json ->> 'operation_id'=:id",
            id=str(operation_id),
        )
        == 0
    )


@pytest.mark.parametrize("delete_item", [True, False], ids=["item", "dataset"])
async def test_secondary_camera_link_blocks_delete_with_primary_in_another_dataset(
    test_engine, db_session, delete_item: bool
):
    _verify_postgres(test_engine.url)
    owner, project, dataset, camera_item, _scene = await _seed_source(db_session)

    primary_dataset = Dataset(
        id=uuid.uuid4(),
        display_id=f"DS-{uuid.uuid4().hex[:10]}",
        name=f"Primary source {uuid.uuid4().hex[:8]}",
        data_type="point_cloud",
        created_by=owner.id,
    )
    db_session.add(primary_dataset)
    await db_session.flush()
    primary_item = DatasetItem(
        id=uuid.uuid4(),
        dataset_id=primary_dataset.id,
        file_name="primary.pcd",
        file_path=f"primary/{uuid.uuid4().hex}.pcd",
        file_type="point_cloud",
    )
    db_session.add(primary_item)
    await db_session.flush()

    task = await create_task(db_session, project_id=project.id)
    task.dataset_item_id = primary_item.id
    task.file_name = primary_item.file_name
    task.file_path = primary_item.file_path
    task.file_type = "point_cloud"
    link = TaskDatasetItemLink(
        id=uuid.uuid4(),
        task_id=task.id,
        dataset_item_id=camera_item.id,
        role="camera_front",
        sensor_name="front",
    )
    db_session.add(link)
    await db_session.flush()

    assert task.dataset_item_id != camera_item.id
    operation_id = uuid.uuid4()
    reply = await _delete_source(
        db_session,
        _deletion_fixture(owner.id, dataset.id),
        operation_id,
        item_id=camera_item.id if delete_item else None,
    )
    assert reply == {
        "schema": "aap-dataset-source-delete/v1",
        "ok": False,
        "error": {"code": "source_in_use"},
    }
    assert (
        await _count(
            db_session,
            "SELECT count(*) FROM public.tasks WHERE dataset_item_id=:item",
            item=camera_item.id,
        )
        == 0
    )
    assert (
        await _count(
            db_session,
            "SELECT count(*) FROM public.tasks WHERE id=:task AND dataset_item_id=:item",
            task=task.id,
            item=primary_item.id,
        )
        == 1
    )
    assert (
        await _count(
            db_session,
            "SELECT count(*) FROM public.task_dataset_item_links WHERE id=:link AND dataset_item_id=:item",
            link=link.id,
            item=camera_item.id,
        )
        == 1
    )
    await _assert_no_delete_effects(
        db_session,
        dataset_id=dataset.id,
        item_id=camera_item.id,
        operation_id=operation_id,
    )


@pytest.mark.parametrize("retired", [False, True], ids=["active", "retired-history"])
@pytest.mark.parametrize("delete_item", [True, False], ids=["item", "dataset"])
async def test_scene_track_history_blocks_source_item_delete(
    test_engine, db_session, retired: bool, delete_item: bool
):
    _verify_postgres(test_engine.url)
    owner, project, dataset, item, scene = await _seed_source(
        db_session, with_scene=True
    )
    assert scene is not None
    track = SceneTrack(
        id=uuid.uuid4(),
        project_id=project.id,
        scene_id=scene.id,
        track_id=f"track-{uuid.uuid4().hex[:12]}",
        class_name="car",
        created_by=owner.id,
        retired_at=datetime.now(UTC) if retired else None,
    )
    db_session.add(track)
    await db_session.flush()

    operation_id = uuid.uuid4()
    reply = await _delete_source(
        db_session,
        _deletion_fixture(owner.id, dataset.id),
        operation_id,
        item_id=item.id if delete_item else None,
    )
    assert reply == {
        "schema": "aap-dataset-source-delete/v1",
        "ok": False,
        "error": {"code": "source_in_use"},
    }
    assert (
        await _count(
            db_session,
            "SELECT count(*) FROM public.scene_tracks WHERE id=:track AND scene_id=:scene",
            track=track.id,
            scene=scene.id,
        )
        == 1
    )
    await _assert_no_delete_effects(
        db_session,
        dataset_id=dataset.id,
        item_id=item.id,
        operation_id=operation_id,
    )


@pytest.mark.parametrize(
    "execution_retired", [False, True], ids=["unretired-pin", "retired-history"]
)
@pytest.mark.parametrize("delete_item", [True, False], ids=["item", "dataset"])
async def test_video_execution_source_pin_blocks_until_journal_retirement(
    test_engine, db_session, execution_retired: bool, delete_item: bool
):
    _verify_postgres(test_engine.url)
    job, input_id, _claim = await _claimed_running_job(db_session)
    item_id = job.dataset_item_id
    task_id = job.task_id
    actor_id = job.created_by
    dataset_id = await db_session.scalar(
        text("SELECT dataset_id FROM public.dataset_items WHERE id=:item"),
        {"item": item_id},
    )

    if execution_retired:
        job.status = "failed"
        await db_session.flush()

    journal = (
        await db_session.execute(
            text(
                "SELECT source_item_id, input_id, execution_retired "
                "FROM aap_jobs_v1.video_execution_journals WHERE job_id=:job"
            ),
            {"job": job.id},
        )
    ).one()
    assert journal.source_item_id == item_id
    assert journal.input_id == input_id
    assert journal.execution_retired is execution_retired
    assert (await db_session.scalar(_VIDEO_SOURCE_RETAINED, {"item_id": item_id})) is (
        not execution_retired
    )
    assert (await db_session.scalar(_VIDEO_INPUT_RETAINED, {"input_id": input_id})) is (
        not execution_retired
    )

    # Remove the canonical task/job rows so the durable Jobs journal is the
    # remaining source reference tested by the delete guard.
    await db_session.execute(
        text("DELETE FROM public.tasks WHERE id=:task"), {"task": task_id}
    )
    assert (
        await _count(
            db_session,
            "SELECT count(*) FROM public.video_tracker_jobs WHERE id=:job",
            job=job.id,
        )
        == 0
    )
    assert (await db_session.scalar(_VIDEO_SOURCE_RETAINED, {"item_id": item_id})) is (
        not execution_retired
    )

    labeling = await db_session.scalar(
        _LABELING_GUARD, {"dataset_id": dataset_id, "item_id": item_id}
    )
    if execution_retired:
        assert labeling == _clear()
    else:
        assert labeling == _blocked()

    operation_id = uuid.uuid4()
    reply = await _delete_source(
        db_session,
        _deletion_fixture(actor_id, dataset_id),
        operation_id,
        item_id=item_id if delete_item else None,
    )
    if execution_retired:
        assert reply["schema"] == "aap-dataset-source-delete/v1"
        assert reply["ok"] is True
        assert reply["data"]["deleted_items"] == 1
        assert (
            await _count(
                db_session,
                "SELECT count(*) FROM public.dataset_items WHERE id=:item",
                item=item_id,
            )
            == 0
        )
    else:
        assert reply == {
            "schema": "aap-dataset-source-delete/v1",
            "ok": False,
            "error": {"code": "source_in_use"},
        }
        await _assert_no_delete_effects(
            db_session,
            dataset_id=dataset_id,
            item_id=item_id,
            operation_id=operation_id,
        )
