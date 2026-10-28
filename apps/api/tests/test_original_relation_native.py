"""Real ordinary-role original relation reads and locks on an owned test DB."""

import os
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.managed_original_creation import ManagedOriginalCreation
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.db.models.task_original import TaskOriginal
from app.db.models.user import User
from app.services import original_sources as sources
from app.services.content_snapshot import ContentError
from app.services.dataset_assets import LEGACY_LOCATION_ID
from tests.factory import create_project, create_task, create_user
from tests.test_labeling_class_usage_reads_legacy import _legacy_runtime_url


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
    reason="requires owned Native role database",
)
async def test_original_relation_native(test_engine):
    await exercise_original_relation_native(test_engine)


async def exercise_original_relation_native(test_engine):
    runtime = create_async_engine(
        _legacy_runtime_url(test_engine), hide_parameters=True
    )
    owner = async_sessionmaker(test_engine, expire_on_commit=False)
    caller = async_sessionmaker(runtime, expire_on_commit=False)
    actor_id = project_id = dataset_id = item_id = task_id = original_id = None
    try:
        async with owner.begin() as db:
            actor = await create_user(
                db,
                "project_admin",
                f"relation-{uuid.uuid4().hex}@example.test",
                "Relation owner",
            )
            actor_id = actor.id
            project = await create_project(db, owner_id=actor_id)
            project_id = project.id
            task = await create_task(db, project_id=project_id, status="pending")
            task_id = task.id
            dataset = Dataset(
                display_id=f"DS-{uuid.uuid4().hex[:10]}",
                name="Relation",
                data_type="pointcloud",
                created_by=actor_id,
            )
            db.add(dataset)
            await db.flush()
            dataset_id = dataset.id
            item = DatasetItem(
                dataset_id=dataset_id,
                file_name="relation.pcd",
                file_path="relation/frame.pcd",
                file_type="pointcloud",
            )
            db.add(item)
            await db.flush()
            item_id = item.id
            task.dataset_item_id = item_id
            primary = TaskDatasetItemLink(
                id=uuid.uuid4(),
                task_id=task_id,
                dataset_item_id=item_id,
                role="primary_lidar",
                sensor_name="lidar",
            )
            camera = TaskDatasetItemLink(
                id=uuid.uuid4(),
                task_id=task_id,
                dataset_item_id=item_id,
                role="camera_front",
                sensor_name="camera",
            )
            db.add_all([primary, camera])
            primary_id, camera_id = primary.id, camera.id
        async with caller() as db:
            assert await db.scalar(text("SELECT current_user")) == "legacy_runtime"
            assert not await db.scalar(
                text(
                    "SELECT has_table_privilege(current_user, 'public.task_dataset_item_links', 'SELECT')"
                )
            )
            for lock in (False, True):
                relation = await sources.resolve_task_relation(db, task_id, lock=lock)
                assert [link.id for link in relation.links] == sorted(
                    [primary_id, camera_id]
                )
                assert relation.item_id == item_id
                async with sources._reads(db, lock):
                    compact = await sources._relation(
                        db,
                        task_id,
                        lock,
                        compact_relation=True,
                        link_id=camera_id,
                        max_links=2,
                    )
                assert [link.id for link in compact.links] == sorted(
                    [primary_id, camera_id]
                )
                async with sources._reads(db, lock):
                    compact = await sources._relation(
                        db, task_id, lock, compact_relation=True, max_links=1
                    )
                assert [link.id for link in compact.links] == [primary_id]
                with pytest.raises(ContentError) as error:
                    await sources.resolve_task_relation(
                        db, task_id, lock=lock, max_links=1
                    )
                assert error.value.code == "original_relation_budget_exceeded"
                await db.rollback()
            # A successful share lock lives until the caller transaction ends.
            await sources.resolve_task_relation(db, task_id, lock=True)
            async with owner() as contender:
                with pytest.raises(DBAPIError) as error:
                    await contender.scalar(
                        select(TaskDatasetItemLink)
                        .where(TaskDatasetItemLink.id == primary_id)
                        .with_for_update(nowait=True)
                    )
                assert getattr(error.value.orig, "sqlstate", None) == "55P03"
                await contender.rollback()
            await db.rollback()
            # Compact locking ignores unselected cameras unless requested.
            async with owner() as blocker:
                await blocker.scalar(
                    select(TaskDatasetItemLink)
                    .where(TaskDatasetItemLink.id == camera_id)
                    .with_for_update()
                )
                async with sources._reads(db, True):
                    relation = await sources._relation(
                        db, task_id, True, compact_relation=True
                    )
                assert [link.id for link in relation.links] == [primary_id]
                with pytest.raises(ContentError) as error:
                    async with sources._reads(db, True):
                        await sources._relation(
                            db, task_id, True, compact_relation=True, link_id=camera_id
                        )
                assert error.value.code == "storage_original_busy"
                await blocker.rollback()
            await db.rollback()
            # Holding the link, not the task, proves the narrow lock is real.
            async with owner() as blocker:
                await db.execute(
                    text("SELECT set_config('aap.relation_probe', 'caller-work', true)")
                )
                await blocker.scalar(
                    select(TaskDatasetItemLink)
                    .where(TaskDatasetItemLink.id == primary_id)
                    .with_for_update()
                )
                pending = DatasetItem(
                    id=uuid.uuid4(), dataset_id=dataset_id, file_name="pending"
                )
                db.add(pending)
                with pytest.raises(ContentError) as error:
                    await sources.resolve_task_relation(db, task_id, lock=True)
                assert error.value.code == "storage_original_busy"
                assert pending in db.new
                with db.no_autoflush:
                    assert await db.scalar(text("SELECT 1")) == 1
                    assert (
                        await db.scalar(
                            text("SELECT current_setting('aap.relation_probe')")
                        )
                        == "caller-work"
                    )
                db.expunge(pending)
                await blocker.rollback()
                # The original caller transaction remains usable after savepoint rollback.
                assert (
                    await sources.resolve_task_relation(db, task_id, lock=True)
                ).item_id == item_id
                await db.rollback()
        # Camera-only standalone original exercises the compact EXISTS conflict.
        original_id = uuid.uuid4()
        key = f"__aap_task_originals__/{task_id}/{original_id}/payload"
        async with owner.begin() as db:
            await db.execute(
                delete(TaskDatasetItemLink).where(TaskDatasetItemLink.id == primary_id)
            )
            await db.execute(
                update(Task).where(Task.id == task_id).values(dataset_item_id=None)
            )
            db.add(
                ManagedOriginalCreation(
                    id=original_id,
                    owner_kind="task",
                    project_id=project_id,
                    task_id=task_id,
                    location_id=LEGACY_LOCATION_ID,
                    storage_identity="a" * 64,
                    bucket="relation-test",
                    object_key=key,
                    logical_path="relation",
                    origin="direct",
                    max_bytes=1,
                    timeout_seconds=60,
                    content_type="application/octet-stream",
                    authority={},
                    configuration={},
                    input_fingerprint="b" * 64,
                    maintenance_revision=1,
                    staging_bucket="relation-test",
                    staging_key=f"staging/{original_id}",
                )
            )
            await db.flush()
            db.add(
                TaskOriginal(
                    id=original_id,
                    creation_id=original_id,
                    task_id=task_id,
                    project_id=project_id,
                    location_id=LEGACY_LOCATION_ID,
                    storage_identity="a" * 64,
                    bucket="relation-test",
                    object_key=key,
                    sha256="c" * 64,
                    size_bytes=1,
                    etag="etag",
                    created_at=datetime.now(timezone.utc),
                )
            )
            await db.flush()
            await db.execute(
                update(Task).where(Task.id == task_id).values(original_id=original_id)
            )
        async with caller() as db:
            for lock in (False, True):
                with pytest.raises(ContentError) as error:
                    async with sources._reads(db, lock):
                        await sources._relation(
                            db, task_id, lock, compact_relation=True
                        )
                assert error.value.code == "original_relation_conflict"
                assert await db.scalar(text("SELECT 1")) == 1
                await db.rollback()
    finally:
        await runtime.dispose()
        async with owner.begin() as db:
            if task_id:
                await db.execute(
                    update(Task).where(Task.id == task_id).values(original_id=None)
                )
                await db.execute(
                    delete(TaskDatasetItemLink).where(
                        TaskDatasetItemLink.task_id == task_id
                    )
                )
                await db.execute(delete(Task).where(Task.id == task_id))
            if original_id:
                await db.execute(
                    delete(TaskOriginal).where(TaskOriginal.id == original_id)
                )
                await db.execute(
                    delete(ManagedOriginalCreation).where(
                        ManagedOriginalCreation.id == original_id
                    )
                )
            if item_id:
                await db.execute(delete(DatasetItem).where(DatasetItem.id == item_id))
            if dataset_id:
                await db.execute(delete(Dataset).where(Dataset.id == dataset_id))
            if project_id:
                await db.execute(delete(Project).where(Project.id == project_id))
            if actor_id:
                await db.execute(delete(User).where(User.id == actor_id))
