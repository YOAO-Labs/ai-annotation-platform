"""Exercise original fences through the isolated, restricted PostgreSQL LOGIN."""

import asyncio
from datetime import timedelta
from hashlib import sha256
from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi import HTTPException
import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core import token_blacklist
from app.core.security import create_access_token
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.dataset_asset import DatasetAsset, DatasetAssetLocation
from app.db.models.project_member import ProjectMember
from app.db.models.storage_maintenance import StorageOperation
from app.services import managed_original_reclamation, managed_original_retention
from app.services import original_binary, original_sources, storage_access
from app.services.content_snapshot import ContentError
from app.services.dataset_assets import LEGACY_LOCATION_ID, ensure_legacy_bindings
from tests.factory import admitted_request, create_project, create_task, create_user
from tests.storage_migration_isolation import native_reclaim_db as native_reclaim_db
from tests.storage_migration_isolation import native_reclaim_only
from tests.test_labeling_class_usage_reads_legacy import _legacy_runtime_url
from tests.test_managed_original_reclaim_fences import claim
from tests.test_managed_original_retention import change, confirmed


async def assert_restricted(db, *relations):
    assert await db.scalar(text("SELECT session_user")) == "legacy_runtime"
    denied = await db.scalar(
        text("""SELECT bool_and(NOT has_any_column_privilege(session_user, name, 'SELECT'))
            FROM unnest(CAST(:relations AS text[])) n(name)"""),
        {"relations": [f"public.{name}" for name in relations]},
    )
    assert denied is True


@native_reclaim_only
async def test_restricted_original_wait_and_permission_recheck(
    native_reclaim_db, test_engine, monkeypatch
):
    owner, admin = native_reclaim_db
    actor = await create_user(
        owner, "employee", f"{uuid4()}@original.test", "Original reader"
    )
    project = await create_project(owner, owner_id=admin.id)
    member = ProjectMember(project_id=project.id, user_id=actor.id, role="annotator")
    dataset = Dataset(
        id=uuid4(),
        display_id=f"DS-{uuid4().hex[:14]}",
        name="Originals",
        created_by=admin.id,
    )
    owner.add_all([member, dataset])
    await owner.flush()
    member_id = member.id
    item = DatasetItem(
        id=uuid4(),
        dataset_id=dataset.id,
        file_name="original.jpg",
        file_path=f"restricted-original/{uuid4()}",
        file_type="image",
    )
    owner.add(item)
    await owner.flush()
    digest = sha256(b"original").hexdigest()
    asset = DatasetAsset(
        id=uuid4(),
        dataset_item_id=item.id,
        media_type="image",
        size_bytes=8,
        verification_state="verified",
        checksum_algorithm="sha256",
        checksum_value=digest,
    )
    owner.add(asset)
    await owner.flush()
    binding = await ensure_legacy_bindings(owner)
    owner.add(
        DatasetAssetLocation(
            asset_id=asset.id,
            location_id=LEGACY_LOCATION_ID,
            bucket=binding.bucket,
            object_key=item.file_path,
            role="managed_original",
            reference_state="present",
        )
    )
    task = await create_task(owner, project_id=project.id, status="in_progress")
    task.dataset_item_id, task.file_path, task.assignee_id = (
        item.id,
        item.file_path,
        actor.id,
    )
    actor_snapshot = type(
        "_ActorSnapshot",
        (),
        {
            "id": actor.id,
            "name": actor.name,
            "email": actor.email,
            "role": actor.role,
            "is_active": actor.is_active,
        },
    )()
    task_id = task.id
    item_id = item.id
    item_file_path = item.file_path
    asset_id = asset.id
    await owner.commit()
    runtime = create_async_engine(
        _legacy_runtime_url(test_engine), hide_parameters=True
    )
    caller = async_sessionmaker(runtime, expire_on_commit=False)
    monkeypatch.setattr(
        token_blacklist, "is_blacklisted", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(
        token_blacklist, "get_user_generation", AsyncMock(return_value=0)
    )
    try:
        async with caller() as db:
            await assert_restricted(
                db,
                "dataset_items",
                "dataset_assets",
                "dataset_asset_locations",
                "task_originals",
            )
            backend = await db.scalar(text("SELECT pg_backend_pid()"))
            await owner.execute(
                select(DatasetItem.id)
                .where(DatasetItem.id == item_id)
                .with_for_update()
            )
            reading = asyncio.create_task(
                original_sources.resolve_task(
                    db, task_id, lock=True, wait_for_item=True
                )
            )
            try:
                async with test_engine.connect() as observer, asyncio.timeout(0.9):
                    while not await observer.scalar(
                        text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"),
                        {"pid": backend},
                    ):
                        await asyncio.sleep(0.01)
            finally:
                await owner.commit()
                source = await reading
            assert (source.content_id, source.sha256, source.object_key) == (
                asset_id,
                digest,
                item_file_path,
            )
            await db.rollback()

            await owner.execute(
                select(DatasetItem.id)
                .where(DatasetItem.id == item_id)
                .with_for_update()
            )
            await db.execute(text("SET LOCAL lock_timeout = '25ms'"))
            try:
                with pytest.raises(ContentError) as busy:
                    await original_sources.resolve_task(
                        db, task_id, lock=True, wait_for_item=True
                    )
                assert busy.value.code == "storage_original_busy"
                assert await db.scalar(text("SHOW lock_timeout")) == "25ms"
            finally:
                await owner.rollback()
                await db.rollback()

            token = create_access_token(
                str(actor_snapshot.id), actor_snapshot.role, timedelta(minutes=5)
            )
            request = admitted_request(actor_snapshot, token=token)
            context = await original_binary.authorize_original(
                db,
                original_binary.Resource(kind="task", task_id=task_id),
                actor_snapshot,
                request=request,
                expected_content_id=asset_id,
                expected_sha256=digest,
            )
            acquire = storage_access.acquire

            async def revoke_after_admission(*args, **kwargs):
                permit = await acquire(*args, **kwargs)
                await owner.execute(
                    delete(ProjectMember).where(ProjectMember.id == member_id)
                )
                await owner.commit()
                return permit

            monkeypatch.setattr(storage_access, "acquire", revoke_after_admission)
            capture = AsyncMock(
                side_effect=AssertionError("revoked original must not reach storage")
            )
            with pytest.raises(HTTPException) as denied:
                await original_binary.capture_authorized(db, context, capture)
            assert denied.value.status_code in {403, 404}
            capture.assert_not_awaited()
            assert (
                await owner.scalar(
                    select(StorageOperation.id).where(
                        StorageOperation.purpose
                        == f"dataset_original_read_{asset_id.hex}"
                    )
                )
                is None
            )
    finally:
        await runtime.dispose()


@native_reclaim_only
async def test_restricted_original_retention_refuses_claimed_object(
    native_reclaim_db, test_engine
):
    owner, actor = native_reclaim_db
    retirement, _, _ = await claim(owner, actor_id=actor.id)
    await owner.commit()
    runtime = create_async_engine(
        _legacy_runtime_url(test_engine), hide_parameters=True
    )
    try:
        async with async_sessionmaker(runtime, expire_on_commit=False)() as db:
            await assert_restricted(
                db,
                "managed_original_retirements",
                "managed_original_reclaim_claims",
                "managed_original_creations",
            )
            observed = await managed_original_reclamation.observe(db, retirement.id)
            assert observed["anchor"]["creation_id"] == str(retirement.creation_id)
            candidate = change(action="add_hold")
            review = await managed_original_retention.preview(
                db, retirement.id, candidate, actor
            )
            with pytest.raises(HTTPException) as rejected:
                await managed_original_retention.apply(
                    db, retirement.id, confirmed(candidate, review), actor
                )
            assert rejected.value.detail == {"code": "managed_original_reclaim_claimed"}
            await db.rollback()
            assert (
                await managed_original_retention.state(db, retirement.id, actor)
            ).policy_revision == 1
    finally:
        await runtime.dispose()
