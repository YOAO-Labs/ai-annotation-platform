"""Original request authority fences real reference and ready-cache admissions."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
import uuid

from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
import pytest
from sqlalchemy import delete, func, select, update
from starlette.requests import Request

from app.core import token_blacklist
from app.core.security import create_access_token, decode_access_token, hash_password
from app.db.models.api_key import ApiKey
from app.db.models.original_cache import (
    OriginalCacheEntry as Entry,
    OriginalCacheReadLease as ReadLease,
)
from app.db.models.storage_maintenance import (
    StorageMaintenanceGate as Gate,
    StorageOperation,
)
from app.deps import get_current_principal, get_current_user
from app.services import comment_attachment_authority as credentials
from app.services import dataset_content as content, dataset_references as refs
from app.services import filesystem_storage, storage_access
from app.services.content_snapshot import ContentError
from app.services.identity_gateway import IdentityGateway
from tests.test_original_cache import MemoryStorage
from tests.test_reference_original_cache import (
    referenced_cache as referenced_cache,
    seed_cache,
)


@pytest.fixture
async def authority_world(referenced_cache, monkeypatch):
    world = referenced_cache
    monkeypatch.setattr(
        token_blacklist, "is_blacklisted", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(
        token_blacklist, "get_user_generation", AsyncMock(return_value=0)
    )
    try:
        yield world
    finally:
        async with world.factory() as db:
            remaining = await db.scalar(
                select(func.count())
                .select_from(StorageOperation)
                .where(StorageOperation.location_id == world.location_id)
            )
            await db.execute(
                delete(StorageOperation).where(
                    StorageOperation.location_id == world.location_id
                )
            )
            await db.execute(delete(Gate).where(Gate.location_id == world.location_id))
            await db.commit()
        assert remaining == 0, "reference request leaked a durable read permit"


async def prepared(world, path):
    cached_id = None
    if path == "cache":
        cached_id = (await seed_cache(world)).id
        (world.content[0] / world.content[7].relative_path).unlink()
    return cached_id


def observe_reads(monkeypatch):
    source_reads, cache_reads = [], []
    source = filesystem_storage.content_file
    stored = MemoryStorage.get_object

    def source_file(*args, **kwargs):
        source_reads.append(True)
        return source(*args, **kwargs)

    def cache_object(self, **kwargs):
        cache_reads.append(True)
        return stored(self, **kwargs)

    monkeypatch.setattr(filesystem_storage, "content_file", source_file)
    monkeypatch.setattr(MemoryStorage, "get_object", cache_object)
    return source_reads, cache_reads


async def call_task(world, token, *, key_id=None):
    async with world.factory() as db:
        request = Request(
            {
                "type": "http",
                "headers": [(b"authorization", ("Bearer " + token).encode())],
            }
        )
        identity = IdentityGateway()
        principal = await get_current_principal(
            request,
            HTTPAuthorizationCredentials(scheme="Bearer", credentials=token),
            db,
            identity,
        )
        actor = await get_current_user(request, principal, db, identity)
        return await content.task_snapshot(
            db,
            world.task.id,
            actor,
            world.sha,
            api_key_id=key_id,
            request=request,
        )


async def assert_no_read_responsibility(world, cached_id):
    async with world.factory() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(StorageOperation)
                .where(StorageOperation.location_id == world.location_id)
            )
            == 0
        )
        if cached_id is not None:
            entry = await db.get(Entry, cached_id, populate_existing=True)
            assert entry is not None and entry.state == "ready"
            assert entry.error_code is None
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(ReadLease)
                    .where(ReadLease.entry_id == cached_id)
                )
                == 0
            )


@pytest.mark.parametrize("path", ["source", "cache"])
@pytest.mark.parametrize("mutation", ["jwt_expiry", "key_revoke", "jwt_generation"])
async def test_reference_admission_rechecks_original_request_before_any_bytes(
    authority_world, monkeypatch, path, mutation
):
    world = authority_world
    cached_id = await prepared(world, path)
    source_reads, cache_reads = observe_reads(monkeypatch)
    token = create_access_token(
        str(world.actor.id), "employee", timedelta(minutes=10), gen=0
    )
    expiration = datetime.fromtimestamp(decode_access_token(token)["exp"], timezone.utc)
    key_id = None
    if mutation == "key_revoke":
        key_id, token = uuid.uuid4(), "ak_" + uuid.uuid4().hex
        async with world.factory() as db:
            db.add(
                ApiKey(
                    id=key_id,
                    user_id=world.actor.id,
                    name="Reference admission",
                    key_prefix=token[:12],
                    key_hash=hash_password(token),
                    scopes=["tasks:read"],
                )
            )
            await db.commit()

        read_reference = refs.reference_snapshot

        async def revoke_before_reference(*args, **kwargs):
            async with world.factory.begin() as owner:
                key = await owner.get(ApiKey, key_id)
                key.revoked_at = datetime.now(timezone.utc)
            return await read_reference(*args, **kwargs)

        monkeypatch.setattr(refs, "reference_snapshot", revoke_before_reference)
    acquire = storage_access.acquire
    admitted = []

    async def changed_after_real_permit(db, location_id, purpose, **kwargs):
        permit = await acquire(db, location_id, purpose, **kwargs)
        if location_id == world.location_id and purpose in {
            "reference_read",
            "original_cache_read",
        }:
            admitted.append(purpose)
            assert len(admitted) == 1
            async with world.factory() as observer:
                assert await observer.get(StorageOperation, permit.id) is not None
            if mutation == "jwt_expiry":
                monkeypatch.setattr(
                    credentials, "now", lambda: expiration + timedelta(seconds=1)
                )
            elif mutation == "jwt_generation":
                monkeypatch.setattr(
                    token_blacklist,
                    "get_user_generation",
                    AsyncMock(return_value=1),
                )
        return permit

    monkeypatch.setattr(storage_access, "acquire", changed_after_real_permit)
    image = None
    try:
        with pytest.raises(HTTPException) as error:
            image = await call_task(world, token, key_id=key_id)
        assert error.value.status_code == 403
    finally:
        if image is not None:
            image.close()
    if mutation == "key_revoke":
        assert error.value.detail == {"code": "api_key_authority_revoked"}
        assert not admitted
    else:
        assert admitted == [
            "original_cache_read" if path == "cache" else "reference_read"
        ]
    assert source_reads == cache_reads == []
    await assert_no_read_responsibility(world, cached_id)
    assert not world.deletions


@pytest.mark.parametrize("path", ["source", "cache"])
async def test_gate_change_after_reference_returns_rejects_and_closes_image(
    authority_world, monkeypatch, path
):
    world = authority_world
    cached_id = await prepared(world, path)
    source_reads, cache_reads = observe_reads(monkeypatch)
    token = create_access_token(str(world.actor.id), "employee")
    read = refs.reference_snapshot
    captured = []

    async def change_after_reference(*args, **kwargs):
        image = await read(*args, **kwargs)
        captured.append(image)
        async with world.factory() as db:
            gate = await db.get(Gate, world.location_id)
            assert gate is not None and gate.state == "active"
            await db.execute(
                update(Gate)
                .where(Gate.location_id == world.location_id)
                .values(revision=Gate.revision + 1)
            )
            await db.commit()
        return image

    monkeypatch.setattr(refs, "reference_snapshot", change_after_reference)
    image = None
    try:
        with pytest.raises(ContentError) as error:
            image = await call_task(world, token)
        assert error.value.status == 409
        assert error.value.code in {
            "content_binding_changed",
            "storage_maintenance_changed",
        }
        assert len(captured) == 1
        assert captured[0]._closed and captured[0].file.closed
    finally:
        if image is not None:
            image.close()
        for result in captured:
            result.close()
    if path == "cache":
        assert source_reads == [] and cache_reads
    else:
        assert source_reads == [True]
    await assert_no_read_responsibility(world, cached_id)
