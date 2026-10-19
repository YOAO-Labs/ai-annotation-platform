"""Database regressions for scene and auxiliary source-retention fences."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.dataset import Dataset
from app.db.models.point_cloud_quality import (
    PointCloudQualityIssue,
    PointCloudQualityRun,
)
from app.db.models.scene_track import SceneTrack, SceneTrackOperation
from app.db.models.task import Task
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.db.models.video_chapter import VideoChapter
from tests.test_assets_accepted_inputs_native import (
    _claim as _claim_image_input,
    _finish as _finish_image_input,
    _reserve as _reserve_image_input,
    _seed_workbench_input,
)
from tests.test_dataset_source_deletion import (
    _SCENE_FOR_UPDATE,
    _cleanup as _cleanup_source,
    _count,
    _delete as _delete_source,
    _seed as _seed_source_deletion,
)
from tests.test_dataset_source_guards import _seed_source
from tests.test_video_input_ledger_execution import (
    _FINISH_INPUT,
    _SEAL,
    _STAGE,
    _frame_plan,
    _seed_prepared_video_input,
    _video_data,
)

pytestmark = pytest.mark.asyncio

_AUXILIARY_GUARD = text(
    "SELECT aap_assets_v1.guard_dataset_source_auxiliary_retention("
    "CAST(:dataset_id AS uuid),CAST(:item_id AS uuid))"
)
_SCENE_PROJECTION = text(
    "SELECT scene_id FROM aap_assets_v1.dataset_item_scene_references("
    "CAST(:dataset_id AS uuid),CAST(:item_id AS uuid))"
)
_LABELING_GUARD = text(
    "SELECT aap_labeling_v1.guard_dataset_source_deletion("
    "CAST(:dataset_id AS uuid),CAST(:item_id AS uuid))"
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


async def _auxiliary_guard(db, dataset_id, item_id):
    return await db.scalar(
        _AUXILIARY_GUARD,
        {"dataset_id": dataset_id, "item_id": item_id},
    )


async def test_item_scene_history_blocks_delete_without_partial_writes_and_is_scoped(
    db_session,
):
    owner, project, dataset, item, scene = await _seed_source(
        db_session, with_scene=True
    )
    assert scene is not None
    track = SceneTrack(
        id=uuid.uuid4(),
        project_id=project.id,
        scene_id=scene.id,
        track_id=f"trk_{uuid.uuid4().hex}",
        class_name="car",
        created_by=owner.id,
    )
    db_session.add(track)
    unrelated_dataset = Dataset(
        id=uuid.uuid4(),
        display_id=f"DS-{uuid.uuid4().hex[:10]}",
        name="Unrelated scene owner",
        data_type="image",
        created_by=owner.id,
    )
    db_session.add(unrelated_dataset)
    await db_session.flush()

    projection = await db_session.scalars(
        _SCENE_PROJECTION,
        {"dataset_id": dataset.id, "item_id": item.id},
    )
    assert list(projection) == [scene.id]
    assert (
        list(
            await db_session.scalars(
                _SCENE_PROJECTION,
                {"dataset_id": unrelated_dataset.id, "item_id": item.id},
            )
        )
        == []
    )
    assert (
        await db_session.scalar(
            _LABELING_GUARD,
            {"dataset_id": unrelated_dataset.id, "item_id": item.id},
        )
        == _clear()
    )

    operation_id = uuid.uuid4()
    fixture = {
        "actor_id": owner.id,
        "dataset_id": dataset.id,
        "operation_ids": [],
    }
    reply = await _delete_source(db_session, fixture, operation_id, item_id=item.id)
    assert reply == {
        "schema": "aap-dataset-source-delete/v1",
        "ok": False,
        "error": {"code": "source_in_use"},
    }
    assert (
        await _count(
            db_session,
            "SELECT count(*) FROM public.dataset_items WHERE id=:id",
            id=item.id,
        )
        == 1
    )
    assert (
        await _count(
            db_session,
            "SELECT count(*) FROM public.scene_tracks WHERE id=:id",
            id=track.id,
        )
        == 1
    )
    assert (
        await _count(
            db_session,
            "SELECT count(*) FROM public.dataset_source_deletion_operations "
            "WHERE operation_id=:id",
            id=operation_id,
        )
        == 0
    )
    assert (
        await _count(
            db_session,
            "SELECT count(*) FROM public.audit_logs "
            "WHERE detail_json ->> 'operation_id'=:id",
            id=str(operation_id),
        )
        == 0
    )


@pytest.mark.parametrize(
    "reference_kind",
    ["scene_operation", "quality_issue", "quality_run", "camera_link", "video_chapter"],
)
async def test_other_auxiliary_item_references_return_source_in_use(
    db_session, reference_kind: str
):
    owner, project, dataset, item, scene = await _seed_source(
        db_session, with_scene=True
    )
    assert scene is not None
    if reference_kind == "scene_operation":
        db_session.add(
            SceneTrackOperation(
                id=uuid.uuid4(),
                scene_id=scene.id,
                actor_id=owner.id,
                kind="mark_absent",
                idempotency_key=f"source-retention-{uuid.uuid4().hex}",
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
                code="source_retention_fixture",
                severity="warning",
                severity_rank=1,
                locator={},
                dedupe_key=uuid.uuid4().hex,
            )
        )
    elif reference_kind == "quality_run":
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
    elif reference_kind == "camera_link":
        task = Task(
            id=uuid.uuid4(),
            project_id=project.id,
            dataset_item_id=None,
            display_id=f"CAM-{uuid.uuid4().hex[:10]}",
            file_name="camera-frame.png",
            file_path=f"camera/{uuid.uuid4().hex}.png",
            file_type="image",
        )
        db_session.add(task)
        db_session.add(
            TaskDatasetItemLink(
                id=uuid.uuid4(),
                task_id=task.id,
                dataset_item_id=item.id,
                role="camera_front",
                sensor_name="front",
            )
        )
    else:
        db_session.add(
            VideoChapter(
                id=uuid.uuid4(),
                dataset_item_id=item.id,
                start_frame=0,
                end_frame=10,
                title="Retained chapter",
                created_by=owner.id,
            )
        )
    await db_session.flush()

    operation_id = uuid.uuid4()
    fixture = {
        "actor_id": owner.id,
        "dataset_id": dataset.id,
        "operation_ids": [],
    }
    reply = await _delete_source(db_session, fixture, operation_id, item_id=item.id)
    assert reply["error"]["code"] == "source_in_use"
    assert (
        await db_session.scalar(
            text("SELECT count(*) FROM public.dataset_items WHERE id=:id"),
            {"id": item.id},
        )
        == 1
    )
    assert (
        await db_session.scalar(
            text(
                "SELECT count(*) FROM public.dataset_source_deletion_operations "
                "WHERE operation_id=:id"
            ),
            {"id": operation_id},
        )
        == 0
    )


async def test_scene_fence_busy_prevents_item_delete_before_any_receipt(test_engine):
    fixture = await _seed_source_deletion(
        test_engine, with_scene=True, with_pyramid=True
    )
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    holder = maker()
    operation_id = uuid.uuid4()
    try:
        await holder.begin()
        await holder.execute(_SCENE_FOR_UPDATE, {"scene_id": fixture["scene_ids"][0]})
        async with maker.begin() as deleter:
            reply = await _delete_source(
                deleter,
                fixture,
                operation_id,
                item_id=fixture["item_ids"][0],
            )
            assert reply["error"]["code"] == "source_busy"
        await holder.commit()

        async with maker() as verify:
            assert (
                await _count(
                    verify,
                    "SELECT count(*) FROM public.dataset_items WHERE id=:id",
                    id=fixture["item_ids"][0],
                )
                == 1
            )
            assert (
                await _count(
                    verify,
                    "SELECT count(*) FROM public.image_pyramid_assets WHERE id=:id",
                    id=fixture["asset_id"],
                )
                == 1
            )
            assert (
                await _count(
                    verify,
                    "SELECT count(*) FROM public.dataset_source_deletion_operations "
                    "WHERE operation_id=:id",
                    id=operation_id,
                )
                == 0
            )
            assert (
                await _count(
                    verify,
                    "SELECT count(*) FROM public.asset_storage_deletions "
                    "WHERE operation_id=:id",
                    id=operation_id,
                )
                == 0
            )
    finally:
        if holder.in_transaction():
            await holder.rollback()
        await holder.close()
        await _cleanup_source(test_engine, fixture)


@pytest.mark.parametrize(
    "job_kind", ["video_metadata", "backfill_dataset", "backfill_items", "backfill_all"]
)
async def test_active_media_job_rejects_delete_without_canceling_or_writing_receipts(
    test_engine, job_kind: str
):
    fixture = await _seed_source_deletion(test_engine, with_pyramid=True)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    operation_id = uuid.uuid4()
    try:
        async with maker.begin() as setup:
            await setup.execute(
                text("UPDATE dataset_items SET file_type='video' WHERE id=:id"),
                {"id": fixture["item_ids"][0]},
            )
            if job_kind == "video_metadata":
                kind = "video_metadata"
                dataset_item_id = fixture["item_ids"][0]
                spec = {"kind": "video_metadata"}
            elif job_kind == "backfill_dataset":
                kind = "video_backfill"
                dataset_item_id = None
                spec = {
                    "kind": "video_backfill",
                    "selection": {"dataset_id": str(fixture["dataset_id"])},
                }
            elif job_kind == "backfill_items":
                kind = "video_backfill"
                dataset_item_id = None
                spec = {
                    "kind": "video_backfill",
                    "selection": {"dataset_item_ids": [str(fixture["item_ids"][0])]},
                }
            else:
                kind = "video_backfill"
                dataset_item_id = None
                spec = {"kind": "video_backfill", "selection": {"all_items": True}}
            await setup.execute(
                text(
                    "INSERT INTO public.asset_media_jobs("
                    "job_id,kind,dataset_item_id,slot_key,spec,status,"
                    "backfill_selection,backfill_cutoff) VALUES ("
                    "CAST(:job AS uuid),:kind,CAST(:item AS uuid),:slot,"
                    "CAST(:spec AS jsonb),'pending',CAST(:selection AS jsonb),"
                    "CASE WHEN :kind='video_backfill' "
                    "THEN pg_catalog.clock_timestamp() END)"
                ),
                {
                    "job": fixture["job_id"],
                    "kind": kind,
                    "item": dataset_item_id,
                    "slot": f"retention-test-{job_kind}",
                    "spec": json.dumps(spec),
                    "selection": (
                        json.dumps(spec["selection"])
                        if kind == "video_backfill"
                        else None
                    ),
                },
            )
        async with maker.begin() as deleter:
            reply = await _delete_source(
                deleter,
                fixture,
                operation_id,
                item_id=fixture["item_ids"][0],
            )
            assert reply["error"]["code"] == "source_in_use"
        async with maker() as verify:
            assert (
                await _count(
                    verify,
                    "SELECT count(*) FROM public.dataset_items WHERE id=:id",
                    id=fixture["item_ids"][0],
                )
                == 1
            )
            assert (
                await verify.scalar(
                    text("SELECT status FROM public.asset_media_jobs WHERE job_id=:id"),
                    {"id": fixture["job_id"]},
                )
                == "pending"
            )
            assert (
                await _count(
                    verify,
                    "SELECT count(*) FROM public.image_pyramid_assets WHERE id=:id",
                    id=fixture["asset_id"],
                )
                == 1
            )
            assert (
                await _count(
                    verify,
                    "SELECT count(*) FROM public.dataset_source_deletion_operations "
                    "WHERE operation_id=:id",
                    id=operation_id,
                )
                == 0
            )
            assert (
                await _count(
                    verify,
                    "SELECT count(*) FROM public.asset_storage_deletions "
                    "WHERE operation_id=:id",
                    id=operation_id,
                )
                == 0
            )
    finally:
        async with maker.begin() as cleanup:
            await cleanup.execute(
                text("DELETE FROM public.asset_media_jobs WHERE job_id=:job"),
                {"job": fixture["job_id"]},
            )
        await _cleanup_source(test_engine, fixture)


@pytest.mark.parametrize("selection", ["all", "dataset", "items"])
async def test_backfill_retains_only_videos_within_its_frozen_cutoff(
    db_session, selection
):
    _owner, _project, dataset, item, _scene = await _seed_source(db_session)
    selector = (
        {"all_items": True}
        if selection == "all"
        else {"dataset_id": str(dataset.id)}
        if selection == "dataset"
        else {"dataset_item_ids": [str(item.id)]}
    )
    job_id = uuid.uuid4()
    await db_session.execute(
        text(
            "INSERT INTO asset_media_jobs(job_id,kind,slot_key,spec,status,backfill_selection,backfill_cutoff) "
            "VALUES (:job,'video_backfill',:slot,CAST(:spec AS jsonb),'pending',CAST(:selection AS jsonb),clock_timestamp()-interval '1 hour')"
        ),
        {
            "job": job_id,
            "slot": str(job_id),
            "spec": json.dumps({"kind": "video_backfill", "selection": selector}),
            "selection": json.dumps(selector),
        },
    )
    await db_session.execute(
        text("UPDATE dataset_items SET file_type='video' WHERE id=:id"), {"id": item.id}
    )
    assert await _auxiliary_guard(db_session, dataset.id, item.id) == _clear()
    await db_session.execute(
        text(
            "UPDATE dataset_items SET created_at=(SELECT backfill_cutoff-interval '1 second' FROM asset_media_jobs WHERE job_id=:job) WHERE id=:id"
        ),
        {"id": item.id, "job": job_id},
    )
    assert await _auxiliary_guard(db_session, dataset.id, item.id) == _blocked()
    await db_session.execute(
        text("UPDATE dataset_items SET file_type='image' WHERE id=:id"), {"id": item.id}
    )
    assert await _auxiliary_guard(db_session, dataset.id, item.id) == _clear()


async def test_image_input_remains_protected_until_its_authoritative_expiry(db_session):
    (
        actor,
        _project,
        dataset_id,
        item_id,
        task,
        job_id,
        source,
    ) = await _seed_workbench_input(db_session)
    accepted = await _reserve_image_input(db_session, actor, task, job_id, source)
    claim = await _claim_image_input(db_session, accepted["input_id"])
    assert await _auxiliary_guard(db_session, dataset_id, item_id) == _blocked()

    ready = await _finish_image_input(db_session, claim)
    assert ready["status"] == "ready"
    await db_session.execute(
        text("UPDATE public.async_jobs SET status='completed' WHERE id=:job"),
        {"job": job_id},
    )
    assert await _auxiliary_guard(db_session, dataset_id, item_id) == _blocked()

    await db_session.execute(
        text(
            "UPDATE public.asset_accepted_image_inputs "
            "SET retire_after=pg_catalog.clock_timestamp()-interval '1 second' "
            "WHERE input_id=CAST(:input AS uuid)"
        ),
        {"input": accepted["input_id"]},
    )
    sweep = await db_session.scalar(
        text("SELECT aap_assets_v1.sweep_accepted_image_inputs(100)")
    )
    assert sweep["ok"] is True
    assert (
        await db_session.scalar(
            text(
                "SELECT status FROM public.asset_accepted_image_inputs "
                "WHERE input_id=CAST(:input AS uuid)"
            ),
            {"input": accepted["input_id"]},
        )
        == "expired"
    )
    assert await _auxiliary_guard(db_session, dataset_id, item_id) == _clear()


async def test_video_input_manifest_and_receipt_expiry_allow_unreferenced_retry(
    db_session,
):
    (
        actor,
        project,
        job_id,
        selection,
        header,
        input_id,
        claim,
    ) = await _seed_prepared_video_input(db_session)
    source = (
        await db_session.execute(
            text(
                "SELECT source->>'dataset_id',task->>'dataset_item_id',task_id "
                "FROM public.asset_accepted_video_inputs WHERE input_id=:id"
            ),
            {"id": input_id},
        )
    ).one()
    dataset_id, item_id, task_id = uuid.UUID(source[0]), uuid.UUID(source[1]), source[2]
    assert await _auxiliary_guard(db_session, dataset_id, item_id) == _blocked()

    plan, entries = _frame_plan(claim, selection)
    staged = _video_data(
        await db_session.scalar(
            _STAGE,
            {
                "input": input_id,
                "generation": claim["generation"],
                "token": claim["lease_token"],
                "cursor": 0,
                "entries": json.dumps(entries),
            },
        )
    )
    assert staged["next_cursor"] == len(entries)
    ready = _video_data(
        await db_session.scalar(
            _FINISH_INPUT,
            {
                "input": input_id,
                "generation": claim["generation"],
                "token": claim["lease_token"],
                "plan": json.dumps(plan),
            },
        )
    )
    assert ready["status"] == "ready"
    sealed = _video_data(
        await db_session.scalar(
            _SEAL,
            {
                "job": job_id,
                "actor": actor,
                "project": project.id,
                "selection_digest": header["selection_digest"],
            },
        )
    )
    assert sealed["status"] == "sealed"
    await db_session.execute(
        text("UPDATE public.async_jobs SET status='completed' WHERE id=:job"),
        {"job": job_id},
    )
    assert await _auxiliary_guard(db_session, dataset_id, item_id) == _blocked()

    await db_session.execute(
        text(
            "UPDATE public.asset_accepted_video_inputs "
            "SET retire_after=pg_catalog.clock_timestamp()-interval '1 second' "
            "WHERE input_id=CAST(:input AS uuid)"
        ),
        {"input": input_id},
    )
    sweep = _video_data(
        await db_session.scalar(
            text("SELECT aap_assets_v1.sweep_accepted_video_inputs(100)")
        )
    )
    assert sweep["expired"] == 1
    assert await _auxiliary_guard(db_session, dataset_id, item_id) == _clear()

    # The immutable sealed manifest remains as history, but the expired input
    # receipt alone no longer prevents deleting its unreferenced source Item.
    await db_session.execute(
        text("UPDATE public.tasks SET dataset_item_id=NULL WHERE id=:task"),
        {"task": task_id},
    )
    fixture = {
        "actor_id": actor,
        "dataset_id": dataset_id,
        "operation_ids": [],
    }
    reply = await _delete_source(db_session, fixture, uuid.uuid4(), item_id=item_id)
    assert reply["ok"] is True
