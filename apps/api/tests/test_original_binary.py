"""Real ownership/authority rows wrap the strict provider and download capture."""

import asyncio
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
import hashlib
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from fastapi import HTTPException, Response
import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.requests import Request
from yoao_identity.contract import Principal

from app.api.v1 import originals as originals_api
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
from app.middleware.request_id import request_id_var
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
from app.services.dataset_assets import LEGACY_LOCATION_ID, ensure_legacy_bindings
from app.services.reference_keys import reference_key
from app.services.storage import storage_service
from tests.factory import create_project
from tests.managed_image_inference_fixtures import managed_database
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


def _principal(actor, *, token, key_id=None, scopes=None):
    name = getattr(actor, "name", "Binary")
    email = getattr(actor, "email", "binary@example.test")
    if key_id is not None:
        return Principal(
            actor_id=actor.id,
            name=name,
            email=email,
            role=actor.role,
            is_active=True,
            credential_type="api_key",
            credential_id=(
                key_id if isinstance(key_id, uuid.UUID) else uuid.UUID(str(key_id))
            ),
            generation=None,
            scopes=list(scopes or ()),
            expires_at=None,
        )
    claims = decode_access_token(token)
    jti = claims.get("jti")
    return Principal(
        actor_id=actor.id,
        name=name,
        email=email,
        role=actor.role,
        is_active=True,
        credential_type="jwt",
        credential_id=uuid.UUID(jti) if jti else uuid.uuid4(),
        generation=claims.get("gen", 0),
        scopes=None,
        expires_at=datetime.fromtimestamp(claims["exp"], timezone.utc),
    )


def request(actor, *, token=None, key_id=None, scopes=None):
    token = token or create_access_token(
        str(actor.id), actor.role, timedelta(minutes=10)
    )
    value = Request(
        {"type": "http", "headers": [(b"authorization", ("Bearer " + token).encode())]}
    )
    value.state.identity_principal = _principal(
        actor, token=token, key_id=key_id, scopes=scopes
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


@pytest.mark.parametrize("change", ["disabled", "timeout"])
async def test_original_recheck_keeps_current_authority(world, monkeypatch, change):
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
            account.name = "Concurrent account update"
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
            with pytest.raises(HTTPException) as error:
                await pending
            assert error.value.status_code == (403 if change == "disabled" else 409)
            assert error.value.detail["code"] == (
                "content_authority_revoked" if change == "disabled" else "content_busy"
            )
            assert world.provider.calls == []
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
    from app.api.v1.tasks._shared import _task_with_url

    task_dto = _task_with_url(task)
    assert task_dto.file_url is None
    assert task_dto.original_identity_path == (
        f"/api/v1/files/tasks/{task.id}/original-identity"
    )
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


async def test_primary_lidar_task_dto_uses_canonical_original_identity(world):
    from app.api.v1.tasks._shared import _task_with_url

    task, _item, asset = await managed(
        world, data=b"point cloud bytes", file_type="point_cloud", primary_only=True
    )
    dto = _task_with_url(task)
    assert dto.file_url is None
    assert dto.original_identity_path == (
        f"/api/v1/files/tasks/{task.id}/original-identity"
    )
    identity = await service.public_identity(
        world.db,
        service.Resource(kind="task", task_id=task.id),
        world.actor,
        request(world.actor),
    )
    assert identity["owner_kind"] == "dataset_asset"
    assert identity["content_id"] == asset.id


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


def _route_request(actor, *, path: str, route_template: str):
    token = create_access_token(str(actor.id), actor.role, timedelta(minutes=10))

    async def receive():
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    req = Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [(b"authorization", ("Bearer " + token).encode())],
            "route": SimpleNamespace(path=route_template, path_format=route_template),
        },
        receive=receive,
    )
    req.state.identity_principal = _principal(actor, token=token)
    req.state.api_key_id = None
    req.state.api_key_scopes = None
    return req, token


async def _wait_for_blocker(engine, holder_pid, pending):
    async with asyncio.timeout(2):
        async with engine.connect() as observer:
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


@pytest.fixture
async def native_world(test_engine, original_client, monkeypatch):
    async with managed_database(test_engine.url) as engine:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
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
            member = ProjectMember(
                project_id=project.id, user_id=actor.id, role="annotator"
            )
            db.add_all([dataset, member])
            await ensure_legacy_bindings(db)
            await db.commit()
            provider = Provider()
            provider.meta.endpoint_url = (
                f"{'https' if settings.minio_use_ssl else 'http'}://"
                f"{settings.minio_endpoint}"
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

            async def recorded(spec, **options):
                stage = await capture(spec, **options)
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


async def test_identity_waits_for_short_dataset_item_update_and_succeeds(
    native_world, caplog
):
    world = native_world
    task, item, asset = await managed(world, file_type="image")
    item_id, expected_content_id, expected_sha = (
        item.id,
        asset.id,
        asset.checksum_value,
    )
    resource = service.Resource(kind="task", task_id=task.id)
    req, _ = _route_request(
        world.actor,
        path=f"/api/v1/files/tasks/{task.id}/original-identity",
        route_template="/api/v1/files/tasks/{task_id}/original-identity",
    )
    caplog.set_level(logging.WARNING, logger="app.api.v1.originals")
    rid_token = request_id_var.set("req-short-item-update")
    try:
        async with async_sessionmaker(
            world.db.bind, expire_on_commit=False
        )() as holder:
            await holder.execute(
                update(DatasetItem).where(DatasetItem.id == item_id).values(width=640)
            )
            holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
            pending = asyncio.create_task(
                originals_api._identity(
                    resource, req, world.db, world.actor, Response()
                )
            )
            try:
                await _wait_for_blocker(world.db.bind, holder_pid, pending)
                await holder.commit()
                identity = await pending
            finally:
                await holder.rollback()
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        assert identity["content_id"] == expected_content_id
        assert identity["sha256"] == expected_sha
        assert caplog.records == []
    finally:
        request_id_var.reset(rid_token)


async def test_identity_long_dataset_item_lock_over_1s_returns_storage_original_busy_and_logs(
    native_world, caplog
):
    world = native_world
    task, item, _ = await managed(world, file_type="image")
    item_id, item_path = item.id, item.file_path
    resource = service.Resource(kind="task", task_id=task.id)
    req, bearer = _route_request(
        world.actor,
        path=f"/api/v1/files/tasks/{task.id}/original-identity",
        route_template="/api/v1/files/tasks/{task_id}/original-identity",
    )
    caplog.set_level(logging.WARNING, logger="app.api.v1.originals")
    rid_token = request_id_var.set("req-long-item-lock")
    try:
        async with async_sessionmaker(
            world.db.bind, expire_on_commit=False
        )() as holder:
            await holder.execute(
                update(DatasetItem).where(DatasetItem.id == item_id).values(width=800)
            )
            try:
                with pytest.raises(HTTPException) as error:
                    await originals_api._identity(
                        resource, req, world.db, world.actor, Response()
                    )
            finally:
                await holder.rollback()
        assert error.value.status_code == 409
        assert error.value.detail == {"code": "storage_original_busy"}
        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert record.levelname == "WARNING"
        assert record.route == "/api/v1/files/tasks/{task_id}/original-identity"
        assert record.request_id == "req-long-item-lock"
        assert record.status_code == 409
        assert record.code == "storage_original_busy"
        assert record.reason is None
        assert record.resource_kind == "task"
        assert record.task_id == str(task.id)
        message = record.getMessage()
        assert bearer not in message
        assert item_path not in message
        assert "saved-binary-originals" not in message
    finally:
        request_id_var.reset(rid_token)


async def test_identity_real_file_path_drift_after_wait_still_returns_content_binding_changed(
    native_world, caplog
):
    world = native_world
    task, item, _ = await managed(world, file_type="image")
    item_id = item.id
    drifted_path = "binary/drifted-" + uuid.uuid4().hex
    resource = service.Resource(kind="task", task_id=task.id)
    req, bearer = _route_request(
        world.actor,
        path=f"/api/v1/files/tasks/{task.id}/original-identity",
        route_template="/api/v1/files/tasks/{task_id}/original-identity",
    )
    caplog.set_level(logging.WARNING, logger="app.api.v1.originals")
    rid_token = request_id_var.set("req-drift-after-wait")
    try:
        async with async_sessionmaker(
            world.db.bind, expire_on_commit=False
        )() as holder:
            await holder.execute(
                update(DatasetItem)
                .where(DatasetItem.id == item_id)
                .values(file_path=drifted_path)
            )
            holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
            pending = asyncio.create_task(
                originals_api._identity(
                    resource, req, world.db, world.actor, Response()
                )
            )
            try:
                await _wait_for_blocker(world.db.bind, holder_pid, pending)
                await holder.commit()
                with pytest.raises(HTTPException) as error:
                    await pending
            finally:
                await holder.rollback()
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        assert error.value.status_code == 409
        assert error.value.detail == {"code": "content_binding_changed"}
        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert record.code == "content_binding_changed"
        assert record.request_id == "req-drift-after-wait"
        assert drifted_path not in record.getMessage()
        assert bearer not in record.getMessage()
    finally:
        request_id_var.reset(rid_token)


async def test_download_conflict_logs_expected_and_actual_digest_prefixes_and_skips_non_409(
    native_world, caplog
):
    world = native_world
    task, item, asset = await managed(world, file_type="image")
    wrong_content_id = uuid.uuid4()
    wrong_sha = "f" * 64
    resource = service.Resource(kind="task", task_id=task.id)
    req, bearer = _route_request(
        world.actor,
        path=f"/api/v1/files/tasks/{task.id}/original",
        route_template="/api/v1/files/tasks/{task_id}/original",
    )
    caplog.set_level(logging.WARNING, logger="app.api.v1.originals")
    rid_token = request_id_var.set("req-download-drift")
    try:
        with pytest.raises(HTTPException) as error:
            await originals_api._download(
                resource, req, world.db, world.actor, wrong_content_id, wrong_sha
            )
        assert error.value.status_code == 409
        assert error.value.detail == {"code": "content_binding_changed"}
        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert record.levelname == "WARNING"
        assert record.route == "/api/v1/files/tasks/{task_id}/original"
        assert record.request_id == "req-download-drift"
        assert record.status_code == 409
        assert record.code == "content_binding_changed"
        assert record.resource_kind == "task"
        assert record.task_id == str(task.id)
        assert record.item_id == str(item.id)
        assert record.expected_content_id == str(wrong_content_id)
        assert record.expected_sha256_prefix == wrong_sha[:12]
        assert record.actual_content_id == str(asset.id)
        assert record.actual_sha256_prefix == asset.checksum_value[:12]
        message = record.getMessage()
        assert bearer not in message
        assert item.file_path not in message
        assert asset.checksum_value not in message
        assert wrong_sha not in message

        caplog.clear()
        corrupted = b"X" * len(RAW)
        world.provider.actual = corrupted
        corrupted_sha = hashlib.sha256(corrupted).hexdigest()
        with pytest.raises(HTTPException) as mismatch:
            await originals_api._download(
                resource, req, world.db, world.actor, asset.id, asset.checksum_value
            )
        assert mismatch.value.status_code == 409
        assert mismatch.value.detail == {"code": "content_mismatch"}
        assert len(caplog.records) == 1
        mismatch_record = caplog.records[0]
        assert mismatch_record.code == "content_mismatch"
        assert mismatch_record.expected_content_id == str(asset.id)
        assert mismatch_record.expected_sha256_prefix == asset.checksum_value[:12]
        assert mismatch_record.actual_content_id == str(asset.id)
        assert mismatch_record.actual_sha256_prefix == corrupted_sha[:12]
        assert corrupted_sha not in mismatch_record.getMessage()
        assert bearer not in mismatch_record.getMessage()

        caplog.clear()
        stranger_req, _ = _route_request(
            world.stranger,
            path=f"/api/v1/files/tasks/{task.id}/original-identity",
            route_template="/api/v1/files/tasks/{task_id}/original-identity",
        )
        with pytest.raises(HTTPException) as denied:
            await originals_api._identity(
                resource, stranger_req, world.db, world.stranger, Response()
            )
        assert denied.value.status_code in {403, 404}
        assert caplog.records == []
    finally:
        request_id_var.reset(rid_token)


async def test_identity_logs_reason_field_on_409_without_credentials(
    monkeypatch, caplog
):
    task_id = uuid.uuid4()
    resource = service.Resource(kind="task", task_id=task_id)
    actor = SimpleNamespace(id=uuid.uuid4(), role="employee")
    req, bearer = _route_request(
        actor,
        path=f"/api/v1/files/tasks/{task_id}/original-identity",
        route_template="/api/v1/files/tasks/{task_id}/original-identity",
    )
    monkeypatch.setattr(
        originals_api.svc,
        "public_identity",
        AsyncMock(side_effect=HTTPException(409, {"reason": "project_busy"})),
    )
    caplog.set_level(logging.WARNING, logger="app.api.v1.originals")
    rid_token = request_id_var.set("req-reason-409")
    try:
        with pytest.raises(HTTPException) as error:
            await originals_api._identity(resource, req, None, actor, Response())
        assert error.value.status_code == 409
        assert error.value.detail == {"reason": "project_busy"}
        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert record.route == "/api/v1/files/tasks/{task_id}/original-identity"
        assert record.request_id == "req-reason-409"
        assert record.status_code == 409
        assert record.code is None
        assert record.reason == "project_busy"
        assert record.resource_kind == "task"
        assert record.task_id == str(task_id)
        assert bearer not in record.getMessage()
    finally:
        request_id_var.reset(rid_token)
