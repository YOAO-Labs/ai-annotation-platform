"""Focused native cleanup leases and retained whole-asset resweeps.

These database regressions use the repository's disposable ``test_engine``
fixture. They are not run by the image-cleanup package's pure test suite.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.project import Project
from app.db.models.image_pyramid import ImagePyramidAsset
from app.db.models.task import Task
from app.db.models.user import User

pytestmark = pytest.mark.asyncio


def _reply(value):
    return json.loads(value) if isinstance(value, str) else value


async def test_retained_storage_outbox_keeps_wire_and_resweeps_after_ack(test_engine):
    operation_id, asset_id = uuid.uuid4(), uuid.uuid4()
    created = datetime.now(timezone.utc)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        await session.begin()
        try:
            await session.execute(
                text(
                    "INSERT INTO public.asset_storage_deletions "
                    "(operation_id, asset_id, bucket, prefix, created_at, "
                    "completed_at, retained, not_before) "
                    "VALUES (:operation_id, :asset_id, :bucket, :prefix, "
                    ":created_at, :completed_at, true, clock_timestamp())"
                ),
                {
                    "operation_id": operation_id,
                    "asset_id": asset_id,
                    "bucket": "media-cache-test",
                    "prefix": f"image-pyramids/{asset_id}/",
                    "created_at": created,
                    "completed_at": created,
                },
            )
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT operation_id, asset_id, bucket, prefix, created_at "
                            "FROM aap_assets_v1.pending_storage_deletions_v2("
                            ":bucket, NULL::timestamptz, NULL::uuid, NULL::uuid, 10)"
                        ),
                        {"bucket": "media-cache-test"},
                    )
                )
                .mappings()
                .all()
            )
            assert len(rows) == 1
            assert set(rows[0]) == {
                "operation_id",
                "asset_id",
                "bucket",
                "prefix",
                "created_at",
            }
            assert rows[0]["operation_id"] == operation_id
            assert rows[0]["prefix"] == f"image-pyramids/{asset_id}/"

            acknowledged = await session.scalar(
                text(
                    "SELECT aap_assets_v1.complete_storage_deletion("
                    ":operation_id, :asset_id)"
                ),
                {"operation_id": operation_id, "asset_id": asset_id},
            )
            assert acknowledged is True
            assert (
                await session.scalars(
                    text(
                        "SELECT operation_id FROM "
                        "aap_assets_v1.pending_storage_deletions_v2("
                        ":bucket, NULL::timestamptz, NULL::uuid, NULL::uuid, 10)"
                    ),
                    {"bucket": "media-cache-test"},
                )
            ).all() == []

            # Fast-forward the retained tombstone instead of waiting for its sweep interval.
            await session.execute(
                text(
                    "UPDATE public.asset_storage_deletions "
                    "SET not_before = clock_timestamp() "
                    "WHERE operation_id=:operation_id AND asset_id=:asset_id"
                ),
                {"operation_id": operation_id, "asset_id": asset_id},
            )
            replay = await session.scalars(
                text(
                    "SELECT operation_id FROM "
                    "aap_assets_v1.pending_storage_deletions_v2("
                    ":bucket, NULL::timestamptz, NULL::uuid, NULL::uuid, 10)"
                ),
                {"bucket": "media-cache-test"},
            )
            assert replay.all() == [operation_id]
            assert (
                await session.scalar(
                    text(
                        "SELECT aap_assets_v1.complete_storage_deletion("
                        ":operation_id, :asset_id)"
                    ),
                    {"operation_id": operation_id, "asset_id": asset_id},
                )
                is True
            )
        finally:
            await session.rollback()


async def test_whole_pyramid_claim_waits_for_task_thumbnail_and_ack_replays(
    test_engine,
):
    user_id, project_id, task_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    garbage_id, asset_id = uuid.uuid4(), uuid.uuid4()
    prefix = f"image-pyramids/{asset_id}/"
    worker_id = uuid.uuid4()
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        await session.begin()
        try:
            session.add(
                User(
                    id=user_id,
                    email=f"image-cleanup-{user_id.hex}@example.test",
                    name="Image cleanup fixture",
                    password_hash="test-only",
                    role="super_admin",
                    is_active=True,
                )
            )
            await session.flush()
            session.add(
                Project(
                    id=project_id,
                    display_id=f"IC-{project_id.hex[:8]}",
                    name="Image cleanup fixture",
                    type_key="image",
                    type_label="Image",
                    data_type="image",
                    owner_id=user_id,
                )
            )
            await session.flush()
            session.add(
                Task(
                    id=task_id,
                    project_id=project_id,
                    display_id=f"IC-T-{task_id.hex[:8]}",
                    file_name="image.png",
                    file_path=f"images/{task_id}.png",
                    file_type="image",
                    thumbnail_path=prefix + "overview.webp",
                )
            )
            await session.flush()
            await session.execute(
                text(
                    "INSERT INTO public.asset_image_garbage "
                    "(garbage_id, prefix, reason, not_before) "
                    "VALUES (:garbage_id, :prefix, 'project_deleted', clock_timestamp())"
                ),
                {"garbage_id": garbage_id, "prefix": prefix},
            )

            blocked = _reply(
                await session.scalar(
                    text(
                        "SELECT aap_assets_v1.claim_image_garbage("
                        "CAST(:worker_id AS uuid), :lease_seconds, :batch_size)"
                    ),
                    {
                        "worker_id": worker_id,
                        "lease_seconds": 300,
                        "batch_size": 5,
                    },
                )
            )
            assert blocked["schema"] == "aap-assets-images/v1"
            assert blocked["ok"] is True
            assert blocked["data"]["items"] == []

            task = await session.get(Task, task_id)
            assert task is not None
            task.thumbnail_path = None
            await session.flush()
            await session.execute(
                text(
                    "UPDATE public.asset_image_garbage "
                    "SET not_before=clock_timestamp() WHERE garbage_id=:id"
                ),
                {"id": garbage_id},
            )
            claimed = _reply(
                await session.scalar(
                    text(
                        "SELECT aap_assets_v1.claim_image_garbage("
                        "CAST(:worker_id AS uuid), :lease_seconds, :batch_size)"
                    ),
                    {
                        "worker_id": worker_id,
                        "lease_seconds": 300,
                        "batch_size": 5,
                    },
                )
            )
            items = claimed["data"]["items"]
            assert len(items) == 1
            assert items[0]["garbage_id"] == str(garbage_id)
            assert items[0]["prefix"] == prefix
            lease_token = uuid.UUID(items[0]["lease_token"])
            complete_sql = text(
                "SELECT aap_assets_v1.complete_image_garbage("
                "CAST(:garbage_id AS uuid), CAST(:lease_token AS uuid))"
            )
            parameters = {"garbage_id": garbage_id, "lease_token": lease_token}

            completed = _reply(await session.scalar(complete_sql, parameters))
            assert completed["schema"] == "aap-assets-images/v1"
            assert completed["ok"] is True
            assert completed["data"] == {
                "garbage_id": str(garbage_id),
                "completed": True,
            }
            replayed = _reply(await session.scalar(complete_sql, parameters))
            assert replayed["data"] == completed["data"]
        finally:
            await session.rollback()


async def test_project_delete_queues_whole_pyramid_prefix_before_removing_asset(
    test_engine,
):
    user_id, project_id, task_id, asset_id = (
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
    )
    prefix = f"image-pyramids/{asset_id}/"
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        await session.begin()
        try:
            session.add(
                User(
                    id=user_id,
                    email=f"image-project-cleanup-{user_id.hex}@example.test",
                    name="Image project cleanup fixture",
                    password_hash="test-only",
                    role="super_admin",
                    is_active=True,
                )
            )
            await session.flush()
            session.add(
                Project(
                    id=project_id,
                    display_id=f"IP-{project_id.hex[:8]}",
                    name="Image project cleanup fixture",
                    type_key="image",
                    type_label="Image",
                    data_type="image",
                    owner_id=user_id,
                )
            )
            await session.flush()
            session.add(
                Task(
                    id=task_id,
                    project_id=project_id,
                    display_id=f"IP-T-{task_id.hex[:8]}",
                    file_name="image.png",
                    file_path=f"images/{task_id}.png",
                    file_type="image",
                )
            )
            session.add(
                ImagePyramidAsset(
                    id=asset_id,
                    task_id=task_id,
                    profile_version="cleanup-test-v1",
                )
            )
            await session.flush()

            deleted = await session.scalar(
                text("SELECT aap_assets_v1.delete_project_owned_records (:project_id)"),
                {"project_id": project_id},
            )
            assert deleted == 1
            assert await session.get(ImagePyramidAsset, asset_id) is None
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT prefix, reason FROM public.asset_image_garbage "
                            "WHERE prefix=:prefix"
                        ),
                        {"prefix": prefix},
                    )
                )
                .mappings()
                .all()
            )
            assert rows == [{"prefix": prefix, "reason": "project_deleted"}]
        finally:
            await session.rollback()
