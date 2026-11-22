"""Media admission keeps source and authority fences without excluding readers."""

from __future__ import annotations

import json
import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from yoao_assets_images.policy import ImagePolicy
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.task import Task
from app.services.content_snapshot import ContentError
from app.services.dataset_assets import register_item_asset
from app.services.original_sources import resolve_task
from tests.factory import create_project
from tests.test_assets_media_live import _guard_role_url
from tests.test_identity_auth_native import _cleanup, _seed
from tests.test_labeling_class_usage_reads_legacy import _legacy_runtime_url


@pytest.mark.asyncio
async def test_image_admission_shares_task_authority_and_serializes_work(test_engine):
    manager = await _seed(test_engine, create_key=False, role="super_admin")
    stranger = await _seed(test_engine, create_key=False)
    actor_id = manager["actor_id"]
    task_id = uuid.uuid4()
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker.begin() as session:
        project = await create_project(session, owner_id=actor_id)
        project_id = project.id
        session.add(
            Task(
                id=task_id,
                project_id=project_id,
                display_id=f"T-ADMIT-{task_id.hex[:12]}",
                file_name="source.png",
                file_path=f"admission/{task_id}.png",
                file_type="image",
                status="pending",
                assignee_id=actor_id,
                media_source_generation=1,
                media_object_identity={
                    "etag": "source",
                    "byte_size": 128,
                    "version_id": None,
                },
                media_source_drifted=False,
            )
        )
    prepare = text(
        "SELECT yoao_assets_v1.prepare_image_media("
        "CAST(:operation AS uuid), CAST(:actor AS uuid), CAST(:task AS uuid), "
        "NULL::uuid, 'thumbnail', NULL::text, CAST(:policy AS jsonb), false)"
    )
    params = {
        "operation": uuid.uuid4(),
        "actor": actor_id,
        "task": task_id,
        "policy": json.dumps(ImagePolicy().model_dump(mode="json")),
    }
    try:
        async with maker() as writer, maker() as reader:
            accepted = await writer.scalar(prepare, params)
            assert accepted["ok"] is True
            rows = await reader.scalar(
                text(
                    "SELECT count(*) FROM yoao_labeling_v1.lock_task_for_core_read("
                    ":task, 'share', true, NULL)"
                ),
                {"task": task_id},
            )
            assert rows == 1
            await reader.rollback()

            async with maker() as unauthorized:
                denied = await unauthorized.scalar(
                    prepare,
                    {
                        **params,
                        "operation": uuid.uuid4(),
                        "actor": stranger["actor_id"],
                    },
                )
                assert denied["error"]["code"] == "task_not_visible"

            for statement, identifier in (
                (
                    "UPDATE public.tasks SET file_path='changed.png' WHERE id=:id",
                    task_id,
                ),
                ("UPDATE public.users SET is_active=false WHERE id=:id", actor_id),
            ):
                async with maker() as mutator:
                    await mutator.execute(text("SET LOCAL lock_timeout='200ms'"))
                    with pytest.raises(DBAPIError) as blocked:
                        await mutator.execute(text(statement), {"id": identifier})
                    assert blocked.value.orig.sqlstate == "55P03"
                    await mutator.rollback()

            async with maker() as contender:
                await contender.execute(text("SET LOCAL lock_timeout='200ms'"))
                conflict = await contender.scalar(
                    prepare, {**params, "operation": uuid.uuid4()}
                )
                assert conflict["error"]["code"] in {
                    "operation_busy",
                    "operation_conflict",
                }
                await contender.rollback()
            assert (
                await writer.scalar(
                    text(
                        "SELECT count(*) FROM public.asset_image_jobs "
                        "WHERE owner_kind='task' AND owner_id=:id AND slot_key='thumbnail'"
                    ),
                    {"id": task_id},
                )
                == 1
            )
            await writer.rollback()
    finally:
        async with maker.begin() as session:
            await session.execute(
                text("DELETE FROM public.tasks WHERE id=:id"), {"id": task_id}
            )
            await session.execute(
                text("DELETE FROM public.projects WHERE id=:id"), {"id": project_id}
            )
        await _cleanup(test_engine, stranger)
        await _cleanup(test_engine, manager)


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
    reason="requires owned Native role database",
)
async def test_task_thumbnail_admission_shares_exact_original_item_fence(test_engine):
    image_role = "yoao_asset_media_control_client"
    image_url = _guard_role_url(
        "AAP_MODULE_DATA_ASSET_MEDIA_CONTROL_URL", image_role, test_engine
    )
    legacy_url = _legacy_runtime_url(test_engine)
    image_engine = create_async_engine(image_url, hide_parameters=True)
    legacy_engine = create_async_engine(legacy_url, hide_parameters=True)
    owner = async_sessionmaker(test_engine)
    image = async_sessionmaker(image_engine)
    original = async_sessionmaker(legacy_engine)
    manager = await _seed(test_engine, create_key=False, role="super_admin")
    actor_id = manager["actor_id"]
    dataset_id, item_id, task_id = (uuid.uuid4() for _ in range(3))
    object_key = f"admission/{item_id}.png"
    expected_sha = "a" * 64
    project_id = None
    prepare = text(
        "SELECT yoao_assets_v1.prepare_image_media("
        "CAST(:operation AS uuid), CAST(:actor AS uuid), CAST(:task AS uuid), "
        "NULL::uuid, 'thumbnail', NULL::text, CAST(:policy AS jsonb), false)"
    )
    params = {
        "operation": uuid.uuid4(),
        "actor": actor_id,
        "task": task_id,
        "policy": json.dumps(ImagePolicy().model_dump(mode="json")),
    }
    change_source = text("UPDATE public.dataset_items SET file_path=:path WHERE id=:id")
    change = {"path": f"admission/{item_id}-changed.png", "id": item_id}
    try:
        async with owner.begin() as db:
            project = await create_project(db, owner_id=actor_id)
            project_id = project.id
            db.add(
                Dataset(
                    id=dataset_id,
                    display_id=f"DS-{dataset_id.hex[:12]}",
                    name="Original image",
                    data_type="image",
                    created_by=actor_id,
                )
            )
            await db.flush()
            item = DatasetItem(
                id=item_id,
                dataset_id=dataset_id,
                file_name="source.png",
                file_path=object_key,
                file_type="image",
                file_size=128,
                width=64,
                height=48,
                media_object_identity={"etag": "source", "byte_size": 128},
            )
            db.add(item)
            await db.flush()
            asset = await register_item_asset(
                db, item, sha256=expected_sha, size_bytes=128
            )
            asset_id = asset.id
            db.add(
                Task(
                    id=task_id,
                    project_id=project_id,
                    display_id=f"T-ADMIT-{task_id.hex[:12]}",
                    file_name="source.png",
                    file_path=object_key,
                    file_type="image",
                    status="pending",
                    assignee_id=actor_id,
                    dataset_item_id=item_id,
                )
            )

        async with image() as admission, original() as reader, owner() as writer:
            assert await admission.scalar(text("SELECT session_user")) == image_role
            assert await reader.scalar(text("SELECT session_user")) == "legacy_runtime"
            await admission.execute(text("SET LOCAL statement_timeout='2s'"))
            await reader.execute(text("SET LOCAL statement_timeout='2s'"))
            accepted = await admission.scalar(prepare, params)
            assert accepted["ok"] is True
            assert accepted["data"]["source"]["item_id"] == str(item_id)
            assert accepted["data"]["source"]["file_path"] == object_key
            assert await admission.scalar(prepare, params) == accepted
            source = await resolve_task(reader, task_id, lock=True)
            assert (
                source.item_id,
                source.content_id,
                source.sha256,
                source.object_key,
                source.size_bytes,
            ) == (item_id, asset_id, expected_sha, object_key, 128)
            await reader.rollback()
            await writer.execute(text("SET LOCAL lock_timeout='200ms'"))
            with pytest.raises(DBAPIError) as blocked:
                await writer.execute(change_source, change)
            assert blocked.value.orig.sqlstate == "55P03"
            await writer.rollback()
            await admission.rollback()

            await writer.execute(change_source, change)
            await admission.execute(text("SET LOCAL statement_timeout='2s'"))
            refused = await admission.scalar(prepare, params)
            assert refused["error"]["code"] == "source_busy"
            await reader.execute(text("SET LOCAL statement_timeout='2s'"))
            with pytest.raises(ContentError, match="storage_original_busy"):
                await resolve_task(reader, task_id, lock=True)
            await writer.rollback()
            assert await resolve_task(reader, task_id, lock=True) == source
    finally:
        await image_engine.dispose()
        await legacy_engine.dispose()
        async with owner.begin() as db:
            await db.execute(
                text("DELETE FROM public.tasks WHERE id=:id"), {"id": task_id}
            )
            await db.execute(
                text("DELETE FROM public.datasets WHERE id=:id"), {"id": dataset_id}
            )
            if project_id is not None:
                await db.execute(
                    text("DELETE FROM public.projects WHERE id=:id"), {"id": project_id}
                )
        await _cleanup(test_engine, manager)
