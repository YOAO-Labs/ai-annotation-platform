from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import ARRAY, bindparam, delete, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.async_job import AsyncJob
from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset, Scene
from app.db.models.image_pyramid import ImagePyramidAsset, ImagePyramidGeneration
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.user import User


pytestmark = pytest.mark.asyncio

_DELETE_DATASET = text(
    "SELECT aap_assets_v1.delete_dataset("
    "CAST(:operation_id AS uuid), CAST(:actor_id AS uuid), "
    "CAST(:dataset_id AS uuid), :bucket, CAST(:audit_context AS jsonb))"
)
_DELETE_ITEM = text(
    "SELECT aap_assets_v1.delete_dataset_item("
    "CAST(:operation_id AS uuid), CAST(:actor_id AS uuid), "
    "CAST(:dataset_id AS uuid), CAST(:item_id AS uuid), "
    ":bucket, CAST(:audit_context AS jsonb))"
)
_LOCK_SCENE_SOURCES = text(
    "SELECT aap_assets_v1.lock_scene_source_references(:scene_ids)"
).bindparams(bindparam("scene_ids", type_=ARRAY(PGUUID(as_uuid=True))))
_SCENE_FOR_UPDATE = text(
    "SELECT id FROM public.scenes WHERE id=CAST(:scene_id AS uuid) FOR UPDATE NOWAIT"
)


def _context(fixture: dict[str, object], operation_id: uuid.UUID) -> str:
    return json.dumps(
        {
            "method": "DELETE",
            "path": f"/api/v1/datasets/{fixture['dataset_id']}",
            "ip": "127.0.0.1",
            "request_id": str(operation_id),
        }
    )


async def _seed(
    test_engine,
    *,
    item_count: int = 1,
    with_scene: bool = False,
    with_association: bool = False,
    with_batch: bool = False,
    with_pyramid: bool = False,
    lease_expires_at: datetime | None = None,
    task_reference: bool = False,
    actor_role: str = "super_admin",
) -> dict[str, object]:
    suffix = uuid.uuid4().hex[:10].upper()
    fixture: dict[str, object] = {
        "actor_id": uuid.uuid4(),
        "dataset_id": uuid.uuid4(),
        "project_id": uuid.uuid4(),
        "association_id": uuid.uuid4(),
        "batch_id": uuid.uuid4(),
        "scene_ids": [uuid.uuid4() for _ in range(2 if with_scene else 0)],
        "item_ids": [uuid.uuid4() for _ in range(item_count)],
        "file_paths": [
            f"{suffix}/original/image-{index}.png" for index in range(item_count)
        ],
        "task_id": uuid.uuid4(),
        "asset_id": uuid.uuid4(),
        "job_id": uuid.uuid4(),
        "operation_ids": [],
        "task_reference": task_reference,
        "with_association": with_association,
        "with_batch": with_batch,
        "with_pyramid": with_pyramid,
    }
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker.begin() as session:
        session.add(
            User(
                id=fixture["actor_id"],
                email=f"source-delete-{suffix}@example.test",
                name="Dataset source deletion fixture",
                password_hash="test-only",
                role=actor_role,
                is_active=True,
            )
        )
        await session.flush()
        session.add_all(
            [
                Dataset(
                    id=fixture["dataset_id"],
                    display_id=f"SD-{suffix}",
                    name=f"source-delete-{suffix}",
                    data_type="image",
                    created_by=fixture["actor_id"],
                    file_count=item_count,
                ),
                Project(
                    id=fixture["project_id"],
                    display_id=f"SP-{suffix}",
                    name=f"Source deletion project {suffix}",
                    type_label="Image",
                    type_key="image",
                    data_type="image",
                    owner_id=fixture["actor_id"],
                ),
            ]
        )
        await session.flush()

        scene_ids = fixture["scene_ids"]
        if scene_ids:
            session.add_all(
                Scene(
                    id=scene_id,
                    display_id=f"SS-{suffix}-{index}",
                    dataset_id=fixture["dataset_id"],
                    name=f"Scene {index}",
                    created_by=fixture["actor_id"],
                )
                for index, scene_id in enumerate(scene_ids)
            )
            await session.flush()

        item_ids = fixture["item_ids"]
        session.add_all(
            DatasetItem(
                id=item_id,
                dataset_id=fixture["dataset_id"],
                file_name=f"image-{index}.png",
                file_path=fixture["file_paths"][index],
                file_type="image",
                scene_id=scene_ids[index % len(scene_ids)] if scene_ids else None,
                frame_index=index if scene_ids else None,
            )
            for index, item_id in enumerate(item_ids)
        )
        await session.flush()

        if with_association:
            session.add(
                ProjectDataset(
                    id=fixture["association_id"],
                    project_id=fixture["project_id"],
                    dataset_id=fixture["dataset_id"],
                )
            )
        if with_batch:
            session.add(
                TaskBatch(
                    id=fixture["batch_id"],
                    project_id=fixture["project_id"],
                    dataset_id=fixture["dataset_id"],
                    display_id=f"SB-{suffix}",
                    name="Retained attribution batch",
                    status="completed",
                    assigned_user_ids=[],
                    total_tasks=7,
                    completed_tasks=4,
                    review_tasks=2,
                    approved_tasks=1,
                    rejected_tasks=1,
                    created_by=fixture["actor_id"],
                )
            )
        if task_reference:
            session.add(
                Task(
                    id=fixture["task_id"],
                    project_id=fixture["project_id"],
                    dataset_item_id=item_ids[0],
                    display_id=f"ST-{suffix}",
                    file_name="retained.png",
                    file_path=f"{suffix}/retained.png",
                    file_type="image",
                )
            )
        if with_pyramid:
            asset_id = fixture["asset_id"]
            session.add(
                ImagePyramidAsset(
                    id=asset_id,
                    dataset_item_id=item_ids[0],
                    profile_version="source-delete-test-v1",
                )
            )
            await session.flush()
            session.add(
                ImagePyramidGeneration(
                    asset_id=asset_id,
                    generation=1,
                    source_identity=f"fixture:{asset_id}",
                    status="building" if lease_expires_at else "ready",
                    lease_token=uuid.uuid4() if lease_expires_at else None,
                    lease_expires_at=lease_expires_at,
                    normalization_version="source-delete-test-v1",
                )
            )
        await session.flush()
    return fixture


async def _cleanup(test_engine, fixture: dict[str, object]) -> None:
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    operation_ids = list(fixture.get("operation_ids", []))
    async with maker.begin() as session:
        database = await session.scalar(text("SELECT current_database()"))
        if not database or not database.endswith("_test"):
            pytest.fail(
                "refusing source-deletion fixture cleanup outside a test database"
            )
        if operation_ids:
            params = {"ids": [str(value) for value in operation_ids]}
            await session.execute(
                text(
                    "DELETE FROM public.dataset_source_deletion_operations "
                    "WHERE operation_id = ANY(CAST(:ids AS uuid[]))"
                ),
                params,
            )
            await session.execute(
                text(
                    "DELETE FROM public.asset_storage_deletions "
                    "WHERE operation_id = ANY(CAST(:ids AS uuid[]))"
                ),
                params,
            )
            await session.execute(text("SET LOCAL \"app.allow_audit_update\" = 'true'"))
            await session.execute(
                text(
                    "DELETE FROM public.audit_logs "
                    "WHERE action IN ('dataset.delete', 'dataset.item_delete') "
                    "AND detail_json ->> 'operation_id' = ANY(CAST(:ids AS text[]))"
                ),
                params,
            )
        await session.execute(delete(AsyncJob).where(AsyncJob.id == fixture["job_id"]))
        await session.execute(delete(Task).where(Task.id == fixture["task_id"]))
        await session.execute(
            delete(TaskBatch).where(TaskBatch.id == fixture["batch_id"])
        )
        await session.execute(
            delete(ImagePyramidAsset).where(ImagePyramidAsset.id == fixture["asset_id"])
        )
        await session.execute(
            delete(ProjectDataset).where(ProjectDataset.id == fixture["association_id"])
        )
        await session.execute(
            delete(DatasetItem).where(DatasetItem.id.in_(fixture["item_ids"]))
        )
        if fixture["scene_ids"]:
            await session.execute(
                delete(Scene).where(Scene.id.in_(fixture["scene_ids"]))
            )
        await session.execute(
            delete(Dataset).where(Dataset.id == fixture["dataset_id"])
        )
        await session.execute(
            delete(Project).where(Project.id == fixture["project_id"])
        )
        await session.execute(delete(User).where(User.id == fixture["actor_id"]))


async def _delete(
    session: AsyncSession,
    fixture: dict[str, object],
    operation_id: uuid.UUID,
    *,
    item_id: uuid.UUID | None = None,
    bucket: str = "media-cache-test",
    actor_id: uuid.UUID | None = None,
    audit_context: str | None = None,
) -> dict:
    if operation_id not in fixture["operation_ids"]:
        fixture["operation_ids"].append(operation_id)
    parameters = {
        "operation_id": operation_id,
        "actor_id": actor_id or fixture["actor_id"],
        "dataset_id": fixture["dataset_id"],
        "bucket": bucket,
        "audit_context": audit_context
        if audit_context is not None
        else _context(fixture, operation_id),
    }
    if item_id is None:
        statement = _DELETE_DATASET
    else:
        statement = _DELETE_ITEM
        parameters["item_id"] = item_id
    return await session.scalar(statement, parameters)


async def _count(session: AsyncSession, statement: str, **parameters) -> int:
    return await session.scalar(text(statement), parameters)


def _sqlstate(error: DBAPIError) -> str | None:
    pending = [error.orig]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        state = getattr(current, "sqlstate", None) or getattr(current, "pgcode", None)
        if state:
            return state
        pending.extend(
            nested
            for nested in (
                getattr(current, "__cause__", None),
                getattr(current, "__context__", None),
            )
            if nested is not None
        )
    return None


async def _wait_for_lock_wait(test_engine, backend_pid: int) -> bool:
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        async with maker() as monitor:
            waiting = await monitor.scalar(
                text(
                    "SELECT wait_event_type = 'Lock' FROM pg_catalog.pg_stat_activity "
                    "WHERE pid = :pid"
                ),
                {"pid": backend_pid},
            )
        if waiting:
            return True
        await asyncio.sleep(0.02)
    return False


async def test_retained_task_reference_rejects_item_delete_without_partial_writes(
    test_engine,
):
    fixture = await _seed(test_engine, with_pyramid=True, task_reference=True)
    operation_id = uuid.uuid4()
    fixture["operation_ids"].append(operation_id)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker.begin() as session:
            reply = await _delete(
                session,
                fixture,
                operation_id,
                item_id=fixture["item_ids"][0],
            )
            assert reply == {
                "schema": "aap-dataset-source-delete/v1",
                "ok": False,
                "error": {"code": "source_in_use"},
            }
            assert (
                await _count(
                    session,
                    "SELECT count(*) FROM public.dataset_items WHERE id=:id",
                    id=fixture["item_ids"][0],
                )
                == 1
            )
            assert (
                await _count(
                    session,
                    "SELECT count(*) FROM public.image_pyramid_assets WHERE id=:id",
                    id=fixture["asset_id"],
                )
                == 1
            )
            assert (
                await _count(
                    session,
                    "SELECT count(*) FROM public.asset_storage_deletions "
                    "WHERE operation_id=:id",
                    id=operation_id,
                )
                == 0
            )
            assert (
                await _count(
                    session,
                    "SELECT count(*) FROM public.dataset_source_deletion_operations "
                    "WHERE operation_id=:id",
                    id=operation_id,
                )
                == 0
            )
            assert (
                await _count(
                    session,
                    "SELECT count(*) FROM public.audit_logs "
                    "WHERE detail_json ->> 'operation_id'=:id",
                    id=str(operation_id),
                )
                == 0
            )
    finally:
        await _cleanup(test_engine, fixture)


async def test_unproven_pyramid_writer_cannot_be_acknowledged_as_deleted(test_engine):
    fixture = await _seed(
        test_engine,
        with_pyramid=True,
        lease_expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    operation = uuid.uuid4()
    fixture["operation_ids"].append(operation)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker.begin() as db:
            await db.execute(
                text(
                    "UPDATE public.image_pyramid_generations SET lease_expires_at=NULL WHERE asset_id=:asset"
                ),
                {"asset": fixture["asset_id"]},
            )
        async with maker.begin() as db:
            reply = await _delete(
                db, fixture, operation, item_id=fixture["item_ids"][0]
            )
            assert reply["error"]["code"] == "source_busy"
        async with maker() as db:
            assert (
                await _count(
                    db,
                    "SELECT count(*) FROM public.image_pyramid_assets WHERE id=:id",
                    id=fixture["asset_id"],
                )
                == 1
            )
            assert (
                await _count(
                    db,
                    "SELECT count(*) FROM public.asset_storage_deletions WHERE operation_id=:id",
                    id=operation,
                )
                == 0
            )
    finally:
        await _cleanup(test_engine, fixture)


async def test_outer_rollback_restores_item_asset_audit_and_receipt(test_engine):
    fixture = await _seed(test_engine, with_pyramid=True)
    operation_id = uuid.uuid4()
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    caller = maker()
    try:
        await caller.begin()
        reply = await _delete(
            caller,
            fixture,
            operation_id,
            item_id=fixture["item_ids"][0],
        )
        assert reply["ok"] is True
        assert (
            await _count(
                caller,
                "SELECT count(*) FROM public.dataset_items WHERE id=:id",
                id=fixture["item_ids"][0],
            )
            == 0
        )
        assert (
            await _count(
                caller,
                "SELECT count(*) FROM public.asset_storage_deletions "
                "WHERE operation_id=:id",
                id=operation_id,
            )
            == 1
        )
        assert (
            await _count(
                caller,
                "SELECT count(*) FROM public.dataset_source_deletion_operations "
                "WHERE operation_id=:id",
                id=operation_id,
            )
            == 1
        )
        await caller.rollback()

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
                    "SELECT count(*) FROM public.asset_storage_deletions "
                    "WHERE operation_id=:id",
                    id=operation_id,
                )
                == 0
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
                    "SELECT count(*) FROM public.audit_logs "
                    "WHERE detail_json ->> 'operation_id'=:id",
                    id=str(operation_id),
                )
                == 0
            )
    finally:
        if caller.in_transaction():
            await caller.rollback()
        await caller.close()
        await _cleanup(test_engine, fixture)


async def test_item_delete_receipt_replays_conflicts_and_preserves_raw_source_key(
    test_engine,
):
    lease = datetime.now(UTC) + timedelta(hours=1)
    fixture = await _seed(
        test_engine,
        item_count=2,
        with_pyramid=True,
        lease_expires_at=lease,
    )
    operation_id = uuid.uuid4()
    fixture["operation_ids"].append(operation_id)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker.begin() as session:
            first = await _delete(
                session,
                fixture,
                operation_id,
                item_id=fixture["item_ids"][0],
            )
        assert first["schema"] == "aap-dataset-source-delete/v1"
        assert first["ok"] is True
        data = first["data"]
        assert data["deleted_datasets"] == 0
        assert data["deleted_items"] == 1
        assert data["deleted_scenes"] == 0
        assert data["deleted_associations"] == 0
        assert data["queued_storage_deletions"] == 1
        assert data["item_id"] == str(fixture["item_ids"][0])

        async with maker.begin() as session:
            replay = await _delete(
                session,
                fixture,
                operation_id,
                item_id=fixture["item_ids"][0],
                audit_context=_context(fixture, uuid.uuid4()),
            )
            assert replay == first
            conflict = await _delete(
                session,
                fixture,
                operation_id,
                item_id=fixture["item_ids"][0],
                bucket="another-media-cache",
            )
            assert conflict == {
                "schema": "aap-dataset-source-delete/v1",
                "ok": False,
                "error": {"code": "operation_conflict"},
            }
            assert (
                await _count(
                    session,
                    "SELECT count(*) FROM public.dataset_items WHERE id=:id",
                    id=fixture["item_ids"][1],
                )
                == 1
            )
            assert (
                await _count(
                    session,
                    "SELECT count(*) FROM public.audit_logs "
                    "WHERE detail_json ->> 'operation_id'=:id",
                    id=str(operation_id),
                )
                == 1
            )

        async with maker() as verify:
            row = (
                (
                    await verify.execute(
                        text(
                            "SELECT bucket, prefix, not_before FROM "
                            "public.asset_storage_deletions WHERE operation_id=:op "
                            "AND asset_id=:asset"
                        ),
                        {"op": operation_id, "asset": fixture["asset_id"]},
                    )
                )
                .mappings()
                .one()
            )
            assert row["bucket"] == "media-cache-test"
            assert row["prefix"] == f"image-pyramids/{fixture['asset_id']}/"
            assert row["not_before"] >= lease
            assert fixture["file_paths"][0] not in row["prefix"]
            assert str(fixture["item_ids"][0]) not in row["prefix"]
            assert (
                await _count(
                    verify,
                    "SELECT count(*) FROM public.dataset_source_deletion_operations "
                    "WHERE operation_id=:id",
                    id=operation_id,
                )
                == 1
            )
            assert (
                await _count(
                    verify,
                    "SELECT count(*) FROM public.audit_logs "
                    "WHERE action='dataset.item_delete' "
                    "AND target_id=:item_id AND detail_json ->> 'operation_id'=:op",
                    item_id=str(fixture["item_ids"][0]),
                    op=str(operation_id),
                )
                == 1
            )
            assert (
                await _count(
                    verify,
                    "SELECT count(*) FROM public.dataset_items WHERE dataset_id=:id",
                    id=fixture["dataset_id"],
                )
                == 1
            )
            assert (
                await verify.scalar(
                    text("SELECT file_count FROM public.datasets WHERE id=:id"),
                    {"id": fixture["dataset_id"]},
                )
                == 1
            )
    finally:
        await _cleanup(test_engine, fixture)


async def test_catalog_authority_is_fresh_for_new_deletions_and_inactive_replay(
    test_engine,
):
    fixture = await _seed(test_engine, actor_role="employee")
    operation_id = uuid.uuid4()
    other_operation_id = uuid.uuid4()
    fixture["operation_ids"].extend([operation_id, other_operation_id])
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker.begin() as session:
            denied = await _delete(
                session,
                fixture,
                operation_id,
                item_id=fixture["item_ids"][0],
            )
            assert denied["error"]["code"] == "catalog_manager_required"
            assert (
                await _count(
                    session,
                    "SELECT count(*) FROM public.dataset_items WHERE id=:id",
                    id=fixture["item_ids"][0],
                )
                == 1
            )
            assert (
                await _count(
                    session,
                    "SELECT count(*) FROM public.dataset_source_deletion_operations "
                    "WHERE operation_id=:id",
                    id=operation_id,
                )
                == 0
            )

        async with maker.begin() as session:
            await session.execute(
                text("UPDATE public.users SET role='super_admin' WHERE id=:id"),
                {"id": fixture["actor_id"]},
            )
            created = await _delete(
                session,
                fixture,
                operation_id,
                item_id=fixture["item_ids"][0],
            )
            assert created["ok"] is True

        async with maker.begin() as session:
            await session.execute(
                text("UPDATE public.users SET role='employee' WHERE id=:id"),
                {"id": fixture["actor_id"]},
            )
            active_demoted_replay = await _delete(
                session,
                fixture,
                operation_id,
                item_id=fixture["item_ids"][0],
            )
            assert active_demoted_replay == created
            await session.execute(
                text("UPDATE public.users SET is_active=false WHERE id=:id"),
                {"id": fixture["actor_id"]},
            )
            inactive = await _delete(
                session,
                fixture,
                operation_id,
                item_id=fixture["item_ids"][0],
            )
            assert inactive["error"]["code"] == "account_not_active"
            assert (
                await _count(
                    session,
                    "SELECT count(*) FROM public.dataset_source_deletion_operations "
                    "WHERE operation_id=:id",
                    id=operation_id,
                )
                == 1
            )
            assert (
                await _count(
                    session,
                    "SELECT count(*) FROM public.audit_logs "
                    "WHERE detail_json ->> 'operation_id'=:id",
                    id=str(operation_id),
                )
                == 1
            )
    finally:
        await _cleanup(test_engine, fixture)


async def test_whole_dataset_delete_commits_owner_receipt_and_batch_attribution(
    test_engine,
):
    fixture = await _seed(
        test_engine,
        with_scene=True,
        with_association=True,
        with_batch=True,
        with_pyramid=True,
    )
    operation_id = uuid.uuid4()
    fixture["operation_ids"].append(operation_id)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker.begin() as session:
            reply = await _delete(session, fixture, operation_id)
        assert reply["ok"] is True
        data = reply["data"]
        assert data["deleted_datasets"] == 1
        assert data["deleted_items"] == 1
        assert data["deleted_scenes"] == 2
        assert data["deleted_associations"] == 1
        assert data["queued_storage_deletions"] == 1
        async with maker() as verify:
            assert (
                await _count(
                    verify,
                    "SELECT count(*) FROM public.datasets WHERE id=:id",
                    id=fixture["dataset_id"],
                )
                == 0
            )
            assert (
                await _count(
                    verify,
                    "SELECT count(*) FROM public.scenes "
                    "WHERE id=ANY(CAST(:ids AS uuid[]))",
                    ids=fixture["scene_ids"],
                )
                == 0
            )
            batch = (
                (
                    await verify.execute(
                        text(
                            "SELECT dataset_id, total_tasks, completed_tasks, "
                            "review_tasks, approved_tasks, rejected_tasks "
                            "FROM public.task_batches WHERE id=:id"
                        ),
                        {"id": fixture["batch_id"]},
                    )
                )
                .mappings()
                .one()
            )
            assert batch["dataset_id"] is None
            assert (
                batch["total_tasks"],
                batch["completed_tasks"],
                batch["review_tasks"],
                batch["approved_tasks"],
                batch["rejected_tasks"],
            ) == (7, 4, 2, 1, 1)
            source_item_id = fixture["item_ids"][0]
            outbox = (
                (
                    await verify.execute(
                        text(
                            "SELECT prefix FROM public.asset_storage_deletions "
                            "WHERE operation_id=:id"
                        ),
                        {"id": operation_id},
                    )
                )
                .scalars()
                .all()
            )
            assert outbox == [f"image-pyramids/{fixture['asset_id']}/"]
            assert str(source_item_id) not in outbox[0]
            assert (
                await _count(
                    verify,
                    "SELECT count(*) FROM public.dataset_source_deletion_operations "
                    "WHERE operation_id=:id",
                    id=operation_id,
                )
                == 1
            )
            assert (
                await _count(
                    verify,
                    "SELECT count(*) FROM public.audit_logs "
                    "WHERE action='dataset.delete' "
                    "AND detail_json ->> 'operation_id'=:id",
                    id=str(operation_id),
                )
                == 1
            )
    finally:
        await _cleanup(test_engine, fixture)


@pytest.mark.parametrize("active_import", [False, True])
async def test_pending_upload_deletion_discards_only_when_no_import_is_active(
    test_engine, active_import: bool
):
    fixture = await _seed(test_engine)
    operation_id = uuid.uuid4()
    upload_id = uuid.uuid4()
    fixture["operation_ids"].append(operation_id)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker.begin() as session:
            if active_import:
                session.add(
                    AsyncJob(
                        id=fixture["job_id"],
                        kind="dataset_import",
                        user_id=fixture["actor_id"],
                        status="pending",
                        payload={"dataset_id": str(fixture["dataset_id"])},
                    )
                )
                await session.flush()
            await session.execute(
                text(
                    "INSERT INTO public.dataset_upload_intents "
                    "(item_id, dataset_id, actor_id, file_name, file_type, "
                    "content_type, staging_key) VALUES "
                    "(:item, :dataset, :actor, 'pending.png', 'image', "
                    "'image/png', :staging)"
                ),
                {
                    "item": upload_id,
                    "dataset": fixture["dataset_id"],
                    "actor": fixture["actor_id"],
                    "staging": f"dataset-uploads/{upload_id}/source",
                },
            )
            reply = await _delete(
                session,
                fixture,
                operation_id,
                item_id=upload_id,
            )

        async with maker() as verify:
            intent = (
                (
                    await verify.execute(
                        text(
                            "SELECT state, result FROM public.dataset_upload_intents "
                            "WHERE item_id=:id"
                        ),
                        {"id": upload_id},
                    )
                )
                .mappings()
                .one()
            )
            if active_import:
                assert reply["error"]["code"] == "source_busy"
                assert intent["state"] == "pending"
                assert intent["result"] is None
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
                        "SELECT count(*) FROM public.audit_logs "
                        "WHERE detail_json ->> 'operation_id'=:id",
                        id=str(operation_id),
                    )
                    == 0
                )
            else:
                assert reply["ok"] is True
                assert reply["data"]["deleted_items"] == 0
                assert reply["data"]["discarded_uploads"] == 1
                assert intent["state"] == "discarded"
                assert intent["result"] == {"reason": "source_deleted"}
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
        await _cleanup(test_engine, fixture)


async def test_item_delete_fk_fence_rejects_writer_that_started_behind_delete(
    test_engine,
):
    fixture = await _seed(test_engine)
    operation_id = uuid.uuid4()
    fixture["operation_ids"].append(operation_id)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    deleter = maker()
    writer = maker()
    writer_flush: asyncio.Task | None = None
    try:
        await deleter.begin()
        reply = await _delete(
            deleter,
            fixture,
            operation_id,
            item_id=fixture["item_ids"][0],
        )
        assert reply["ok"] is True

        await writer.begin()
        backend_pid = await writer.scalar(text("SELECT pg_backend_pid()"))
        writer.add(
            Task(
                id=uuid.uuid4(),
                project_id=fixture["project_id"],
                dataset_item_id=fixture["item_ids"][0],
                display_id=f"Fence-{uuid.uuid4().hex[:12]}",
                file_name="late-reference.png",
                file_path="late-reference.png",
                file_type="image",
            )
        )
        writer_flush = asyncio.create_task(writer.flush())
        assert await _wait_for_lock_wait(test_engine, backend_pid)
        await deleter.commit()
        with pytest.raises(DBAPIError) as failed_writer:
            await asyncio.wait_for(writer_flush, timeout=5)
        assert _sqlstate(failed_writer.value) == "23503"
        await writer.rollback()
    finally:
        if writer_flush is not None and not writer_flush.done():
            writer_flush.cancel()
            try:
                await writer_flush
            except asyncio.CancelledError:
                pass
        if deleter.in_transaction():
            await deleter.rollback()
        if writer.in_transaction():
            await writer.rollback()
        await deleter.close()
        await writer.close()
        await _cleanup(test_engine, fixture)


async def test_scene_reference_fence_is_ordered_and_nonblocking(test_engine):
    fixture = await _seed(test_engine, with_scene=True)
    scene_ids = fixture["scene_ids"]
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    holder = maker()
    try:
        # Simulate the source deleter holding its exclusive scene fence.
        await holder.begin()
        await holder.execute(_SCENE_FOR_UPDATE, {"scene_id": scene_ids[0]})
        async with maker.begin() as contender:
            result = await contender.scalar(
                _LOCK_SCENE_SOURCES, {"scene_ids": list(reversed(scene_ids))}
            )
            assert result is False
        await holder.commit()

        async with maker.begin() as fence:
            assert (
                await fence.scalar(
                    _LOCK_SCENE_SOURCES,
                    {"scene_ids": list(reversed(scene_ids))},
                )
                is True
            )
            async with maker() as writer:
                await writer.begin()
                with pytest.raises(DBAPIError) as blocked_delete:
                    await writer.execute(_SCENE_FOR_UPDATE, {"scene_id": scene_ids[1]})
                assert _sqlstate(blocked_delete.value) == "55P03"
                await writer.rollback()
        async with maker() as missing:
            await missing.begin()
            assert (
                await missing.scalar(
                    _LOCK_SCENE_SOURCES,
                    {"scene_ids": [scene_ids[0], uuid.uuid4()]},
                )
                is False
            )
            # A failed multi-scene fence must release every partial KEY SHARE
            # lock before its caller decides whether to commit or roll back.
            async with maker() as writer:
                await writer.begin()
                await writer.execute(_SCENE_FOR_UPDATE, {"scene_id": scene_ids[0]})
                await writer.rollback()
            await missing.commit()
    finally:
        if holder.in_transaction():
            await holder.rollback()
        await holder.close()
        await _cleanup(test_engine, fixture)
