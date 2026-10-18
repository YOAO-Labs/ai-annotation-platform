from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import bindparam, delete, func, select, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.async_job import AsyncJob
from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_append_operation import TaskAppendOperation
from app.db.models.task_creation_chunk import TaskCreationChunk
from app.db.models.task_creation_operation import TaskCreationOperation
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.db.models.user import User


_ASSOCIATE = text(
    """
    SELECT aap_assets_v1.associate_project(
        CAST(:actor_id AS uuid),
        CAST(:dataset_id AS uuid),
        CAST(:project_id AS uuid)
    )
    """
)
_ACCEPT = text(
    """
    SELECT aap_labeling_v1.accept_dataset_task_creation(
        CAST(:operation_id AS uuid),
        CAST(:actor_id AS uuid),
        CAST(:dataset_id AS uuid),
        CAST(:project_id AS uuid),
        CAST(:association_id AS uuid),
        CAST(:job_id AS uuid),
        CAST(:mode AS text),
        CAST(:chunk_size AS integer)
    )
    """
)
_PREPARE = text(
    """
    SELECT aap_labeling_v1.prepare_dataset_task_creation(
        CAST(:operation_id AS uuid), CAST(:frames AS jsonb)
    )
    """
)
_READ = text(
    """
    SELECT aap_labeling_v1.read_dataset_task_creation(
        CAST(:operation_id AS uuid)
    )
    """
)
_ADVANCE = text(
    """
    SELECT aap_labeling_v1.advance_dataset_task_creation(
        CAST(:operation_id AS uuid), CAST(:chunk_index AS integer)
    )
    """
)
_APPEND = text(
    """
    SELECT aap_labeling_v1.append_dataset_items(
        CAST(:dataset_id AS uuid), CAST(:item_ids AS uuid[]),
        CAST(:operation_id AS uuid)
    )
    """
).bindparams(
    bindparam("dataset_id", type_=UUID(as_uuid=True)),
    bindparam("item_ids", type_=ARRAY(UUID(as_uuid=True))),
    bindparam("operation_id", type_=UUID(as_uuid=True)),
)


async def _associate(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID,
    dataset_id: uuid.UUID,
    project_id: uuid.UUID,
) -> uuid.UUID:
    response = (
        await db.execute(
            _ASSOCIATE,
            {
                "actor_id": actor_id,
                "dataset_id": dataset_id,
                "project_id": project_id,
            },
        )
    ).scalar_one()
    assert response["schema"] == "aap-dataset-association/v1"
    assert response["ok"] is True
    return uuid.UUID(response["data"]["link_id"])


async def _accept(
    db: AsyncSession,
    *,
    operation_id: uuid.UUID,
    actor_id: uuid.UUID,
    dataset_id: uuid.UUID,
    project_id: uuid.UUID,
    association_id: uuid.UUID,
    job_id: uuid.UUID | None = None,
    mode: str = "generic",
    chunk_size: int = 5000,
) -> dict:
    return (
        await db.execute(
            _ACCEPT,
            {
                "operation_id": operation_id,
                "actor_id": actor_id,
                "dataset_id": dataset_id,
                "project_id": project_id,
                "association_id": association_id,
                "job_id": job_id,
                "mode": mode,
                "chunk_size": chunk_size,
            },
        )
    ).scalar_one()


async def _prepare(
    db: AsyncSession, operation_id: uuid.UUID, frames: list[dict] | None
) -> dict:
    return (
        await db.execute(
            _PREPARE,
            {
                "operation_id": operation_id,
                "frames": json.dumps(frames) if frames is not None else None,
            },
        )
    ).scalar_one()


async def _read(db: AsyncSession, operation_id: uuid.UUID) -> dict:
    return (await db.execute(_READ, {"operation_id": operation_id})).scalar_one()


async def _advance(db: AsyncSession, operation_id: uuid.UUID, chunk_index: int) -> dict:
    return (
        await db.execute(
            _ADVANCE,
            {"operation_id": operation_id, "chunk_index": chunk_index},
        )
    ).scalar_one()


async def _seed_uncommitted(
    db: AsyncSession,
    *,
    owner_id: uuid.UUID,
    item_count: int = 1,
    mode: str = "generic",
) -> SimpleNamespace:
    suffix = uuid.uuid4().hex[:10].upper()
    media = "lidar" if mode == "pointcloud" else "image"
    dataset = Dataset(
        id=uuid.uuid4(),
        display_id=f"D-TC-{suffix}",
        name=f"task-create-{suffix}",
        data_type="point_cloud" if mode == "pointcloud" else "image",
        created_by=owner_id,
        file_count=item_count,
    )
    project = Project(
        id=uuid.uuid4(),
        display_id=f"P-TC-{suffix}",
        name=f"task create {suffix}",
        type_label="点云检测" if mode == "pointcloud" else "图像检测",
        type_key=media,
        data_type=media,
        scene_mode=False,
        owner_id=owner_id,
        total_tasks=0,
    )
    db.add_all([dataset, project])
    await db.flush()

    items = []
    for index in range(item_count):
        if mode == "pointcloud":
            file_name = f"{index:06d}.pcd"
            file_path = f"{dataset.name}/lidar/{file_name}"
            file_type = "other" if index == 0 else "point_cloud"
        else:
            file_name = f"image-{index:04d}.jpg"
            file_path = f"{dataset.name}/{file_name}"
            file_type = "image"
        item = DatasetItem(
            id=uuid.uuid4(),
            dataset_id=dataset.id,
            file_name=file_name,
            file_path=file_path,
            file_type=file_type,
        )
        items.append(item)
    db.add_all(items)
    await db.flush()

    association_id = await _associate(
        db,
        actor_id=owner_id,
        dataset_id=dataset.id,
        project_id=project.id,
    )
    return SimpleNamespace(
        actor_id=owner_id,
        dataset_id=dataset.id,
        project_id=project.id,
        association_id=association_id,
        items=items,
        mode=mode,
    )


async def _add_job(
    db: AsyncSession, state: SimpleNamespace, operation_id: uuid.UUID
) -> uuid.UUID:
    job_id = uuid.uuid4()
    db.add(
        AsyncJob(
            id=job_id,
            kind="create_tasks",
            user_id=state.actor_id,
            project_id=state.project_id,
            status="pending",
            payload={
                "schema": "aap-create-tasks/v1",
                "actor_id": str(state.actor_id),
                "dataset_id": str(state.dataset_id),
                "project_id": str(state.project_id),
                "association_id": str(state.association_id),
                "operation_id": str(operation_id),
                "mode": "link",
            },
            result={},
        )
    )
    await db.flush()
    return job_id


async def _seed_committed(test_engine, *, item_count: int = 1) -> dict:
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:10]
    actor_id = uuid.uuid4()
    state: SimpleNamespace
    async with maker() as db:
        db.add(
            User(
                id=actor_id,
                email=f"task-create-{suffix}@test.local",
                name=f"task create {suffix}",
                password_hash="test-only",
                role="super_admin",
                status="offline",
                is_active=True,
            )
        )
        await db.flush()
        state = await _seed_uncommitted(db, owner_id=actor_id, item_count=item_count)
        await db.commit()
    return {
        "maker": maker,
        "actor_id": actor_id,
        "dataset_id": state.dataset_id,
        "project_id": state.project_id,
        "association_id": state.association_id,
        "item_ids": [item.id for item in state.items],
    }


async def _cleanup(
    test_engine, fixture: dict, operation_ids: list[uuid.UUID], job_ids: list[uuid.UUID]
):
    maker = fixture["maker"]
    async with maker() as db:
        if operation_ids:
            await db.execute(
                delete(TaskCreationOperation).where(
                    TaskCreationOperation.id.in_(operation_ids)
                )
            )
            await db.execute(
                delete(TaskAppendOperation).where(
                    TaskAppendOperation.operation_id.in_(operation_ids)
                )
            )
        if job_ids:
            await db.execute(delete(AsyncJob).where(AsyncJob.id.in_(job_ids)))
        await db.execute(delete(Task).where(Task.project_id == fixture["project_id"]))
        await db.execute(
            delete(ProjectDataset).where(
                ProjectDataset.dataset_id == fixture["dataset_id"]
            )
        )
        await db.execute(
            delete(DatasetItem).where(DatasetItem.dataset_id == fixture["dataset_id"])
        )
        await db.execute(delete(Dataset).where(Dataset.id == fixture["dataset_id"]))
        await db.execute(delete(Project).where(Project.id == fixture["project_id"]))
        await db.execute(delete(User).where(User.id == fixture["actor_id"]))
        await db.commit()


@pytest.mark.asyncio
async def test_generic_accept_prepare_and_chunk_receipt_survive_client_restart(
    db_session: AsyncSession, super_admin
):
    actor, _ = super_admin
    state = await _seed_uncommitted(db_session, owner_id=actor.id, item_count=3)
    operation_id = uuid.uuid4()
    job_id = await _add_job(db_session, state, operation_id)
    accepted = await _accept(
        db_session,
        operation_id=operation_id,
        actor_id=state.actor_id,
        dataset_id=state.dataset_id,
        project_id=state.project_id,
        association_id=state.association_id,
        job_id=job_id,
        chunk_size=2,
    )
    assert accepted["schema"] == "aap-task-creation/v1"
    assert accepted["ok"] is True
    assert accepted["data"]["state"] == "accepted"
    assert accepted["data"]["total"] is None

    replayed_accept = await _accept(
        db_session,
        operation_id=operation_id,
        actor_id=state.actor_id,
        dataset_id=state.dataset_id,
        project_id=state.project_id,
        association_id=state.association_id,
        job_id=job_id,
        chunk_size=2,
    )
    assert replayed_accept == accepted
    conflict = await _accept(
        db_session,
        operation_id=operation_id,
        actor_id=state.actor_id,
        dataset_id=state.dataset_id,
        project_id=state.project_id,
        association_id=state.association_id,
        job_id=job_id,
        chunk_size=1,
    )
    assert conflict["ok"] is False
    assert conflict["error"]["code"] == "operation_conflict"

    prepared = await _prepare(db_session, operation_id, None)
    assert prepared["ok"] is True
    assert prepared["data"]["state"] == "running"
    assert prepared["data"]["total"] == 3
    # A prepared snapshot is frozen; a retry cannot replace it with new input.
    assert await _prepare(db_session, operation_id, {}) == prepared

    first = await _advance(db_session, operation_id, 0)
    assert first["schema"] == "aap-task-creation-chunk/v1"
    assert first["ok"] is True
    assert first["data"]["project_id"] == str(state.project_id)
    assert first["data"]["created"] == 2
    assert first["data"]["processed"] == 2
    assert first["data"]["cumulative_created"] == 2
    assert first["data"]["cumulative_processed"] == 2
    assert first["data"]["total"] == 3
    assert first["data"]["next_chunk"] == 1
    assert first["data"]["complete"] is False
    assert first["data"]["project_total"] == 2
    assert (await db_session.get(AsyncJob, job_id)).progress_pct >= 65
    assert await _advance(db_session, operation_id, 0) == first

    summary = await _read(db_session, operation_id)
    assert summary["ok"] is True
    assert summary["data"]["next_chunk"] == 1
    assert summary["data"]["processed"] == 2

    last = await _advance(db_session, operation_id, 1)
    assert last["data"]["created"] == 1
    assert last["data"]["cumulative_created"] == 3
    assert last["data"]["cumulative_processed"] == 3
    assert last["data"]["complete"] is True
    assert last["data"]["project_total"] == 3
    assert (await db_session.get(AsyncJob, job_id)).progress_pct == 100
    assert await _advance(db_session, operation_id, 1) == last

    tasks = (
        (
            await db_session.execute(
                select(Task).where(Task.project_id == state.project_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(tasks) == 3
    assert all(
        task.status == "pending"
        and task.tags == []
        and task.assignee_is_override is False
        and task.is_labeled is False
        and task.overlap == 1
        and task.total_annotations == 0
        and task.total_predictions == 0
        and task.reviewer_is_override is False
        and task.first_review_eligible is True
        and task.annotation_contributor_ids == []
        and task.reopened_count == 0
        and task.batch_id is None
        and task.version == 1
        for task in tasks
    )
    assert (
        await db_session.scalar(
            select(Project.total_tasks).where(Project.id == state.project_id)
        )
        == 3
    )
    operation = await db_session.get(TaskCreationOperation, operation_id)
    assert operation is not None
    assert operation.state == "completed"
    assert operation.total == 3
    assert operation.next_chunk == 2
    assert operation.processed == 3
    assert operation.created == 3
    assert await db_session.get(TaskCreationChunk, (operation_id, 0)) is not None
    assert await db_session.get(TaskCreationChunk, (operation_id, 1)) is not None


@pytest.mark.asyncio
async def test_pointcloud_chunk_creates_primary_and_camera_links(
    db_session, super_admin
):
    actor, _ = super_admin
    state = await _seed_uncommitted(
        db_session, owner_id=actor.id, item_count=3, mode="pointcloud"
    )
    operation_id = uuid.uuid4()
    await _accept(
        db_session,
        operation_id=operation_id,
        actor_id=state.actor_id,
        dataset_id=state.dataset_id,
        project_id=state.project_id,
        association_id=state.association_id,
        mode="pointcloud",
    )
    frame = {
        "primary_item_id": str(state.items[0].id),
        "cameras": [
            {"item_id": str(state.items[1].id), "sensor_name": "front"},
            {"item_id": str(state.items[2].id), "sensor_name": "left"},
        ],
    }
    prepared = await _prepare(db_session, operation_id, [frame])
    assert prepared["ok"] is True
    assert prepared["data"]["total"] == 1
    result = await _advance(db_session, operation_id, 0)
    assert result["ok"] is True
    assert result["data"]["created"] == 1
    task = await db_session.scalar(
        select(Task).where(
            Task.project_id == state.project_id,
            Task.dataset_item_id == state.items[0].id,
        )
    )
    assert task is not None
    assert task.file_type == "point_cloud"
    assert task.file_path == state.items[0].file_path
    links = (
        (
            await db_session.execute(
                select(TaskDatasetItemLink).where(
                    TaskDatasetItemLink.task_id == task.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert {(link.role, link.dataset_item_id, link.sensor_name) for link in links} == {
        ("primary_lidar", state.items[0].id, None),
        ("camera_front", state.items[1].id, "front"),
        ("camera_left", state.items[2].id, "left"),
    }


@pytest.mark.asyncio
async def test_invalid_pointcloud_proposals_are_rejected_before_preparation(
    db_session, super_admin
):
    actor, _ = super_admin
    state = await _seed_uncommitted(
        db_session, owner_id=actor.id, item_count=1, mode="pointcloud"
    )
    operation_id = uuid.uuid4()
    await _accept(
        db_session,
        operation_id=operation_id,
        actor_id=state.actor_id,
        dataset_id=state.dataset_id,
        project_id=state.project_id,
        association_id=state.association_id,
        mode="pointcloud",
    )
    invalid = [
        {
            "primary_item_id": str(state.items[0].id),
            "cameras": [
                {"item_id": str(state.items[0].id), "sensor_name": "front"},
                {"item_id": str(state.items[0].id), "sensor_name": "front"},
            ],
        }
    ]
    response = await _prepare(db_session, operation_id, invalid)
    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"
    operation = await db_session.get(TaskCreationOperation, operation_id)
    assert operation is not None
    assert operation.manifest is None
    assert operation.total is None


@pytest.mark.asyncio
async def test_accepted_operation_survives_actor_deactivation_but_not_relink(
    db_session, super_admin
):
    actor, _ = super_admin
    state = await _seed_uncommitted(db_session, owner_id=actor.id, item_count=1)
    operation_id = uuid.uuid4()
    await _accept(
        db_session,
        operation_id=operation_id,
        actor_id=state.actor_id,
        dataset_id=state.dataset_id,
        project_id=state.project_id,
        association_id=state.association_id,
    )
    assert (await _prepare(db_session, operation_id, None))["ok"] is True

    actor.is_active = False
    await db_session.flush()
    summary = await _read(db_session, operation_id)
    assert summary["ok"] is True

    await db_session.execute(
        delete(ProjectDataset).where(ProjectDataset.id == state.association_id)
    )
    new_association_id = uuid.uuid4()
    db_session.add(
        ProjectDataset(
            id=new_association_id,
            dataset_id=state.dataset_id,
            project_id=state.project_id,
        )
    )
    await db_session.flush()

    stale = await _advance(db_session, operation_id, 0)
    assert stale["ok"] is False
    assert stale["error"]["code"] == "association_stale"
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Task)
            .where(Task.project_id == state.project_id)
        )
        == 0
    )


@pytest.mark.asyncio
async def test_cancelled_or_terminal_jobs_cannot_start_new_chunks(
    db_session, super_admin
):
    actor, _ = super_admin
    state = await _seed_uncommitted(db_session, owner_id=actor.id, item_count=1)
    cancelled_operation_id = uuid.uuid4()
    cancelled_job_id = await _add_job(db_session, state, cancelled_operation_id)
    await _accept(
        db_session,
        operation_id=cancelled_operation_id,
        actor_id=state.actor_id,
        dataset_id=state.dataset_id,
        project_id=state.project_id,
        association_id=state.association_id,
        job_id=cancelled_job_id,
    )
    assert (await _prepare(db_session, cancelled_operation_id, None))["ok"] is True
    cancelled_job = await db_session.get(AsyncJob, cancelled_job_id)
    cancelled_job.payload = {
        **cancelled_job.payload,
        "cancel_requested": True,
    }
    await db_session.flush()
    cancelled = await _advance(db_session, cancelled_operation_id, 0)
    assert cancelled["ok"] is False
    assert cancelled["error"]["code"] == "job_cancelled"

    terminal_operation_id = uuid.uuid4()
    terminal_job_id = await _add_job(db_session, state, terminal_operation_id)
    await _accept(
        db_session,
        operation_id=terminal_operation_id,
        actor_id=state.actor_id,
        dataset_id=state.dataset_id,
        project_id=state.project_id,
        association_id=state.association_id,
        job_id=terminal_job_id,
    )
    assert (await _prepare(db_session, terminal_operation_id, None))["ok"] is True
    terminal_job = await db_session.get(AsyncJob, terminal_job_id)
    terminal_job.status = "completed"
    await db_session.flush()
    rejected = await _advance(db_session, terminal_operation_id, 0)
    assert rejected["ok"] is False
    assert rejected["error"]["code"] == "job_not_active"
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Task)
            .where(Task.project_id == state.project_id)
        )
        == 0
    )


@pytest.mark.asyncio
async def test_deleted_source_after_prepare_returns_source_changed(
    db_session, super_admin
):
    actor, _ = super_admin
    state = await _seed_uncommitted(db_session, owner_id=actor.id, item_count=1)
    operation_id = uuid.uuid4()
    await _accept(
        db_session,
        operation_id=operation_id,
        actor_id=state.actor_id,
        dataset_id=state.dataset_id,
        project_id=state.project_id,
        association_id=state.association_id,
    )
    assert (await _prepare(db_session, operation_id, None))["ok"] is True
    await db_session.execute(
        delete(DatasetItem).where(DatasetItem.id == state.items[0].id)
    )
    source_changed = await _advance(db_session, operation_id, 0)
    assert source_changed["ok"] is False
    assert source_changed["error"]["code"] == "source_changed"
    operation = await db_session.get(TaskCreationOperation, operation_id)
    assert operation is not None
    assert operation.next_chunk == 0
    assert operation.processed == 0


@pytest.mark.asyncio
async def test_initial_chunk_serializes_with_append_without_duplicate_task(test_engine):
    fixture = await _seed_committed(test_engine, item_count=1)
    operation_id = uuid.uuid4()
    append_operation_id = uuid.uuid4()
    job_id = None
    maker = fixture["maker"]
    try:
        async with maker() as db:
            await _accept(
                db,
                operation_id=operation_id,
                actor_id=fixture["actor_id"],
                dataset_id=fixture["dataset_id"],
                project_id=fixture["project_id"],
                association_id=fixture["association_id"],
                chunk_size=1,
            )
            assert (await _prepare(db, operation_id, None))["ok"] is True
            await db.commit()

        async def run_initial() -> dict:
            async with maker() as db:
                async with db.begin():
                    return await _advance(db, operation_id, 0)

        async def run_append() -> dict:
            async with maker() as db:
                async with db.begin():
                    return (
                        await db.execute(
                            _APPEND,
                            {
                                "dataset_id": fixture["dataset_id"],
                                "item_ids": [fixture["item_ids"][0]],
                                "operation_id": append_operation_id,
                            },
                        )
                    ).scalar_one()

        initial_result, append_result = await asyncio.wait_for(
            asyncio.gather(run_initial(), run_append()), timeout=20
        )
        assert initial_result["ok"] is True
        assert append_result["schema"] == "aap-task-append/v1"
        assert initial_result["data"]["created"] + append_result["created"] == 1

        async with maker() as observer:
            assert (
                await observer.scalar(
                    select(func.count())
                    .select_from(Task)
                    .where(
                        Task.project_id == fixture["project_id"],
                        Task.dataset_item_id == fixture["item_ids"][0],
                    )
                )
                == 1
            )
            assert (
                await observer.scalar(
                    select(Project.total_tasks).where(
                        Project.id == fixture["project_id"]
                    )
                )
                == 1
            )
    finally:
        await _cleanup(
            test_engine,
            fixture,
            [operation_id, append_operation_id],
            [job_id] if job_id else [],
        )


@pytest.mark.asyncio
async def test_failed_commit_preserves_tasks_counter_cursor_and_receipt(test_engine):
    fixture = await _seed_committed(test_engine, item_count=1)
    operation_id = uuid.uuid4()
    suffix = uuid.uuid4().hex
    function_name = f"fail_task_creation_commit_{suffix}"
    trigger_name = f"fail_task_creation_commit_{suffix}"
    maker = fixture["maker"]
    async with maker() as db:
        await _accept(
            db,
            operation_id=operation_id,
            actor_id=fixture["actor_id"],
            dataset_id=fixture["dataset_id"],
            project_id=fixture["project_id"],
            association_id=fixture["association_id"],
        )
        assert (await _prepare(db, operation_id, None))["ok"] is True
        await db.commit()

    async with test_engine.begin() as connection:
        await connection.execute(
            text(
                f"""
                CREATE FUNCTION public.{function_name}() RETURNS trigger
                LANGUAGE plpgsql AS $test$
                BEGIN
                    IF current_setting('aap.test_task_creation_fail_commit', true) = 'on' THEN
                        RAISE EXCEPTION USING
                            ERRCODE = '23514',
                            MESSAGE = 'forced initial task creation commit failure';
                    END IF;
                    RETURN NULL;
                END;
                $test$
                """
            )
        )
        await connection.execute(
            text(
                f"""
                CREATE CONSTRAINT TRIGGER {trigger_name}
                AFTER INSERT ON public.task_creation_chunks
                DEFERRABLE INITIALLY DEFERRED
                FOR EACH ROW EXECUTE FUNCTION public.{function_name}()
                """
            )
        )

    try:
        async with maker() as db:
            await db.begin()
            await db.execute(
                text(
                    "SELECT pg_catalog.set_config("
                    "'aap.test_task_creation_fail_commit', 'on', true)"
                )
            )
            assert (await _advance(db, operation_id, 0))["data"]["created"] == 1
            with pytest.raises(
                DBAPIError, match="forced initial task creation commit failure"
            ):
                await db.commit()
            await db.rollback()

        async with maker() as observer:
            operation = await observer.get(TaskCreationOperation, operation_id)
            assert operation is not None
            assert operation.state == "running"
            assert operation.next_chunk == 0
            assert operation.processed == 0
            assert operation.created == 0
            assert await observer.get(TaskCreationChunk, (operation_id, 0)) is None
            assert (
                await observer.scalar(
                    select(func.count())
                    .select_from(Task)
                    .where(Task.project_id == fixture["project_id"])
                )
                == 0
            )
            assert (
                await observer.scalar(
                    select(Project.total_tasks).where(
                        Project.id == fixture["project_id"]
                    )
                )
                == 0
            )
    finally:
        async with test_engine.begin() as connection:
            await connection.execute(
                text(
                    f"DROP TRIGGER IF EXISTS {trigger_name} "
                    "ON public.task_creation_chunks"
                )
            )
            await connection.execute(
                text(f"DROP FUNCTION IF EXISTS public.{function_name}()")
            )
        await _cleanup(test_engine, fixture, [operation_id], [])


def test_task_creation_schema_and_frozen_sql_chunks():
    schema_path = (
        Path(__file__).parents[1] / "alembic" / "sql" / "0194_task_creation.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert schema["$defs"]["acceptRequest"]["additionalProperties"] is False
    assert set(schema["$defs"]["acceptRequest"]["required"]) == {
        "operation_id",
        "actor_id",
        "dataset_id",
        "project_id",
        "association_id",
        "job_id",
        "mode",
        "chunk_size",
    }
    chunk = schema["$defs"]["taskCreationChunkData"]
    assert chunk["additionalProperties"] is False
    assert "project_id" in chunk["required"]
    assert "project_id" in chunk["properties"]
    sql_path = schema_path.with_name("0194_initial_task_creation.sql")
    statements = [
        statement.strip()
        for statement in sql_path.read_text(encoding="utf-8").split(
            "-- aap-statement-break"
        )
        if statement.strip()
    ]
    functions = [
        statement for statement in statements if statement.startswith("CREATE FUNCTION")
    ]
    assert len(functions) == 5
    assert all(statement.endswith("$function$;") for statement in functions)
    assert all(
        statement.count(";") == 1
        for statement in statements
        if not statement.startswith("CREATE FUNCTION")
    )
    assert not TaskCreationOperation.__table__.foreign_keys
    assert len(TaskCreationChunk.__table__.foreign_keys) == 1
