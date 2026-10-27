"""Actual async owners publish independently after their native resource drain."""

import asyncio
import gzip
import io
import threading
from types import SimpleNamespace
import uuid

from fastapi import HTTPException
import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.requests import Request

from app.config import settings
from app.db.models.storage_maintenance import StorageOperation
from app.db.models.storage_usage import StorageUsageMutation as Mutation
from app.services import (
    avatar,
    avatar_uploads,
    comment_attachment_uploads,
    guide_asset_uploads,
    media_cache_accounting,
    raster_mask_objects,
    storage,
    storage_access,
)
from app.services.raster_mask_storage import (
    build_rle_gzip_reference,
    canonical_rle_bytes,
)
from tests import live_provider_guard
from tests.test_bug_attachment_accounting import Provider
from tests.test_comment_polish import _seed_project_with_annotation
from tests.test_conditional_write_accounting import (
    CASES,
    MODULES,
    PRODUCERS,
    records,
    transport,
)


@pytest.fixture
async def caller(db_session, super_admin):
    factory = async_sessionmaker(
        storage_access.engine(db_session), expire_on_commit=False
    )
    previous = {row.id for row in await records(factory)}
    project, annotation = await _seed_project_with_annotation(
        db_session, super_admin[0].id
    )
    state = SimpleNamespace(
        db=db_session,
        actor=super_admin[0],
        token=super_admin[1],
        factory=factory,
        previous=previous,
        request=Request(
            {
                "type": "http",
                "method": "POST",
                "scheme": "http",
                "path": "/test-upload",
                "query_string": b"",
                "server": ("test", 80),
                "client": ("127.0.0.1", 12345),
                "headers": [(b"authorization", ("Bearer " + super_admin[1]).encode())],
            }
        ),
        project_id=project.id,
        annotation_id=annotation.id,
    )
    try:
        yield state
    finally:
        owned = [row for row in await records(factory) if row.id not in previous]
        purposes = [
            "avatar_upload"
            if row.producer == "avatar_uploads"
            else "comment_attachment_write_" + row.owner_id.hex
            if row.producer == "comment_attachment_uploads"
            else "guide_asset_write_" + row.owner_id.hex
            if row.producer == "guide_asset_uploads"
            else "raster_object_write_" + row.owner_id.hex
            for row in owned
        ]
        async with factory() as db:
            if purposes:
                await db.execute(
                    delete(StorageOperation).where(
                        StorageOperation.purpose.in_(purposes)
                    )
                )
            if owned:
                await db.execute(
                    delete(Mutation).where(Mutation.id.in_([row.id for row in owned]))
                )
            await db.commit()


async def operation(kind, caller, service):
    db, actor, request = caller.db, caller.actor, caller.request
    if kind == "avatar":
        from functools import partial

        from fastapi.security import HTTPAuthorizationCredentials

        from app.api.v1.account_commands import execute_account, get_account_commands
        from app.deps import get_current_principal
        from app.services.identity_gateway import get_identity_gateway

        identity = get_identity_gateway()
        credentials = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials=caller.token
        )
        principal = await get_current_principal(request, credentials, db, identity)
        commands = get_account_commands(principal=principal, db=db, identity=identity)
        execute = partial(execute_account, request, credentials, commands)
        authority = await avatar_uploads.capture_authority(db, execute, principal)
        data = b"complete normalized avatar"

        async def call():
            return await avatar_uploads.upload(
                db, execute, data, principal=principal, authority=authority
            )
    elif kind == "comment":
        data = b"comment attachment bytes"
        proof, context = await comment_attachment_uploads.capture_upload(
            db, actor, request, caller.annotation_id
        )

        async def call():
            return await comment_attachment_uploads.upload(
                db, proof, context, data, "text/plain", "comment.txt"
            )
    elif kind == "guide":
        data = b'<svg xmlns="http://www.w3.org/2000/svg"><text>guide</text></svg>'
        proof = await guide_asset_uploads.capture_upload(
            db, actor, request, caller.project_id
        )

        async def call():
            return await guide_asset_uploads.upload(
                db, proof, caller.project_id, data, "image/svg+xml", "guide.svg"
            )
    else:
        counts = [int(value, 16) + 1 for value in uuid.uuid4().hex]
        rle = {"encoding": "coco_rle", "size": [1, sum(counts)], "counts": counts}
        reference = build_rle_gzip_reference(rle)
        data = gzip.compress(canonical_rle_bytes(rle), mtime=0)

        async def call():
            return await raster_mask_objects.store(
                reference, data, db=db, storage=service
            )

    return call, data


def patch_service(monkeypatch, kind, service):
    monkeypatch.setattr(MODULES[kind], "storage_service", service)
    if kind == "avatar":
        monkeypatch.setattr(avatar, "storage_service", service)
    elif kind == "comment":
        monkeypatch.setattr(comment_attachment_uploads, "storage_service", service)
    elif kind == "guide":
        monkeypatch.setattr(guide_asset_uploads, "storage_service", service)


@pytest.mark.parametrize("kind", CASES)
@pytest.mark.parametrize(
    "failure",
    [
        None,
        "revoke",
        "settle",
        "body_and_settle",
        "cancel",
        "base_exception",
        "publish_error",
        "client_close",
    ],
)
async def test_real_owner_keeps_physical_facts_and_drains_before_finish(
    caller, monkeypatch, kind, failure
):
    provider = Provider()
    shared, _ = transport(monkeypatch, kind, provider)
    service = SimpleNamespace(client=shared)
    patch_service(monkeypatch, kind, service)
    # Avatar allocation uses its configured bucket while all annotation producers
    # resolve their existing purpose binding through the ordinary allocation path.
    service.avatars_bucket = settings.minio_avatars_bucket
    call, data = await operation(kind, caller, service)
    original_check = storage_access.check

    async def check(*args, **kwargs):
        if failure == "revoke" and kind != "guide" and provider.puts:
            raise HTTPException(403, {"code": "late_revocation"})
        return await original_check(*args, **kwargs)

    monkeypatch.setattr(storage_access, "check", check)
    if failure == "revoke" and kind == "guide":
        from app.services.identity_session_data import IdentitySessionData

        original_run = storage_access.run
        actor_id = caller.actor.id

        async def revoke_after_io(*args, **kwargs):
            result = await original_run(*args, **kwargs)
            await IdentitySessionData(caller.db).advance_credential_generation(actor_id)
            await caller.db.commit()
            return result

        monkeypatch.setattr(storage_access, "run", revoke_after_io)
    if failure in {"settle", "body_and_settle"}:

        async def settle(permit):
            raise OSError("settle failed")

        monkeypatch.setattr(storage_access.Permit, "_settle", settle)
    if failure == "body_and_settle":

        class Broken(io.BytesIO):
            def close(self):
                raise OSError("body close failed")

        provider.body_factory = Broken
    if failure == "client_close":

        def close():
            provider.closed = True
            raise OSError("client close result unknown")

        provider.close = close
    if failure == "publish_error":

        async def publish(*args, **kwargs):
            raise OSError("accounting deferred")

        monkeypatch.setattr(media_cache_accounting.accounting, "publish", publish)
    if failure == "base_exception":

        class NativeStopped(BaseException):
            pass

        provider.after_put = lambda: (_ for _ in ()).throw(NativeStopped())
    if failure == "cancel":
        provider.started, provider.release = threading.Event(), threading.Event()
    task = asyncio.create_task(call())
    if failure == "cancel":
        assert await asyncio.to_thread(provider.started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not provider.closed
        provider.release.set()
    try:
        if failure in {
            "revoke",
            "settle",
            "body_and_settle",
            "cancel",
            "base_exception",
            "client_close",
        }:
            with pytest.raises(BaseException) as caught:
                await task
            if failure == "revoke":
                assert (
                    isinstance(caught.value, HTTPException)
                    and caught.value.status_code == 403
                )
            if failure == "cancel":
                assert isinstance(caught.value, asyncio.CancelledError)
            if failure == "base_exception":
                assert isinstance(caught.value, NativeStopped)
        else:
            await task
        facts = [
            row
            for row in await records(caller.factory)
            if row.producer == PRODUCERS[kind] and row.id not in caller.previous
        ]
        (fact,) = facts
        if failure in {"settle", "body_and_settle", "base_exception", "client_close"}:
            assert fact.state == "unknown" and fact.current_delta_bytes is None
        elif failure == "publish_error":
            assert fact.state == "pending" and fact.current_delta_bytes is None
        else:
            assert fact.state == "confirmed" and fact.current_delta_bytes == len(data)
        assert provider.closed
        if failure != "body_and_settle":
            assert all(body.closed for body in provider.bodies)
        # A ready RLE reuse verifies the winner without inventing another PUT.
        if kind == "raster" and failure is None:
            assert await call()
            assert provider.puts == 1
            assert (
                len(
                    [
                        row
                        for row in await records(caller.factory)
                        if row.producer == PRODUCERS[kind]
                        and row.id not in caller.previous
                    ]
                )
                == 1
            )
    finally:
        for body in provider.bodies:
            io.BytesIO.close(body)


@pytest.mark.parametrize("kind", CASES)
async def test_real_minio_actual_production_owner(caller, monkeypatch, kind):
    purpose = "avatars" if kind == "avatar" else "annotations"
    bucket = live_provider_guard.owned_bucket(
        settings.minio_avatars_bucket if kind == "avatar" else settings.minio_bucket,
        purpose=purpose,
    )
    service = storage.StorageService()
    service.client.put_bucket_versioning(
        Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
    )
    patch_service(monkeypatch, kind, service)
    call, data = await operation(kind, caller, service)
    key = None
    try:
        await call()
        facts = [
            row
            for row in await records(caller.factory)
            if row.producer == PRODUCERS[kind] and row.id not in caller.previous
        ]
        (fact,) = facts
        key = fact.object_key
        assert fact.state == "confirmed" and fact.current_delta_bytes == len(data)
        assert fact.evidence["final_send_count"] == 1 and fact.evidence["full_readback"]
        head = service.client.head_object(Bucket=bucket, Key=key)
        assert head["VersionId"] == fact.evidence["returned_version"]
        assert head["ContentLength"] == len(data)
        if kind == "raster":
            await call()
            assert (
                len(
                    [
                        row
                        for row in await records(caller.factory)
                        if row.producer == PRODUCERS[kind]
                        and row.id not in caller.previous
                    ]
                )
                == 1
            )
    finally:
        if key:
            versions = service.client.list_object_versions(Bucket=bucket, Prefix=key)
            entries = [
                {"Key": row["Key"], "VersionId": row["VersionId"]}
                for field in ("Versions", "DeleteMarkers")
                for row in versions.get(field, [])
                if row["Key"] == key
            ]
            if entries:
                service.client.delete_objects(
                    Bucket=bucket, Delete={"Objects": entries}
                )
            result = service.client.list_object_versions(Bucket=bucket, Prefix=key)
            assert not any(
                row["Key"] == key
                for field in ("Versions", "DeleteMarkers")
                for row in result.get(field, [])
            )
        service.client.close()
