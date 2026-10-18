from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models.audit_log import AuditLog
from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_append_operation import TaskAppendOperation
from app.db.models.user import User
from app.services.dataset_upload_contract import (
    DatasetUploadCompletion,
    DatasetUploadReserve,
)
from app.services.dataset_upload_data import DatasetUploadData

pytestmark = pytest.mark.asyncio

_ASSOCIATE = text(
    """
    SELECT aap_assets_v1.associate_project(
        CAST(:actor AS uuid), CAST(:dataset AS uuid), CAST(:project AS uuid)
    )
    """
)
_RESERVE = text(
    """
    SELECT aap_assets_v1.reserve_dataset_upload(
        CAST(:item AS uuid), CAST(:actor AS uuid), CAST(:dataset AS uuid),
        CAST(:file_name AS text), CAST(:content_type AS text)
    )
    """
)
_LOCK = text(
    """
    SELECT aap_assets_v1.lock_dataset_upload(
        CAST(:actor AS uuid), CAST(:dataset AS uuid), CAST(:item AS uuid)
    )
    """
)
_FIND_DUPLICATE = text(
    """
    SELECT aap_assets_v1.find_dataset_upload_duplicate(
        CAST(:actor AS uuid), CAST(:dataset AS uuid), CAST(:item AS uuid),
        CAST(:content_hash AS text)
    )
    """
)
_COMPLETE = text(
    """
    SELECT aap_assets_v1.complete_dataset_upload(
        CAST(:actor AS uuid), CAST(:dataset AS uuid), CAST(:item AS uuid),
        CAST(:publication AS uuid), CAST(:file_size AS bigint),
        CAST(:content_hash AS text), CAST(:width AS integer), CAST(:height AS integer)
    )
    """
)
_DISCARD = text(
    """
    SELECT aap_assets_v1.discard_dataset_upload(
        CAST(:actor AS uuid), CAST(:dataset AS uuid), CAST(:item AS uuid),
        CAST(:reason AS text)
    )
    """
)
_DELETE_ITEM = text(
    """
    SELECT aap_assets_v1.delete_dataset_item(
        CAST(:operation AS uuid), CAST(:actor AS uuid), CAST(:dataset AS uuid),
        CAST(:item AS uuid), CAST(:bucket AS text), CAST(:context AS jsonb)
    )
    """
)
_CONTENT_HASH = "c" * 32


@dataclass
class _World:
    maker: async_sessionmaker[AsyncSession]
    actor_id: uuid.UUID
    other_actor_id: uuid.UUID
    dataset_id: uuid.UUID
    project_id: uuid.UUID
    association_id: uuid.UUID
    item_ids: set[uuid.UUID] = field(default_factory=set)
    operation_ids: set[uuid.UUID] = field(default_factory=set)
    audit_ids: set[int] = field(default_factory=set)


async def _seed_world(test_engine) -> _World:
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:10]
    actor_id = uuid.uuid4()
    other_actor_id = uuid.uuid4()
    dataset_id = uuid.uuid4()
    project_id = uuid.uuid4()
    async with maker() as db:
        db.add_all(
            [
                User(
                    id=actor_id,
                    email=f"dataset-upload-{suffix}@test.local",
                    name=f"Dataset upload {suffix}",
                    password_hash="test-only",
                    role="super_admin",
                    status="offline",
                    is_active=True,
                ),
                User(
                    id=other_actor_id,
                    email=f"dataset-upload-other-{suffix}@test.local",
                    name=f"Dataset upload other {suffix}",
                    password_hash="test-only",
                    role="employee",
                    status="offline",
                    is_active=True,
                ),
            ]
        )
        await db.flush()
        db.add_all(
            [
                Dataset(
                    id=dataset_id,
                    display_id=f"D-UP-{suffix}",
                    name=f"dataset-upload-{suffix}",
                    data_type="image",
                    created_by=actor_id,
                    file_count=0,
                ),
                Project(
                    id=project_id,
                    display_id=f"P-UP-{suffix}",
                    name=f"Dataset upload project {suffix}",
                    type_label="Image detection",
                    type_key="image",
                    data_type="image",
                    owner_id=actor_id,
                    scene_mode=False,
                    total_tasks=0,
                    tool_bindings={
                        "bbox": {
                            "enabled": True,
                            "classes": [{"name": "object", "order": 0}],
                            "attribute_schema": {"fields": []},
                        }
                    },
                ),
            ]
        )
        await db.flush()
        association = (
            await db.execute(
                _ASSOCIATE,
                {"actor": actor_id, "dataset": dataset_id, "project": project_id},
            )
        ).scalar_one()
        assert association["schema"] == "aap-dataset-association/v1"
        assert association["ok"] is True, association
        await db.commit()
    return _World(
        maker=maker,
        actor_id=actor_id,
        other_actor_id=other_actor_id,
        dataset_id=dataset_id,
        project_id=project_id,
        association_id=uuid.UUID(association["data"]["link_id"]),
    )


async def _cleanup_world(world: _World) -> None:
    async with world.maker() as db:
        await db.execute(delete(Task).where(Task.project_id == world.project_id))
        await db.execute(
            delete(ProjectDataset).where(ProjectDataset.dataset_id == world.dataset_id)
        )
        if world.item_ids:
            await db.execute(
                delete(TaskAppendOperation).where(
                    TaskAppendOperation.operation_id.in_(world.item_ids)
                )
            )
        await db.execute(
            text(
                "DELETE FROM public.dataset_upload_intents "
                "WHERE dataset_id = CAST(:dataset AS uuid)"
            ),
            {"dataset": world.dataset_id},
        )
        await db.execute(
            delete(DatasetItem).where(DatasetItem.dataset_id == world.dataset_id)
        )
        if world.operation_ids:
            await db.execute(
                text(
                    "DELETE FROM public.dataset_source_deletion_operations "
                    "WHERE operation_id = ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": list(world.operation_ids)},
            )
        if world.audit_ids:
            await db.execute(delete(AuditLog).where(AuditLog.id.in_(world.audit_ids)))
        await db.execute(delete(Dataset).where(Dataset.id == world.dataset_id))
        await db.execute(delete(Project).where(Project.id == world.project_id))
        await db.execute(
            delete(User).where(User.id.in_([world.actor_id, world.other_actor_id]))
        )
        await db.commit()


@pytest.fixture
async def upload_world(test_engine):
    world = await _seed_world(test_engine)
    try:
        yield world
    finally:
        await _cleanup_world(world)


def _new_item(world: _World) -> uuid.UUID:
    item_id = uuid.uuid4()
    world.item_ids.add(item_id)
    return item_id


def _new_operation(world: _World) -> uuid.UUID:
    operation_id = uuid.uuid4()
    world.operation_ids.add(operation_id)
    return operation_id


async def _reserve(
    db: AsyncSession,
    world: _World,
    item_id: uuid.UUID,
    *,
    actor_id: uuid.UUID | None = None,
    file_name: str = "frames/frame-001.jpg",
    content_type: str = "image/jpeg",
) -> dict:
    world.item_ids.add(item_id)
    return (
        await db.execute(
            _RESERVE,
            {
                "item": item_id,
                "actor": actor_id or world.actor_id,
                "dataset": world.dataset_id,
                "file_name": file_name,
                "content_type": content_type,
            },
        )
    ).scalar_one()


async def _lock(
    db: AsyncSession,
    world: _World,
    item_id: uuid.UUID,
    *,
    actor_id: uuid.UUID | None = None,
) -> dict:
    return (
        await db.execute(
            _LOCK,
            {
                "actor": actor_id or world.actor_id,
                "dataset": world.dataset_id,
                "item": item_id,
            },
        )
    ).scalar_one()


async def _find_duplicate(
    db: AsyncSession,
    world: _World,
    item_id: uuid.UUID,
    content_hash: str | None,
) -> dict:
    return (
        await db.execute(
            _FIND_DUPLICATE,
            {
                "actor": world.actor_id,
                "dataset": world.dataset_id,
                "item": item_id,
                "content_hash": content_hash,
            },
        )
    ).scalar_one()


async def _complete(
    db: AsyncSession,
    world: _World,
    item_id: uuid.UUID,
    publication_id: uuid.UUID,
    *,
    content_hash: str | None = _CONTENT_HASH,
) -> dict:
    return (
        await db.execute(
            _COMPLETE,
            {
                "actor": world.actor_id,
                "dataset": world.dataset_id,
                "item": item_id,
                "publication": publication_id,
                "file_size": 1234,
                "content_hash": content_hash,
                "width": 640,
                "height": 480,
            },
        )
    ).scalar_one()


async def _dataset_upload_row(db: AsyncSession, item_id: uuid.UUID):
    return (
        (
            await db.execute(
                text(
                    "SELECT actor_id, dataset_id, file_name, file_path, file_type, "
                    "content_type, staging_key, publication_id, state, result "
                    "FROM public.dataset_upload_intents WHERE item_id = CAST(:item AS uuid)"
                ),
                {"item": item_id},
            )
        )
        .mappings()
        .one()
    )


async def _count(db: AsyncSession, statement: str, params: dict) -> int:
    return int(await db.scalar(text(statement), params) or 0)


async def test_reservation_is_invisible_and_pending_locks_rotate_publication_after_rollback(
    upload_world: _World,
):
    world = upload_world
    item_id = _new_item(world)
    async with world.maker() as db:
        reserved = await _reserve(db, world, item_id)
        assert reserved["ok"] is True, reserved
        assert reserved["data"]["state"] == "pending"
        assert reserved["data"]["publication_id"] is None
        assert reserved["data"]["file_path"] is None
        assert reserved["data"]["staging_key"] == f"dataset-uploads/{item_id}/source"
        await db.commit()

    async with world.maker() as observer:
        assert await observer.get(DatasetItem, item_id) is None
        dataset = await observer.get(Dataset, world.dataset_id)
        project = await observer.get(Project, world.project_id)
        assert dataset.file_count == 0
        assert project.total_tasks == 0
        assert (
            await _count(
                observer,
                "SELECT count(*) FROM public.tasks WHERE project_id = CAST(:project AS uuid)",
                {"project": world.project_id},
            )
            == 0
        )

    async with world.maker() as first_attempt:
        first = await _lock(first_attempt, world, item_id)
        assert first["ok"] is True, first
        first_data = first["data"]
        assert first_data["file_path"].startswith(
            f"dataset-sources/{world.dataset_id}/{item_id}/"
        )
        await first_attempt.rollback()

    async with world.maker() as retry:
        second = await _lock(retry, world, item_id)
        assert second["ok"] is True, second
        assert second["data"]["publication_id"] != first_data["publication_id"]
        assert second["data"]["file_path"] != first_data["file_path"]
        await retry.rollback()


async def test_upload_scope_actor_state_roles_and_untrusted_inputs(
    upload_world: _World,
):
    world = upload_world
    item_id = _new_item(world)
    async with world.maker() as db:
        reserved = await _reserve(db, world, item_id)
        assert reserved["ok"] is True, reserved
        conflict = await _reserve(db, world, item_id, file_name="different-name.jpg")
        assert conflict["error"]["code"] == "operation_conflict"
        wrong_actor = await _lock(db, world, item_id, actor_id=world.other_actor_id)
        assert wrong_actor["error"]["code"] == "upload_scope_mismatch"
        missing_actor = await _lock(db, world, item_id, actor_id=uuid.uuid4())
        assert missing_actor["error"]["code"] == "account_not_active"
        await db.commit()

    async with world.maker() as db:
        await db.execute(
            update(User).where(User.id == world.actor_id).values(is_active=False)
        )
        inactive = await _lock(db, world, item_id)
        assert inactive["error"]["code"] == "account_not_active"
        await db.commit()
    async with world.maker() as db:
        await db.execute(
            update(User).where(User.id == world.actor_id).values(is_active=True)
        )
        await db.execute(
            update(User).where(User.id == world.other_actor_id).values(role="employee")
        )
        employee_upload = await _reserve(
            db, world, _new_item(world), actor_id=world.other_actor_id
        )
        assert employee_upload["ok"] is True, employee_upload
        await db.commit()

    viewer_item = _new_item(world)
    async with world.maker() as db:
        await db.execute(
            update(User).where(User.id == world.other_actor_id).values(role="viewer")
        )
        viewer_upload = await _reserve(
            db, world, viewer_item, actor_id=world.other_actor_id
        )
        assert viewer_upload["ok"] is True, viewer_upload
        await db.commit()

    unknown_item = _new_item(world)
    async with world.maker() as db:
        await db.execute(
            update(User).where(User.id == world.other_actor_id).values(role="unknown")
        )
        unknown_role = await _reserve(
            db, world, unknown_item, actor_id=world.other_actor_id
        )
        assert unknown_role["error"]["code"] == "invalid_account_role"
        await db.execute(
            update(User).where(User.id == world.other_actor_id).values(role="employee")
        )
        await db.commit()

    bad_names = (
        "",
        "/absolute.jpg",
        "C:/absolute.jpg",
        "../escape.jpg",
        "frames/../escape.jpg",
        "frames//image.jpg",
        "frames\\image.jpg",
        "frames/image\n.jpg",
        "a" * 501,
    )
    async with world.maker() as db:
        for bad_name in bad_names:
            invalid = await _reserve(db, world, _new_item(world), file_name=bad_name)
            assert invalid["error"]["code"] == "invalid_request", invalid
        invalid_type = await _reserve(
            db,
            world,
            _new_item(world),
            content_type="image/jpeg" + ("x" * 250),
        )
        assert invalid_type["error"]["code"] == "invalid_request", invalid_type
        await db.commit()

    async with world.maker() as db:
        await db.execute(
            update(Dataset)
            .where(Dataset.id == world.dataset_id)
            .values(name="__aap_trusted_nuscenes__-reserved")
        )
        await db.commit()
    async with world.maker() as db:
        reserved_root = await _reserve(db, world, _new_item(world))
        assert reserved_root["error"]["code"] == "invalid_request", reserved_root
        await db.commit()
    async with world.maker() as db:
        await db.execute(
            update(Dataset)
            .where(Dataset.id == world.dataset_id)
            .values(name="dataset-upload-restored")
        )
        await db.commit()

    async with world.maker() as observer:
        assert (
            await _count(
                observer,
                "SELECT count(*) FROM public.dataset_upload_intents "
                "WHERE dataset_id = CAST(:dataset AS uuid)",
                {"dataset": world.dataset_id},
            )
            == 3
        )


async def test_complete_appends_once_and_replays_frozen_result_after_unlink(
    upload_world: _World,
):
    world = upload_world
    item_id = _new_item(world)
    async with world.maker() as db:
        reserved = await _reserve(db, world, item_id)
        assert reserved["ok"] is True, reserved
        await db.commit()

    async with world.maker() as db:
        locked = await _lock(db, world, item_id)
        assert locked["ok"] is True, locked
        publication_id = uuid.UUID(locked["data"]["publication_id"])
        no_duplicate = await _find_duplicate(db, world, item_id, None)
        assert no_duplicate["ok"] is True
        assert no_duplicate["data"]["duplicate_of"] is None
        completed = await _complete(db, world, item_id, publication_id)
        assert completed["ok"] is True, completed
        assert completed["data"]["disposition"] == "accepted"
        assert completed["data"]["state"] == "completed"
        assert completed["data"]["result"] == {
            "item_id": str(item_id),
            "linked_tasks": 1,
        }
        await db.commit()

    async with world.maker() as observer:
        item = await observer.get(DatasetItem, item_id)
        dataset = await observer.get(Dataset, world.dataset_id)
        project = await observer.get(Project, world.project_id)
        task = await observer.scalar(
            select(Task).where(Task.dataset_item_id == item_id)
        )
        append = await observer.get(TaskAppendOperation, item_id)
        intent = await _dataset_upload_row(observer, item_id)
        assert item is not None
        assert item.file_path == completed["data"]["file_path"]
        assert item.content_hash == _CONTENT_HASH
        assert dataset.file_count == 1
        assert project.total_tasks == 1
        assert task is not None
        assert append is not None
        assert append.dataset_id == world.dataset_id
        assert append.item_ids == [item_id]
        assert intent["state"] == "completed"
        assert intent["result"] == {"item_id": str(item_id), "linked_tasks": 1}

    async with world.maker() as db:
        await db.execute(
            delete(ProjectDataset).where(
                ProjectDataset.dataset_id == world.dataset_id,
                ProjectDataset.project_id == world.project_id,
            )
        )
        await db.commit()

    async with world.maker() as replay:
        locked = await _lock(replay, world, item_id)
        assert locked["ok"] is True, locked
        assert locked["data"]["publication_id"] == str(publication_id)
        assert locked["data"]["result"] == {
            "item_id": str(item_id),
            "linked_tasks": 1,
        }
        repeated = await _complete(replay, world, item_id, publication_id)
        assert repeated == completed
        await replay.commit()

    async with world.maker() as observer:
        assert (
            await _count(
                observer,
                "SELECT count(*) FROM public.tasks WHERE dataset_item_id = CAST(:item AS uuid)",
                {"item": item_id},
            )
            == 1
        )
        project = await observer.get(Project, world.project_id)
        assert project.total_tasks == 1


async def test_duplicate_and_missing_discard_never_publish_a_dataset_item(
    upload_world: _World,
):
    world = upload_world
    existing_id = _new_item(world)
    async with world.maker() as db:
        db.add(
            DatasetItem(
                id=existing_id,
                dataset_id=world.dataset_id,
                file_name="existing.jpg",
                file_path="existing/existing.jpg",
                file_type="image",
                file_size=50,
                content_hash=_CONTENT_HASH,
            )
        )
        dataset = await db.get(Dataset, world.dataset_id)
        dataset.file_count = 1
        await db.commit()

    duplicate_id = _new_item(world)
    async with world.maker() as db:
        reserved = await _reserve(db, world, duplicate_id)
        assert reserved["ok"] is True, reserved
        await db.commit()
    async with world.maker() as db:
        locked = await _lock(db, world, duplicate_id)
        publication_id = uuid.UUID(locked["data"]["publication_id"])
        duplicate = await _find_duplicate(db, world, duplicate_id, _CONTENT_HASH)
        assert duplicate["data"]["duplicate_of"] == str(existing_id)
        completed = await _complete(
            db, world, duplicate_id, publication_id, content_hash=_CONTENT_HASH
        )
        assert completed["ok"] is True, completed
        assert completed["data"]["disposition"] == "duplicate"
        assert completed["data"]["duplicate_of"] == str(existing_id)
        await db.commit()

    missing_id = _new_item(world)
    async with world.maker() as db:
        reserved = await _reserve(db, world, missing_id)
        assert reserved["ok"] is True, reserved
        await db.commit()
    async with world.maker() as db:
        locked_missing = await _lock(db, world, missing_id)
        missing_publication = uuid.UUID(locked_missing["data"]["publication_id"])
        discarded = await db.execute(
            _DISCARD,
            {
                "actor": world.actor_id,
                "dataset": world.dataset_id,
                "item": missing_id,
                "reason": "missing",
            },
        )
        missing = discarded.scalar_one()
        assert missing["ok"] is True, missing
        assert missing["data"]["state"] == "discarded"
        assert missing["data"]["result"] == {"reason": "missing"}
        await db.commit()

    async with world.maker() as db:
        duplicate_row = await _dataset_upload_row(db, duplicate_id)
        missing_row = await _dataset_upload_row(db, missing_id)
        dataset = await db.get(Dataset, world.dataset_id)
        assert duplicate_row["state"] == "discarded"
        assert duplicate_row["result"] == {"duplicate_of": str(existing_id)}
        assert missing_row["state"] == "discarded"
        assert missing_row["result"] == {"reason": "missing"}
        assert missing_row["publication_id"] == missing_publication
        assert await db.get(DatasetItem, duplicate_id) is None
        assert await db.get(DatasetItem, missing_id) is None
        assert dataset.file_count == 1
        retry = await _reserve(db, world, missing_id)
        assert retry["error"]["code"] == "upload_not_pending"
        await db.commit()


async def test_labeling_append_failure_rolls_back_item_and_counter_then_retries(
    upload_world: _World,
):
    world = upload_world
    item_id = _new_item(world)
    conflicting_dataset_id = uuid.uuid4()
    async with world.maker() as db:
        db.add(
            TaskAppendOperation(
                operation_id=item_id,
                dataset_id=conflicting_dataset_id,
                item_ids=[uuid.uuid4()],
                result=None,
            )
        )
        reserved = await _reserve(db, world, item_id)
        assert reserved["ok"] is True, reserved
        await db.commit()

    async with world.maker() as db:
        locked = await _lock(db, world, item_id)
        publication_id = uuid.UUID(locked["data"]["publication_id"])
        failed = await _complete(db, world, item_id, publication_id)
        assert failed["ok"] is False
        assert failed["error"]["code"] == "operation_conflict"
        await db.commit()

    async with world.maker() as observer:
        dataset = await observer.get(Dataset, world.dataset_id)
        intent = await _dataset_upload_row(observer, item_id)
        assert dataset.file_count == 0
        assert await observer.get(DatasetItem, item_id) is None
        assert intent["state"] == "pending"
        assert intent["result"] is None
        assert (
            await observer.scalar(
                select(Task.id).where(Task.dataset_item_id == item_id)
            )
            is None
        )
        assert await observer.get(TaskAppendOperation, item_id) is not None

    async with world.maker() as retry:
        await retry.execute(
            delete(TaskAppendOperation).where(
                TaskAppendOperation.operation_id == item_id
            )
        )
        await retry.commit()
    async with world.maker() as retry:
        locked = await _lock(retry, world, item_id)
        new_publication = uuid.UUID(locked["data"]["publication_id"])
        assert new_publication != publication_id
        completed = await _complete(retry, world, item_id, new_publication)
        assert completed["ok"] is True, completed
        await retry.commit()

    async with world.maker() as observer:
        dataset = await observer.get(Dataset, world.dataset_id)
        assert dataset.file_count == 1
        assert await observer.get(DatasetItem, item_id) is not None
        assert (
            await observer.scalar(
                select(Task.id).where(Task.dataset_item_id == item_id)
            )
            is not None
        )


async def test_current_manager_source_delete_discards_a_pending_upload(
    upload_world: _World,
):
    world = upload_world
    item_id = _new_item(world)
    operation_id = _new_operation(world)
    async with world.maker() as db:
        reserved = await _reserve(db, world, item_id)
        assert reserved["ok"] is True, reserved
        await db.commit()

    async with world.maker() as db:
        await db.execute(
            update(User).where(User.id == world.actor_id).values(role="employee")
        )
        denied = (
            await db.execute(
                _DELETE_ITEM,
                {
                    "operation": operation_id,
                    "actor": world.actor_id,
                    "dataset": world.dataset_id,
                    "item": item_id,
                    "bucket": "media-cache-test",
                    "context": None,
                },
            )
        ).scalar_one()
        assert denied["ok"] is False
        assert denied["error"]["code"] == "catalog_manager_required"
        await db.execute(
            update(User).where(User.id == world.actor_id).values(role="super_admin")
        )
        await db.commit()

    async with world.maker() as db:
        deleted = (
            await db.execute(
                _DELETE_ITEM,
                {
                    "operation": operation_id,
                    "actor": world.actor_id,
                    "dataset": world.dataset_id,
                    "item": item_id,
                    "bucket": "media-cache-test",
                    "context": None,
                },
            )
        ).scalar_one()
        assert deleted["ok"] is True, deleted
        assert deleted["data"]["discarded_uploads"] == 1
        assert deleted["data"]["deleted_items"] == 0
        world.audit_ids.add(deleted["data"]["audit_id"])
        await db.commit()

    async with world.maker() as observer:
        intent = await _dataset_upload_row(observer, item_id)
        dataset = await observer.get(Dataset, world.dataset_id)
        assert intent["state"] == "discarded"
        assert intent["result"] == {"reason": "source_deleted"}
        assert await observer.get(DatasetItem, item_id) is None
        assert dataset.file_count == 0
        retry = await _reserve(observer, world, item_id)
        assert retry["error"]["code"] == "upload_not_pending"
        await observer.commit()


async def test_configured_append_workflow_login_can_complete_native_upload(
    upload_world: _World,
):
    workflow_url = os.getenv("AAP_MODULE_DATA_APPEND_URL")
    if not workflow_url:
        pytest.skip("AAP_MODULE_DATA_APPEND_URL is not configured")
    world = upload_world
    item_id = _new_item(world)
    engine = create_async_engine(workflow_url)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker() as db:
            data = DatasetUploadData(db)
            reserved = await data.reserve(
                DatasetUploadReserve(
                    item_id=item_id,
                    actor_id=world.actor_id,
                    dataset_id=world.dataset_id,
                    file_name="restricted/frame.jpg",
                    content_type="image/jpeg",
                )
            )
            await db.commit()
            assert reserved.state == "pending"
        async with maker() as db:
            data = DatasetUploadData(db)
            locked = await data.lock(
                actor_id=world.actor_id,
                dataset_id=world.dataset_id,
                item_id=item_id,
            )
            assert locked.publication_id is not None
            completed = await data.complete(
                DatasetUploadCompletion(
                    actor_id=world.actor_id,
                    dataset_id=world.dataset_id,
                    item_id=item_id,
                    publication_id=locked.publication_id,
                    file_size=100,
                    content_hash=_CONTENT_HASH,
                    width=10,
                    height=10,
                )
            )
            assert completed.disposition == "accepted"
            await db.commit()
    finally:
        await engine.dispose()

    async with world.maker() as observer:
        assert await observer.get(DatasetItem, item_id) is not None
        assert (
            await observer.scalar(
                select(Task.id).where(Task.dataset_item_id == item_id)
            )
            is not None
        )
