"""Real ownership/authority rows wrap the strict provider and download capture."""

import asyncio
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from fastapi import HTTPException
import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.requests import Request

from app.config import settings
from app.core import token_blacklist
from app.core.security import create_access_token, decode_access_token, hash_password
from app.db.models.api_key import ApiKey
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.dataset_asset import DatasetAsset, DatasetAssetLocation
from app.db.models.project_member import ProjectMember
from app.db.models.storage_maintenance import (
    StorageMaintenanceGate as Gate,
    StorageOperation,
)
from app.db.models.task import Task
from app.db.models.user import User
from app.services import comment_attachment_authority as credentials
from app.services import (
    managed_original_creation as creation,
    managed_original_reader as reader,
)
from app.services import (
    original_binary as service,
    original_binary_response as protocol,
)
from app.services import storage_access, task_originals
from app.services.content_snapshot import ContentError
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.reference_keys import reference_key
from app.services.storage import storage_service
from tests.factory import create_project
from tests.test_managed_original_reader import Provider
from tests.test_original_sources import add_link
from tests.test_task_originals import (
    original_client as original_client,
    original_db as original_db,
)

RAW = b"opaque original\x00\xff"


@pytest.fixture
async def world(original_db, original_client, test_engine, monkeypatch):
    db = original_db
    # Extend the existing per-test owner schema with real authority tables.
    # LIKE preserves production columns/checks without introducing business FKs
    # into the independently disposable ledger fixture.
    async with test_engine.connect() as connection:
        template = await connection.scalar(text("SELECT current_schema()"))
    schema = await db.scalar(text("SELECT current_schema()"))
    for table in (
        "users",
        "api_keys",
        "projects",
        "project_members",
        "task_batches",
        "datasets",
    ):
        await db.execute(
            text(
                f'CREATE TABLE "{schema}"."{table}" (LIKE "{template}"."{table}" INCLUDING ALL)'
            )
        )
    users = [
        User(
            id=uuid.uuid4(),
            email=uuid.uuid4().hex + "@example.test",
            name="Binary",
            password_hash="unused",
            role=role,
            is_active=True,
        )
        for role in ("project_admin", "employee", "employee")
    ]
    db.add_all(users)
    await db.flush()
    manager, actor, stranger = users
    project = await create_project(db, owner_id=manager.id)
    dataset = Dataset(
        id=uuid.uuid4(),
        display_id="bin-" + uuid.uuid4().hex[:12],
        name="Binary originals",
        created_by=manager.id,
    )
    member = ProjectMember(project_id=project.id, user_id=actor.id, role="annotator")
    db.add_all([dataset, member])
    await db.commit()
    provider = Provider()
    provider.meta.endpoint_url = (
        f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}"
    )
    monkeypatch.setattr(reader, "create_client", lambda **_: provider)
    monkeypatch.setattr(
        token_blacklist, "is_blacklisted", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(
        token_blacklist, "get_user_generation", AsyncMock(return_value=0)
    )
    value = SimpleNamespace(
        db=db,
        client=original_client,
        provider=provider,
        manager=manager,
        actor=actor,
        stranger=stranger,
        project=project,
        dataset=dataset,
        member=member,
        stages=[],
    )
    capture = protocol.capture_download

    async def recorded(spec):
        stage = await capture(spec)
        value.stages.append(stage)
        return stage

    monkeypatch.setattr(protocol, "capture_download", recorded)
    try:
        yield value
    finally:
        for stage in value.stages:
            stage.close()
        await db.rollback()
        assert all(body.closed for body in provider.bodies)
        # Only this schema is dropped by original_db; immutable receipts never
        # need a production deletion exception for test teardown.


def request(actor, *, token=None, key_id=None, scopes=None):
    token = token or create_access_token(
        str(actor.id), actor.role, timedelta(minutes=10)
    )
    value = Request(
        {"type": "http", "headers": [(b"authorization", ("Bearer " + token).encode())]}
    )
    value.state.api_key_id = str(key_id) if key_id is not None else None
    value.state.api_key_scopes = list(scopes) if scopes is not None else None
    return value


async def managed(world, *, data=RAW, file_type="other", primary_only=False):
    item = DatasetItem(
        id=uuid.uuid4(),
        dataset_id=world.dataset.id,
        file_name="original.bin",
        file_path="binary/" + uuid.uuid4().hex,
        file_type=file_type,
    )
    world.db.add(item)
    await world.db.flush()
    asset = DatasetAsset(
        id=uuid.uuid4(),
        dataset_item_id=item.id,
        media_type=file_type,
        size_bytes=len(data),
        verification_state="verified",
        checksum_algorithm="sha256",
        checksum_value=hashlib.sha256(data).hexdigest(),
    )
    world.db.add(asset)
    await world.db.flush()
    world.db.add(
        DatasetAssetLocation(
            asset_id=asset.id,
            location_id=LEGACY_LOCATION_ID,
            bucket="saved-binary-originals",
            object_key=item.file_path,
            role="managed_original",
            reference_state="present",
        )
    )
    task = Task(
        id=uuid.uuid4(),
        project_id=world.project.id,
        display_id="bin-" + uuid.uuid4().hex[:20],
        file_name=item.file_name,
        file_path=item.file_path,
        file_type=file_type,
        dataset_item_id=None if primary_only else item.id,
        assignee_id=world.actor.id,
        status="in_progress",
    )
    world.db.add(task)
    await world.db.flush()
    if primary_only:
        await add_link(world.db, task.id, item.id)
    await world.db.commit()
    world.provider.raw = world.provider.actual = data
    return task, item, asset


async def standalone(world, data, file_type):
    task = Task(
        id=uuid.uuid4(),
        project_id=world.project.id,
        display_id="bin-" + uuid.uuid4().hex[:20],
        file_name="original.bin",
        file_path="not-allocated",
        file_type=file_type,
        status="uploading",
        assignee_id=world.actor.id,
    )
    world.db.add(task)
    await world.db.flush()
    allocation = await creation.allocate_task(
        world.db,
        project_id=world.project.id,
        task_id=task.id,
        logical_path=task.file_name,
        max_bytes=1024,
        requested_by=world.manager.id,
        authority={},
        content_type="application/octet-stream",
    )
    task.file_path = allocation["object_key"]
    await world.db.commit()
    world.client.store(allocation["staging_bucket"], allocation["staging_key"], data)
    created = await creation.complete_direct(world.db, allocation)
    await task_originals.publish(world.db, task, allocation, created)
    task.status = "pending"
    await world.db.commit()
    world.provider.raw = world.provider.actual = data
    return task


async def read(world, resource, actor=None, *, presented=None, **kwargs):
    actor = actor or world.actor
    return await service.snapshot(
        world.db,
        resource,
        actor,
        expected_content_id=kwargs.pop("content_id"),
        expected_sha256=kwargs.pop("sha256"),
        request=presented or request(actor),
        **kwargs,
    )


async def operations(world):
    return await world.db.scalar(select(func.count()).select_from(StorageOperation))


@pytest.mark.parametrize("change", ["preferences", "disabled", "timeout"])
async def test_original_recheck_waits_for_account_save_and_keeps_current_authority(
    world, monkeypatch, change
):
    task, _, _ = await managed(world)
    context = await service.authorize_original(
        world.db,
        service.Resource(kind="task", task_id=task.id),
        world.actor,
        request=request(world.actor),
    )
    actor_id = context.proof.actor_id
    if change == "timeout":
        monkeypatch.setattr(credentials, "AUTHORITY_LOCK_TIMEOUT", "500ms")
    async with async_sessionmaker(world.db.bind, expire_on_commit=False)() as holder:
        account = await holder.scalar(
            select(User).where(User.id == actor_id).with_for_update()
        )
        if change == "disabled":
            account.is_active = False
        else:
            account.preferences = {"workbench": {"save_in_progress": True}}
        await holder.flush()
        holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
        pending = asyncio.create_task(service.recheck_original(world.db, context))
        try:
            async with asyncio.timeout(2):
                async with world.db.bind.connect() as observer:
                    while not await observer.scalar(
                        text(
                            "SELECT count(*) FROM pg_stat_activity "
                            "WHERE datname=current_database() "
                            "AND :holder=ANY(pg_blocking_pids(pid))"
                        ),
                        {"holder": holder_pid},
                    ):
                        if pending.done():
                            await pending
                        await asyncio.sleep(0.01)
            assert world.provider.calls == []
            if change != "timeout":
                await holder.commit()
            if change in {"disabled", "timeout"}:
                with pytest.raises(HTTPException) as error:
                    await pending
                assert error.value.status_code == (403 if change == "disabled" else 409)
                assert error.value.detail["code"] == (
                    "content_authority_revoked"
                    if change == "disabled"
                    else "content_busy"
                )
                assert world.provider.calls == []
            else:
                await pending
                image = await read(
                    world,
                    context.resource,
                    content_id=context.source.content_id,
                    sha256=context.source.sha256,
                )
                try:
                    assert image.file.read() == RAW
                finally:
                    image.close()
        finally:
            await holder.rollback()
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("owner", ["task", "dataset"])
@pytest.mark.parametrize(
    "data,file_type",
    [
        (b"", "other"),
        (b"GIF89a raw unchanged", "image"),
        (b'<svg xmlns="http://www.w3.org/2000/svg"/>', "image"),
        (b"video\x00opaque-container", "video"),
    ],
)
async def test_generic_bytes_and_empty_do_not_enter_pillow(
    world, monkeypatch, owner, data, file_type
):
    if owner == "task":
        task = await standalone(world, data, file_type)
        content_id = task.original_id
    else:
        task, _, asset = await managed(world, data=data, file_type=file_type)
        content_id = asset.id
    resource = service.Resource(kind="task", task_id=task.id)
    monkeypatch.setattr(storage_service, "bucket", "wrong-current-bucket")
    monkeypatch.setattr(storage_service, "datasets_bucket", "wrong-current-datasets")
    identity = await service.public_identity(
        world.db, resource, world.actor, request(world.actor)
    )
    assert identity["owner_kind"] == (
        "task_original" if owner == "task" else "dataset_asset"
    )
    assert identity["content_id"] == content_id and identity["media_type"] == file_type
    assert identity["original_availability"] == "unchecked"
    assert world.provider.calls == []
    image = await read(
        world, resource, content_id=content_id, sha256=identity["sha256"]
    )
    try:
        assert image.file.read() == data and image.size == len(data)
        assert image.sha256 == hashlib.sha256(data).hexdigest()
        expected_bucket = identity["owner_kind"] == "task_original"
        assert all(
            params["Bucket"] != "wrong-current-bucket"
            and params["Bucket"] != "wrong-current-datasets"
            for action, params in world.provider.calls
            if action in {"get", "head"}
        )
        assert image.observation["bucket"] == (
            settings.minio_bucket if expected_bucket else "saved-binary-originals"
        )
    finally:
        image.close()
    assert await operations(world) == 0


async def test_real_request_and_current_scope_are_required_even_when_key_id_omitted(
    world,
):
    task, _, asset = await managed(world)
    resource = service.Resource(kind="task", task_id=task.id)
    with pytest.raises(HTTPException):
        await service.public_identity(world.db, resource, world.actor, None)
    token, key_id = "ak_" + uuid.uuid4().hex, uuid.uuid4()
    world.db.add(
        ApiKey(
            id=key_id,
            user_id=world.actor.id,
            name="No task read",
            key_prefix=token[:12],
            key_hash=hash_password(token),
            scopes=["datasets:read"],
        )
    )
    await world.db.commit()
    presented = request(
        world.actor, token=token, key_id=key_id, scopes=["datasets:read"]
    )
    with pytest.raises(HTTPException) as error:
        await read(
            world,
            resource,
            presented=presented,
            content_id=asset.id,
            sha256=asset.checksum_value,
        )
    assert error.value.status_code == 403 and world.provider.calls == []


async def test_reference_and_wrong_dataset_path_never_start_object_io(world):
    task, item, asset = await managed(world, file_type="image")
    item_id, task_id, content_id, sha = item.id, task.id, asset.id, asset.checksum_value
    with pytest.raises(HTTPException) as error:
        await service.public_identity(
            world.db,
            service.Resource(kind="item", dataset_id=uuid.uuid4(), item_id=item_id),
            world.manager,
            request(world.manager),
        )
    assert error.value.status_code == 404
    item.file_path = task.file_path = reference_key(item_id, sha)
    await world.db.commit()
    with pytest.raises(ContentError, match="reference_requires_verified_content"):
        await read(
            world,
            service.Resource(kind="task", task_id=task_id),
            content_id=content_id,
            sha256=sha,
        )
    assert world.provider.calls == []


@pytest.mark.parametrize("when", ["admission", "after_bytes"])
@pytest.mark.parametrize("mutation", ["membership", "generation", "gate"])
async def test_current_authority_and_gate_are_checked_at_real_wait_boundaries(
    world, monkeypatch, when, mutation
):
    task, _, asset = await managed(world)
    resource = service.Resource(kind="task", task_id=task.id)
    content_id, sha = asset.id, asset.checksum_value
    changed = []

    async def change():
        changed.append(True)
        if mutation == "membership":
            await world.db.execute(
                delete(ProjectMember).where(ProjectMember.id == world.member.id)
            )
        elif mutation == "generation":
            monkeypatch.setattr(
                token_blacklist, "get_user_generation", AsyncMock(return_value=99)
            )
        else:
            gate = await world.db.get(Gate, LEGACY_LOCATION_ID, populate_existing=True)
            assert gate is not None
            gate.revision += 1
        await world.db.commit()

    if when == "admission":
        acquire = storage_access.acquire

        async def admitted(*args, **kwargs):
            permit = await acquire(*args, **kwargs)
            await change()
            return permit

        monkeypatch.setattr(storage_access, "acquire", admitted)
    else:
        capture = protocol.capture_download

        async def captured(spec):
            stage = await capture(spec)
            await change()
            return stage

        monkeypatch.setattr(protocol, "capture_download", captured)
    with pytest.raises((HTTPException, ContentError)):
        await read(world, resource, content_id=content_id, sha256=sha)
    assert changed == [True]
    assert bool(world.provider.calls) == (when == "after_bytes")
    assert all(stage._closed and stage.file.closed for stage in world.stages)
    assert await operations(world) == 0


@pytest.mark.parametrize("when", ["admission_commit", "final_commit"])
async def test_natural_jwt_expiry_after_commit_closes_or_prevents_download(
    world, monkeypatch, when
):
    task, _, asset = await managed(world)
    resource = service.Resource(kind="task", task_id=task.id)
    token = create_access_token(str(world.actor.id), "employee", timedelta(minutes=10))
    expiration = datetime.fromtimestamp(decode_access_token(token)["exp"], timezone.utc)
    commit = world.db.commit
    commits = []

    async def expires_after_commit():
        await commit()
        commits.append(True)
        if len(commits) == (2 if when == "admission_commit" else 3):
            monkeypatch.setattr(
                credentials, "now", lambda: expiration + timedelta(seconds=1)
            )

    monkeypatch.setattr(world.db, "commit", expires_after_commit)
    with pytest.raises(HTTPException) as error:
        await read(
            world,
            resource,
            presented=request(world.actor, token=token),
            content_id=asset.id,
            sha256=asset.checksum_value,
        )
    assert error.value.status_code == 403
    assert bool(world.provider.calls) == (when == "final_commit")
    assert all(stage.file.closed for stage in world.stages)
    assert await operations(world) == 0


def test_resource_is_frozen_and_rejects_mixed_or_missing_scope():
    value = service.Resource(kind="task", task_id=uuid.uuid4())
    with pytest.raises(FrozenInstanceError):
        value.task_id = uuid.uuid4()
    with pytest.raises(ContentError):
        service.Resource(kind="camera", task_id=uuid.uuid4())
    with pytest.raises(ContentError):
        service.Resource(
            kind="item",
            dataset_id=uuid.uuid4(),
            item_id=uuid.uuid4(),
            task_id=uuid.uuid4(),
        )


@pytest.mark.parametrize(
    "when,change",
    [
        ("admission", "revoked"),
        ("after_bytes", "rotated"),
        ("after_bytes", "scopes"),
        ("after_bytes", "expiry_changed"),
    ],
)
async def test_original_key_receipt_cannot_change_during_download(
    world, monkeypatch, when, change
):
    task, _, asset = await managed(world)
    resource = service.Resource(kind="task", task_id=task.id)
    content_id, sha = asset.id, asset.checksum_value
    key_id, token = uuid.uuid4(), "ak_" + uuid.uuid4().hex
    key = ApiKey(
        id=key_id,
        user_id=world.actor.id,
        name="Binary key",
        key_prefix=token[:12],
        key_hash=hash_password(token),
        scopes=["tasks:read"],
    )
    world.db.add(key)
    await world.db.commit()
    presented = request(world.actor, token=token, key_id=key_id, scopes=["tasks:read"])

    async def mutate():
        current = await world.db.get(ApiKey, key_id, populate_existing=True)
        if change == "revoked":
            current.revoked_at = datetime.now(timezone.utc)
        elif change == "rotated":
            current.key_hash = hash_password("another original key")
        elif change == "scopes":
            current.scopes = ["tasks:read", "datasets:read"]
        else:
            current.expires_at = datetime(2100, 1, 1, tzinfo=timezone.utc)
        await world.db.commit()

    if when == "admission":
        acquire = storage_access.acquire

        async def admitted(*args, **kwargs):
            permit = await acquire(*args, **kwargs)
            await mutate()
            return permit

        monkeypatch.setattr(storage_access, "acquire", admitted)
    else:
        capture = protocol.capture_download

        async def captured(spec):
            stage = await capture(spec)
            await mutate()
            return stage

        monkeypatch.setattr(protocol, "capture_download", captured)
    with pytest.raises(HTTPException) as error:
        await read(
            world,
            resource,
            presented=presented,
            api_key_id=key_id,
            content_id=content_id,
            sha256=sha,
        )
    assert error.value.status_code == 403
    assert bool(world.provider.calls) == (when == "after_bytes")
    assert all(stage.file.closed for stage in world.stages)
    assert await operations(world) == 0


async def test_unknown_physical_close_keeps_independent_operation(world):
    task, _, asset = await managed(world)
    world.provider.body_close_error = OSError("synthetic private close detail")
    with pytest.raises(ContentError) as error:
        await read(
            world,
            service.Resource(kind="task", task_id=task.id),
            content_id=asset.id,
            sha256=asset.checksum_value,
        )
    assert getattr(error.value, "storage_operation_unsettled", False)
    assert await operations(world) == 1
    assert world.provider.calls and all(body.closed for body in world.provider.bodies)
    # The schema fixture is the sole owner of this synthetic unresolved permit;
    # teardown destroys its isolated schema, never deletes production evidence.
