"""Real transactions fence late changes without holding locks during decoding."""

import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from fastapi import HTTPException
import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.requests import Request

from app.api.v1 import training_datasets as api
from app.core.security import create_access_token
from app.db.models.annotation import Annotation
from app.db.models.async_job import AsyncJob
from app.db.models.dataset import DatasetItem
from app.db.models.dataset_asset import DatasetAsset, DatasetAssetLocation
from app.db.models.project import Project
from app.db.models.storage_maintenance import StorageMaintenanceGate
from app.db.models.task import Task
from app.db.models.training_dataset import TrainingDatasetVersion
from app.db.models.user import User
from app.schemas.training_dataset import TrainingDatasetCreate, TrainingDatasetSelection
from app.services import training_dataset_originals as originals
from app.services import training_datasets as service
from app.services.content_snapshot import ContentError
from app.services.training_dataset_bridge import TrainingDatasetError
from tests.factory import create_user
from tests.sample_evaluation_original_fixtures import Objects
from tests.storage_migration_isolation import (
    db_session as db_session,
    httpx_client as httpx_client,
    migration_engine as migration_engine,
    super_admin as super_admin,
)
from tests.test_training_datasets import reviewed_project
from tests.test_training_datasets import source_headers as source_headers


@pytest.fixture
async def committed_training(migration_engine, monkeypatch):
    """Committed facts belong to this test's schema, never to shared data."""
    maker = async_sessionmaker(migration_engine, expire_on_commit=False)
    objects = Objects().install(monkeypatch)
    async with maker() as db:
        owner = await create_user(
            db, "project_admin", f"training-{uuid.uuid4().hex}@example.test", "Owner"
        )
        other = await create_user(
            db, "employee", f"training-{uuid.uuid4().hex}@example.test", "Other"
        )
        project, tasks = await reviewed_project(db, owner, original_objects=objects)
        await db.commit()
    world = SimpleNamespace(
        maker=maker,
        owner=owner,
        other=other,
        project=project,
        tasks=tasks,
        objects=objects,
    )
    try:
        yield world
    finally:
        assert all(body.closed for body in objects.bodies)


@pytest.mark.parametrize("phase", ["decode", "close"])
@pytest.mark.parametrize(
    "change", ["actor", "owner", "task", "dimensions", "asset", "replica", "gate"]
)
async def test_late_changes_cannot_finish_preparation_and_do_not_wait_for_media_locks(
    committed_training, monkeypatch, phase, change
):
    world = committed_training
    entered, release = threading.Event(), threading.Event()
    reached, resume = asyncio.Event(), asyncio.Event()
    captured = {}
    inspect = originals.inspect_image
    close = originals.close_stage

    def inspect_image(stage, sample):
        captured["task_id"] = uuid.UUID(sample["task_id"])
        if phase == "decode" and not entered.is_set():
            entered.set()
            if not release.wait(10):
                raise TimeoutError("test did not release decoder")
        return inspect(stage, sample)

    async def close_stage(stage):
        await close(stage)
        if phase == "close" and not reached.is_set():
            reached.set()
            await resume.wait()

    monkeypatch.setattr(originals, "inspect_image", inspect_image)
    monkeypatch.setattr(originals, "close_stage", close_stage)
    async with world.maker() as db:
        pending = asyncio.create_task(
            service.prepare_selection(
                db,
                world.project,
                TrainingDatasetSelection(),
                actor_id=world.owner.id,
                trusted_actor=True,
            )
        )
        try:
            if phase == "decode":
                if not await asyncio.to_thread(entered.wait, 5):
                    result = await pending
                    pytest.fail(f"No image decoding: {result.report}")
            else:
                staged = asyncio.ensure_future(reached.wait())
                try:
                    done, _ = await asyncio.wait(
                        {staged, pending},
                        timeout=5,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    staged.cancel()
                if staged not in done:
                    if pending in done:
                        result = await pending
                        pytest.fail(f"No staged media to close: {result.report}")
                    pytest.fail("No staged media close within 5s")
            async with world.maker() as editor:
                task = await editor.get(Task, captured["task_id"])
                item = await editor.get(DatasetItem, task.dataset_item_id)
                asset = await editor.scalar(
                    select(DatasetAsset).where(DatasetAsset.dataset_item_id == item.id)
                )
                if change == "actor":
                    statement = (
                        update(User)
                        .where(User.id == world.owner.id)
                        .values(is_active=False)
                    )
                elif change == "owner":
                    statement = (
                        update(Project)
                        .where(Project.id == world.project.id)
                        .values(owner_id=world.other.id)
                    )
                elif change == "task":
                    statement = (
                        update(Task)
                        .where(Task.id == task.id)
                        .values(file_path="changed/path.png")
                    )
                elif change == "dimensions":
                    statement = (
                        update(DatasetItem)
                        .where(DatasetItem.id == item.id)
                        .values(width=item.width + 1)
                    )
                elif change == "asset":
                    statement = delete(DatasetAsset).where(DatasetAsset.id == asset.id)
                elif change == "replica":
                    statement = (
                        update(DatasetAssetLocation)
                        .where(DatasetAssetLocation.asset_id == asset.id)
                        .values(bucket="other-originals")
                    )
                else:
                    statement = (
                        update(StorageMaintenanceGate)
                        .where(
                            StorageMaintenanceGate.location_id
                            == originals.original_sources.LEGACY_LOCATION_ID
                        )
                        .values(revision=StorageMaintenanceGate.revision + 1)
                    )
                # A business/source lock held across physical work would block here.
                await asyncio.wait_for(editor.execute(statement), 2)
                await editor.commit()
            release.set()
            resume.set()
            with pytest.raises((ContentError, HTTPException, TrainingDatasetError)):
                await pending
        finally:
            release.set()
            resume.set()
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)


async def test_finalization_rechecks_labels_without_reopening_media(committed_training):
    world = committed_training
    async with world.maker() as db:
        prepared = await service.prepare_selection(
            db,
            world.project,
            TrainingDatasetSelection(),
            actor_id=world.owner.id,
            trusted_actor=True,
        )
        assert prepared.report["ready"]
        reads = list(world.objects.calls)
        async with world.maker() as editor:
            annotation = await editor.scalar(
                select(Annotation).where(Annotation.project_id == world.project.id)
            )
            annotation.geometry = {
                "type": "bbox",
                "x": 0.2,
                "y": 0.2,
                "w": 0.1,
                "h": 0.1,
            }
            annotation.version += 1
            await editor.commit()
        with pytest.raises(TrainingDatasetError) as failure:
            await service.finalize_selection(db, prepared)
        assert failure.value.code == "preflight_changed"
        assert world.objects.calls == reads


async def test_preparation_and_finalization_never_commit_the_callers_session(
    db_session, super_admin, monkeypatch
):
    owner, _ = super_admin
    objects = Objects().install(monkeypatch)
    project, _tasks = await reviewed_project(
        db_session, owner, original_objects=objects
    )
    commit = AsyncMock(wraps=db_session.commit)
    monkeypatch.setattr(db_session, "commit", commit)
    prepared = await service.prepare_selection(
        db_session,
        project,
        TrainingDatasetSelection(),
        actor_id=owner.id,
        trusted_actor=True,
    )
    assert prepared.report["ready"]
    snapshot, _ = await service.finalize_selection(db_session, prepared)
    commit.assert_not_awaited()
    serialized = json.dumps(snapshot)
    assert "saved-training-originals" not in serialized
    assert all(
        "source_bucket" not in sample and "original" in sample
        for sample in snapshot["samples"]
    )
    assert {bucket for kind, bucket, _key in objects.calls if kind == "get"} == {
        "saved-training-originals"
    }


@pytest.mark.parametrize("route", ["preflight", "create"])
async def test_disconnected_route_drains_decoder_without_creating_or_reading_next_image(
    committed_training, monkeypatch, route
):
    world = committed_training
    entered, release = threading.Event(), threading.Event()
    disconnected = asyncio.Event()
    inspect = originals.inspect_image
    async with world.maker() as db:
        prepared = await service.prepare_selection(
            db,
            world.project,
            TrainingDatasetSelection(),
            actor_id=world.owner.id,
            trusted_actor=True,
        )
        assert prepared.report["ready"]
        world.objects.calls.clear()

        def inspect_image(stage, sample):
            entered.set()
            if not release.wait(10):
                raise TimeoutError("test did not release decoder")
            return inspect(stage, sample)

        async def receive():
            await disconnected.wait()
            return {"type": "http.disconnect"}

        token = create_access_token(subject=str(world.owner.id), role=world.owner.role)
        request = Request(
            {
                "type": "http",
                "headers": [(b"authorization", f"Bearer {token}".encode())],
            },
            receive,
        )
        monkeypatch.setattr(originals, "inspect_image", inspect_image)
        call = api.preflight if route == "preflight" else api.create_version
        body = (
            TrainingDatasetSelection()
            if route == "preflight"
            else TrainingDatasetCreate(
                name="Disconnected",
                request_id=uuid.uuid4(),
                preflight_digest=prepared.report["digest"],
            )
        )
        pending = asyncio.create_task(
            call(
                body=body,
                request=request,
                project=world.project,
                user=world.owner,
                db=db,
            )
        )
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            disconnected.set()
            await asyncio.sleep(0.02)
            assert not pending.done()
            release.set()
            with pytest.raises(HTTPException) as failure:
                await pending
            assert failure.value.status_code == 499
            gets = [
                (bucket, key)
                for kind, bucket, key in world.objects.calls
                if kind == "get"
            ]
            # One impossible-condition probe and one full GET for the same image.
            assert len(gets) == 2 and len(set(gets)) == 1
            assert not await db.scalar(
                select(TrainingDatasetVersion.id).where(
                    TrainingDatasetVersion.project_id == world.project.id
                )
            )
            assert not await db.scalar(
                select(AsyncJob.id).where(AsyncJob.project_id == world.project.id)
            )
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)


async def test_exact_read_scopes_create_http_version_and_same_id_key_rotation_revokes_it(
    db_session, super_admin, httpx_client, source_headers
):
    from app.services.api_key_service import create_key

    owner, _ = super_admin
    project, _tasks = await reviewed_project(
        db_session, owner, original_objects=source_headers
    )
    key, token = await create_key(
        db_session,
        owner,
        "Training read contract",
        ["datasets:read", "annotations:read"],
    )
    endpoint = f"/api/v1/projects/{project.id}/training-datasets"
    headers = {"Authorization": f"Bearer {token}"}
    preflight = await httpx_client.post(
        endpoint + ":preflight", json={}, headers=headers
    )
    assert preflight.status_code == 200 and preflight.json()["ready"], preflight.text
    response = await httpx_client.post(
        endpoint,
        headers=headers,
        json={
            "name": "Scoped original",
            "request_id": str(uuid.uuid4()),
            "preflight_digest": preflight.json()["digest"],
        },
    )
    assert response.status_code == 202, response.text
    assert "api_key_fingerprint" not in response.text
    version = await db_session.get(
        TrainingDatasetVersion, uuid.UUID(response.json()["id"])
    )
    assert version.snapshot["authorization"]["api_key_id"] == str(key.id)
    io_before = list(source_headers.calls)
    key.key_hash = "same-id-new-secret"
    await db_session.flush()
    with pytest.raises(TrainingDatasetError) as failure:
        await service.assert_dataset_authority(db_session, version)
    assert failure.value.code == "api_key_revoked"
    assert source_headers.calls == io_before
