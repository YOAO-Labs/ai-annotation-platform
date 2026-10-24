"""Standalone-original database invariants in an explicitly owned namespace."""

from datetime import timedelta
import uuid

import pytest
from sqlalchemy import delete, inspect, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.managed_original_creation import ManagedOriginalCreation as Creation
from app.db.models.task import Task
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.db.models.task_original import TaskOriginal
from app.services import managed_original_creation as creation
from app.services import task_originals as service
from app.services.managed_original_retirement import managed_storage_identity
from tests.test_task_originals import (
    allocated,
    migration,
    original_client as original_client,
    original_db as original_db,
    owners,
    verified,
)


async def values_for(db, created):
    source = await creation._read(db, created.creation_id)
    values = {
        field: source[field]
        for field in service.FACTS
        if field not in {"creation_id", "created_at"}
    }
    return values | {
        "creation_id": created.creation_id,
        "created_at": source["completed_at"],
    }


async def test_empty_roundtrip_preserves_creation_and_legacy_tasks(original_db):
    db = original_db
    legacy = Task(
        project_id=uuid.uuid4(),
        display_id="legacy-" + uuid.uuid4().hex[:16],
        file_name="old",
        file_path="old",
        dataset_item_id=None,
    )
    db.add(legacy)
    await db.flush()
    identifier = legacy.id
    connection = await db.connection()

    def roundtrip(sync):
        module = migration(sync, "0231")
        assert module.down_revision == "0230"
        schema = sync.scalar(text("SELECT current_schema()"))
        foreign = inspect(sync).get_foreign_keys("task_originals", schema=schema)
        assert len(foreign) == 1
        assert foreign[0]["referred_table"] == "managed_original_creations"
        assert foreign[0]["options"]["ondelete"] == "RESTRICT"
        module.downgrade()
        assert not inspect(sync).has_table("task_originals", schema=schema)
        assert inspect(sync).has_table("managed_original_creations", schema=schema)
        module.upgrade()

    await connection.run_sync(roundtrip)
    await db.refresh(legacy)
    assert (
        legacy.id == identifier
        and legacy.original_id is None
        and legacy.file_path == "old"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"id": uuid.uuid4()},
        {"creation_id": uuid.uuid4()},
        {"task_id": uuid.uuid4()},
        {"project_id": uuid.uuid4()},
        {"location_id": uuid.uuid4()},
        {"storage_identity": "a" * 64},
        {"bucket": "other-bucket"},
        {"object_key": "old/path"},
        {"sha256": "a" * 64},
        {"size_bytes": 0},
        {"etag": '"other"'},
        {"version_id": "not-the-source-version"},
    ],
)
async def test_first_insert_requires_exact_verified_creation(
    original_db, original_client, changes
):
    _, _, created = await verified(original_db, original_client)
    values = await values_for(original_db, created)
    with pytest.raises(IntegrityError, match="matching verified creation"):
        async with original_db.begin_nested():
            await original_db.execute(
                TaskOriginal.__table__.insert().values(**(values | changes))
            )
    assert not await owners(original_db)


async def test_pending_creation_cannot_forge_owner_even_with_matching_candidate_facts(
    original_db,
):
    db = original_db
    task, allocation = await allocated(db)
    values = {
        "id": allocation["id"],
        "creation_id": allocation["id"],
        "task_id": task.id,
        "project_id": task.project_id,
        "location_id": allocation["location_id"],
        "storage_identity": allocation["storage_identity"],
        "bucket": allocation["bucket"],
        "object_key": allocation["object_key"],
        "sha256": "a" * 64,
        "size_bytes": 0,
        "etag": '"forged"',
        "version_id": None,
        "created_at": allocation["allocated_at"],
    }
    with pytest.raises(IntegrityError, match="matching verified creation"):
        async with db.begin_nested():
            await db.execute(TaskOriginal.__table__.insert().values(**values))
    assert not await owners(db)


@pytest.mark.parametrize("field", service.FACTS)
async def test_owner_facts_and_location_are_immutable(
    original_db, original_client, field
):
    db = original_db
    task, allocation, created = await verified(db, original_client)
    owner = await service.publish(db, task, allocation, created)
    old = getattr(owner, field)
    if isinstance(old, uuid.UUID):
        value = uuid.uuid4()
    elif field == "created_at":
        value = old + timedelta(seconds=1)
    elif field == "size_bytes":
        value = old + 1
    elif field in {"sha256", "storage_identity"}:
        value = "f" * 64
    else:
        value = "changed"
    with pytest.raises(IntegrityError, match="immutable"):
        async with db.begin_nested():
            await db.execute(
                update(TaskOriginal)
                .where(TaskOriginal.id == owner.id)
                .values(**{field: value})
            )


async def test_owner_and_creation_cannot_be_deleted_after_task_is_gone(
    original_db, original_client
):
    db = original_db
    task, allocation, created = await verified(db, original_client)
    owner = await service.publish(db, task, allocation, created)
    await db.delete(task)
    await db.commit()
    for model in (TaskOriginal, Creation):
        with pytest.raises(IntegrityError, match="immutable|cannot be deleted"):
            async with db.begin_nested():
                await db.execute(delete(model).where(model.id == owner.id))
    assert (await owners(db))[0]["id"] == owner.id


async def test_restore_changes_only_current_bucket_and_identity(
    original_db, original_client
):
    db = original_db
    task, allocation, created = await verified(db, original_client)
    owner = await service.publish(db, task, allocation, created)
    before = service.snapshot(owner)
    source = await creation._read(db, owner.id)
    endpoint = service.resolve(owner).endpoint_url
    mapped = {
        "bucket": "restored-annotations",
        "storage_identity": managed_storage_identity(endpoint, "restored-annotations"),
    }
    await db.execute(text("SET LOCAL aap.restore_training_storage='on'"))
    await db.execute(
        update(TaskOriginal).where(TaskOriginal.id == owner.id).values(**mapped)
    )
    current = await db.get(TaskOriginal, owner.id, populate_existing=True)
    for field in service.FACTS:
        assert getattr(current, field) == mapped.get(field, getattr(before, field))
    assert service.resolve(current).bucket == mapped["bucket"]
    assert await creation._read(db, owner.id) == source
    for changes in (
        {"bucket": "bucket-only"},
        {"storage_identity": "e" * 64},
        mapped | {"sha256": "f" * 64},
        mapped | {"object_key": "forged"},
    ):
        with pytest.raises(IntegrityError, match="immutable"):
            async with db.begin_nested():
                await db.execute(
                    update(TaskOriginal)
                    .where(TaskOriginal.id == owner.id)
                    .values(**changes)
                )


@pytest.mark.parametrize(
    "changes",
    [
        {"original_id": None},
        {"original_id": uuid.uuid4()},
        {"project_id": uuid.uuid4()},
        {"file_path": "other/key"},
        {"dataset_item_id": uuid.uuid4()},
        {"id": uuid.uuid4()},
    ],
)
async def test_attached_task_cannot_rebind_or_change_original_scope(
    original_db, original_client, changes
):
    db = original_db
    task, allocation, created = await verified(db, original_client)
    await service.publish(db, task, allocation, created)
    with pytest.raises(IntegrityError, match="original"):
        async with db.begin_nested():
            await db.execute(update(Task).where(Task.id == task.id).values(**changes))


async def test_owner_cannot_be_attached_to_another_task(original_db, original_client):
    db = original_db
    first, allocation, created = await verified(db, original_client)
    owner = await service.publish(db, first, allocation, created)
    second = Task(
        project_id=first.project_id,
        display_id="other-" + uuid.uuid4().hex[:18],
        file_name="other",
        file_path=first.file_path,
    )
    db.add(second)
    await db.flush()
    with pytest.raises(IntegrityError, match="attachment does not match"):
        async with db.begin_nested():
            await db.execute(
                update(Task).where(Task.id == second.id).values(original_id=owner.id)
            )


async def test_link_then_attach_and_attach_then_link_are_both_rejected(
    original_db, original_client
):
    db = original_db
    task, allocation, created = await verified(db, original_client)
    values = await values_for(db, created)
    await service._durable_owner(db, values)
    link_id = uuid.uuid4()
    db.add(
        TaskDatasetItemLink(
            id=link_id,
            task_id=task.id,
            dataset_item_id=uuid.uuid4(),
            role="primary_lidar",
        )
    )
    await db.flush()
    with pytest.raises(IntegrityError, match="attachment does not match"):
        async with db.begin_nested():
            await db.execute(
                update(Task)
                .where(Task.id == task.id)
                .values(original_id=created.creation_id)
            )
    await db.execute(
        delete(TaskDatasetItemLink).where(TaskDatasetItemLink.id == link_id)
    )
    await service.publish(db, task, allocation, created)
    with pytest.raises(IntegrityError, match="cannot acquire dataset links"):
        async with db.begin_nested():
            await db.execute(
                TaskDatasetItemLink.__table__.insert().values(
                    id=uuid.uuid4(),
                    task_id=task.id,
                    dataset_item_id=uuid.uuid4(),
                    role="camera_front",
                )
            )


async def test_concurrent_link_insert_cannot_pass_inflight_attachment(
    original_db, original_client
):
    db = original_db
    task, allocation, created = await verified(db, original_client)
    await service.publish(db, task, allocation, created)
    async with async_sessionmaker(
        service.storage_access.engine(db), expire_on_commit=False
    )() as other:
        with pytest.raises(DBAPIError) as error:
            await other.execute(
                TaskDatasetItemLink.__table__.insert().values(
                    id=uuid.uuid4(),
                    task_id=task.id,
                    dataset_item_id=uuid.uuid4(),
                    role="primary_lidar",
                )
            )
        assert getattr(error.value.orig, "sqlstate", None) == "55P03"
        await other.rollback()
    await db.commit()


async def test_any_owner_prevents_destructive_downgrade(original_db, original_client):
    db = original_db
    task, allocation, created = await verified(db, original_client)
    identifier = task.id
    await service.publish(db, task, allocation, created)
    await db.rollback()
    assert not await db.scalar(select(Task.original_id).where(Task.id == identifier))
    connection = await db.connection()
    with pytest.raises(RuntimeError, match="prevents destructive downgrade"):
        await connection.run_sync(lambda sync: migration(sync, "0231").downgrade())
