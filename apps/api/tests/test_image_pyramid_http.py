"""Committed HTTP authority tests with independent real read/check transactions.

Only the provider byte transport and URL signer are fake. Setup and revocations
commit through separate sessions so the real output reader cannot accidentally
inherit the SAVEPOINT test fixture's identity map or uncommitted authority.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
import uuid

import httpx
import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.security import hash_password
from app.db.models.api_key import ApiKey
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.image_pyramid import (
    ImagePyramidAsset,
    ImagePyramidGeneration,
    ImagePyramidOutput,
)
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.storage_maintenance import StorageOperation
from app.db.models.task import Task
from app.db.models.user import User
from app.deps import get_db
from app.services import api_key_service
from app.services import image_pyramid as pyramids
from app.services import image_pyramid_objects as objects
from app.services import image_pyramid_originals as originals
from app.services import image_pyramid_storage as physical
from tests.image_pyramid_fixtures import (
    PhysicalTransport,
    make_image_task,
    ready_generation,
)


@pytest.fixture
async def committed_pyramid(test_engine, app_module, monkeypatch):
    """Own every business row; never remove shared Location/Binding/Gate rows."""
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    state = SimpleNamespace(
        factory=factory,
        owner_id=uuid.uuid4(),
        actor_id=uuid.uuid4(),
        task_id=None,
        item_id=None,
        project_id=None,
        dataset_id=None,
        asset_id=None,
        output_id=None,
    )
    state.transport = PhysicalTransport(monkeypatch)
    old_override = app_module.dependency_overrides.get(get_db)

    async def request_db():
        async with factory() as db:
            yield db

    try:
        async with factory() as db:
            db.add_all(
                [
                    User(
                        id=state.owner_id,
                        email=f"pyramid-owner-{state.owner_id}@test.local",
                        name="Pyramid owner",
                        password_hash=hash_password("fixture-password"),
                        role="project_admin",
                        is_active=True,
                    ),
                    User(
                        id=state.actor_id,
                        email=f"pyramid-reader-{state.actor_id}@test.local",
                        name="Pyramid employee",
                        password_hash=hash_password("fixture-password"),
                        role="employee",
                        is_active=True,
                    ),
                ]
            )
            await db.flush()
            task, item = await make_image_task(db, state.owner_id)
            state.task_id, state.item_id = task.id, item.id
            state.project_id, state.dataset_id = task.project_id, item.dataset_id
            task.assignee_id = state.actor_id
            db.add(
                ProjectMember(
                    project_id=task.project_id,
                    user_id=state.actor_id,
                    role="annotator",
                    assigned_by=state.owner_id,
                )
            )
            actor = await db.get(User, state.actor_id)
            key, state.token = await api_key_service.create_key(
                db, actor, "Pyramid read only", ["annotations:read"]
            )
            state.key_id = key.id
            asset, generation, output, manifest = await ready_generation(db, item)
            state.asset_id, state.output_id = asset.id, output.id
            state.manifest, state.original = manifest, generation.original_ref
            state.transport.register(output, manifest)
            await db.commit()
        state.headers = {"Authorization": f"Bearer {state.token}"}
        state.route = f"/api/v1/tasks/{state.task_id}/image-pyramid"
        app_module.dependency_overrides[get_db] = request_db
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_module), base_url="http://test"
        ) as client:
            state.client = client
            yield state
    finally:
        if old_override is None:
            app_module.dependency_overrides.pop(get_db, None)
        else:
            app_module.dependency_overrides[get_db] = old_override
        async with factory() as db:
            # Output rows intentionally outlive business cascades in production.
            # Remove this test's exact rows only, including any leaked permit.
            if state.output_id is not None:
                await db.execute(
                    delete(StorageOperation).where(
                        StorageOperation.purpose == objects.purpose(state.output_id)
                    )
                )
            if state.asset_id is not None:
                await db.execute(
                    delete(ImagePyramidGeneration).where(
                        ImagePyramidGeneration.asset_id == state.asset_id
                    )
                )
                await db.execute(
                    delete(ImagePyramidAsset).where(
                        ImagePyramidAsset.id == state.asset_id
                    )
                )
            if state.output_id is not None:
                # There are no real provider objects in this transport fixture.
                # Still honor the production ledger's forward-only settlement
                # and deletion guards rather than disabling database triggers.
                await db.execute(
                    update(ImagePyramidOutput)
                    .where(
                        ImagePyramidOutput.id == state.output_id,
                        ImagePyramidOutput.state.in_(("building", "ready")),
                    )
                    .values(state="retiring")
                )
                await db.execute(
                    update(ImagePyramidOutput)
                    .where(
                        ImagePyramidOutput.id == state.output_id,
                        ImagePyramidOutput.state == "retiring",
                    )
                    .values(state="deleted")
                )
                await db.execute(
                    delete(ImagePyramidOutput).where(
                        ImagePyramidOutput.id == state.output_id
                    )
                )
            if state.task_id is not None:
                await db.execute(delete(Task).where(Task.id == state.task_id))
            if state.project_id is not None:
                await db.execute(
                    delete(ProjectMember).where(
                        ProjectMember.project_id == state.project_id
                    )
                )
                await db.execute(delete(Project).where(Project.id == state.project_id))
            if state.item_id is not None:
                await db.execute(
                    delete(DatasetItem).where(DatasetItem.id == state.item_id)
                )
            if state.dataset_id is not None:
                await db.execute(delete(Dataset).where(Dataset.id == state.dataset_id))
            await db.execute(
                delete(ApiKey).where(
                    ApiKey.user_id.in_([state.owner_id, state.actor_id])
                )
            )
            await db.execute(
                delete(User).where(User.id.in_([state.owner_id, state.actor_id]))
            )
            await db.commit()


@pytest.mark.parametrize("archived", [False, True])
async def test_read_only_key_keeps_task_access_without_dataset_manager_or_lifecycle_gate(
    committed_pyramid, archived
):
    state = committed_pyramid
    if archived:
        async with state.factory() as db:
            await db.execute(
                update(Dataset)
                .where(Dataset.id == state.dataset_id)
                .values(archived_at=pyramids.utcnow())
            )
            await db.commit()
    response = await state.client.get(state.route, headers=state.headers)
    assert response.status_code == 200, response.text
    assert (
        response.json()["status"] == "ready"
        and response.json()["manifest"] == state.manifest
    )
    assert state.transport.verified and state.transport.signed
    async with state.factory() as db:
        key = await db.get(ApiKey, state.key_id)
        actor = await db.get(User, state.actor_id)
        assert actor.role == "employee" and key.scopes == ["annotations:read"]
        assert (
            await db.scalar(
                select(StorageOperation.id).where(
                    StorageOperation.purpose == objects.purpose(state.output_id)
                )
            )
            is None
        )


async def test_read_only_key_can_retry_and_queues_exact_task_and_original(
    committed_pyramid, monkeypatch
):
    state, queued = committed_pyramid, []
    async with state.factory() as db:
        await db.execute(
            update(ImagePyramidAsset)
            .where(ImagePyramidAsset.id == state.asset_id)
            .values(active_generation=None)
        )
        await db.execute(
            update(ImagePyramidGeneration)
            .where(ImagePyramidGeneration.asset_id == state.asset_id)
            .values(status="failed", updated_at=pyramids.utcnow() - timedelta(hours=1))
        )
        await db.commit()
    monkeypatch.setattr(
        "app.api.v1.tasks.image_pyramid.enqueue_image_pyramid",
        lambda *args, **kwargs: queued.append((args, kwargs)) or "queued-fixture",
    )
    response = await state.client.post(state.route + "/retry", headers=state.headers)
    assert response.status_code == 202 and response.json()["status"] == "queued", (
        response.text
    )
    args, options = queued[0]
    assert args == ("task", state.task_id) and options["force"]
    ref = options["delegation"]
    assert ref["mode"] == "http" and ref["actor_id"] == str(state.actor_id)
    assert ref["api_key_id"] == str(state.key_id) and ref["task_id"] == str(
        state.task_id
    )
    assert ref["original"] == state.original
    assert "jwt" not in str(ref).lower() and state.token not in str(ref)


@pytest.mark.parametrize("operation", ["manifest", "asset_urls"])
@pytest.mark.parametrize("revocation", ["key_rotation", "membership"])
async def test_revocation_committed_after_physical_read_rejects_http_response(
    committed_pyramid,
    monkeypatch,
    operation,
    revocation,
):
    state = committed_pyramid
    physical_call, changed = originals.physical, False

    async def read_then_revoke(call, *args, **kwargs):
        nonlocal changed
        result = await physical_call(call, *args, **kwargs)
        if call is physical.verify_object and not changed:
            changed = True
            async with state.factory() as db:
                if revocation == "key_rotation":
                    # Same ID/scopes/expiry remains a different presented key.
                    await db.execute(
                        update(ApiKey)
                        .where(ApiKey.id == state.key_id)
                        .values(key_hash=hash_password("different-live-key"))
                    )
                else:
                    await db.execute(
                        delete(ProjectMember).where(
                            ProjectMember.project_id == state.project_id,
                            ProjectMember.user_id == state.actor_id,
                        )
                    )
                await db.commit()
        return result

    monkeypatch.setattr(originals, "physical", read_then_revoke)
    if operation == "manifest":
        response = await state.client.get(state.route, headers=state.headers)
    else:
        response = await state.client.post(
            state.route + "/asset-urls",
            headers=state.headers,
            json={"items": [{"kind": "overview", "generation": 1}]},
        )
    assert changed and state.transport.verified
    assert response.status_code == (403 if revocation == "key_rotation" else 404), (
        response.text
    )
    assert "overview" not in response.json() and "items" not in response.json()
    assert "storage.invalid" not in response.text
    async with state.factory() as db:
        assert (
            await db.scalar(
                select(StorageOperation.id).where(
                    StorageOperation.purpose == objects.purpose(state.output_id)
                )
            )
            is None
        )


async def test_key_missing_annotations_read_is_rejected_before_physical_io(
    committed_pyramid,
):
    state = committed_pyramid
    async with state.factory() as db:
        await db.execute(
            update(ApiKey)
            .where(ApiKey.id == state.key_id)
            .values(scopes=["tasks:read"])
        )
        await db.commit()
    response = await state.client.get(state.route, headers=state.headers)
    assert response.status_code == 403
    assert not state.transport.verified and not state.transport.signed


async def test_key_rotated_after_read_context_exit_is_rechecked_before_ready_response(
    committed_pyramid, monkeypatch
):
    from app.api.v1.tasks import image_pyramid as api

    state, changed = committed_pyramid, False
    building_generation = api._building_generation

    async def building_after_rotation(db, asset):
        nonlocal changed
        result = await building_generation(db, asset)
        assert state.transport.verified and state.transport.signed
        async with state.factory() as independent:
            # This specifically covers the gap after reading()'s successful
            # post-check and durable Permit settlement, before GET returns.
            assert (
                await independent.scalar(
                    select(StorageOperation.id).where(
                        StorageOperation.purpose == objects.purpose(state.output_id)
                    )
                )
                is None
            )
            await independent.execute(
                update(ApiKey)
                .where(ApiKey.id == state.key_id)
                .values(key_hash=hash_password("rotated-after-reading-exited"))
            )
            await independent.commit()
        changed = True
        return result

    monkeypatch.setattr(api, "_building_generation", building_after_rotation)
    response = await state.client.get(state.route, headers=state.headers)
    assert changed and response.status_code == 403, response.text
    assert "overview" not in response.json() and "storage.invalid" not in response.text


async def test_invalid_manifest_metadata_invalidates_ready_and_can_retry_after_cooldown(
    committed_pyramid, monkeypatch
):
    state, queued = committed_pyramid, []
    monkeypatch.setattr(
        "app.api.v1.tasks.image_pyramid.settings.image_pyramid_auto_generate", False
    )
    monkeypatch.setattr(
        "app.api.v1.tasks.image_pyramid.enqueue_image_pyramid",
        lambda *args, **kwargs: queued.append((args, kwargs)) or "metadata-retry",
    )
    async with state.factory() as db:
        generation = await db.get(ImagePyramidGeneration, (state.asset_id, 1))
        generation.overview_width += 1
        await db.commit()

    # Real validate_inventory rejects this metadata with ContentError, rather
    # than the physical transport's ImagePyramidStorageError. Authorization and
    # the independent output-owner checks remain entirely real.
    response = await state.client.get(state.route, headers=state.headers)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "inconsistent"
    assert response.json()["error_code"] == "image_pyramid_storage_invalid_receipt"
    assert response.json()["retryable"] is True
    assert not state.transport.signed
    async with state.factory() as db:
        generation = await db.get(ImagePyramidGeneration, (state.asset_id, 1))
        asset = await db.get(ImagePyramidAsset, state.asset_id)
        assert generation.status == "failed" and asset.active_generation is None
        assert generation.error_code == "image_pyramid_storage_invalid_receipt"
        assert (
            await db.scalar(
                select(StorageOperation.id).where(
                    StorageOperation.purpose == objects.purpose(state.output_id)
                )
            )
            is None
        )

    cooling = await state.client.post(state.route + "/retry", headers=state.headers)
    assert cooling.status_code == 429 and not queued
    async with state.factory() as db:
        await db.execute(
            update(ImagePyramidGeneration)
            .where(
                ImagePyramidGeneration.asset_id == state.asset_id,
                ImagePyramidGeneration.generation == 1,
            )
            .values(updated_at=pyramids.utcnow() - timedelta(hours=1))
        )
        await db.commit()
    retried = await state.client.post(state.route + "/retry", headers=state.headers)
    assert retried.status_code == 202 and retried.json()["status"] == "queued", (
        retried.text
    )
    args, options = queued[0]
    assert args == ("task", state.task_id)
    assert options["delegation"]["original"] == state.original
