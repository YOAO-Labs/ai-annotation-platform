"""Attachment facts require a single observed condition and real owner drain."""

import asyncio
import io
import os
import re
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from botocore.config import Config
from botocore.exceptions import ClientError
from botocore.hooks import HierarchicalEmitter
from fastapi import HTTPException
import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.db.models.bug_attachment_upload import BugAttachmentUpload
from app.db.models.storage_usage import StorageUsageMutation as Mutation
from app.services import bug_attachment_accounting as accounting
from app.services import (
    bug_attachment_storage as physical,
    bug_attachment_uploads as uploads,
)
from app.services import media_cache_accounting, storage, storage_access
from app.services.managed_s3_target import Target
from tests.test_bug_attachment_storage import RAW, endpoint, record
from tests.test_bug_attachment_uploads import clear_closed_test_operations
from tests.test_storage_usage_accounting import accounting_factory as accounting_factory


def s3_error(code, status=None):
    return ClientError(
        {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "PutObject",
    )


class Provider:
    def __init__(self):
        self.meta = SimpleNamespace(
            endpoint_url=endpoint(),
            config=Config(retries={"max_attempts": 3}),
            events=HierarchicalEmitter(),
        )
        self.objects, self.current, self.bodies = {}, {}, []
        self.puts, self.sends, self.closed = 0, 1, False
        self.version, self.failure, self.params_change = "actual", None, {}
        self.hooks = True
        self.wrong_read = self.replace_current = False
        self.started = self.release = None
        self.before_put = self.after_put = None
        self.body_factory = io.BytesIO

    def put_object(self, **params):
        self.puts += 1
        if self.before_put:
            self.before_put()
        assert params["IfNoneMatch"] == "*"
        if self.hooks:
            self.meta.events.emit(
                "before-parameter-build.s3.PutObject",
                params={**params, **self.params_change},
            )
            for _ in range(self.sends):
                self.meta.events.emit("before-send.s3.PutObject")
        if self.started:
            self.started.set()
            self.release.wait(5)
        if self.failure:
            raise self.failure
        key = params["Bucket"], params["Key"]
        if key in self.current:
            raise s3_error("PreconditionFailed", 412)
        version = "null" if self.version == "null" else "actual-" + str(self.puts)
        self.objects[key, version] = bytes(params["Body"])
        self.current[key] = version
        if self.after_put:
            self.after_put()
        return {
            "ResponseMetadata": {"HTTPStatusCode": 200},
            "VersionId": version if self.version == "actual" else self.version,
        }

    def head_object(self, **params):
        key = params["Bucket"], params["Key"]
        version = params.get("VersionId") or self.current[key]
        data = self.objects[key, version]
        if self.wrong_read and params.get("VersionId"):
            version = "wrong-version"
        if self.replace_current and not params.get("VersionId"):
            version = "foreign-current"
        return {
            "ResponseMetadata": {"HTTPStatusCode": 200},
            "VersionId": version,
            "ContentLength": len(data),
            "ETag": "opaque",
        }

    def get_object(self, **params):
        key = params["Bucket"], params["Key"]
        version = params.get("VersionId") or self.current[key]
        body = self.body_factory(self.objects[key, version])
        self.bodies.append(body)
        return {**self.head_object(**params), "Body": body}

    def close(self):
        self.closed = True


@pytest.fixture
def provider(monkeypatch):
    client = Provider()
    shared = SimpleNamespace(
        meta=SimpleNamespace(endpoint_url=endpoint(), config=client.meta.config)
    )
    monkeypatch.setattr(
        physical,
        "storage_service",
        SimpleNamespace(
            client=shared, bug_reports_bucket=settings.minio_bug_reports_bucket
        ),
    )
    configs = []

    def create(**kwargs):
        configs.append(kwargs["client_config"])
        return SimpleNamespace(client=client)

    monkeypatch.setattr(storage, "StorageService", create)
    client.configs = configs
    return client


async def facts(factory):
    async with factory() as db:
        return list(
            await db.scalars(
                select(Mutation)
                .where(Mutation.producer == "bug_attachment_uploads")
                .order_by(Mutation.started_at, Mutation.id)
            )
        )


async def write(factory, provider, *, row=None):
    row = row or record()
    ref = row.storage_ref
    rec = media_cache_accounting.Recorder(
        factory,
        "bug_attachment_uploads",
        row.id,
        ref["endpoint_url"],
        ref["bucket"],
        purpose="other_platform",
    )
    failure = None
    try:
        await asyncio.to_thread(physical.write, row, RAW, accounting=rec)
    except BaseException as exc:
        failure = exc
    assert provider.closed
    failure = await accounting.finish(rec, failure)
    if failure:
        raise failure
    return row


async def test_exact_saved_scope_intent_before_put_and_single_412_zero(
    accounting_factory, provider
):
    loop = asyncio.get_running_loop()

    async def pending():
        rows = await facts(accounting_factory)
        assert rows[-1].state == "pending"

    provider.before_put = lambda: asyncio.run_coroutine_threadsafe(
        pending(), loop
    ).result(3)
    row = await write(accounting_factory, provider)
    with pytest.raises(physical.BugAttachmentStorageError):
        await write(accounting_factory, provider)
    first, second = await facts(accounting_factory)
    assert first.state == "confirmed" and first.current_delta_bytes == len(RAW)
    assert second.state == "confirmed" and second.current_delta_bytes == 0
    assert second.physical_outcome == "not_written"
    assert first.owner_id == second.owner_id == row.id and first.id != second.id
    assert (
        first.object_key == row.storage_ref["object_key"]
        and first.purpose == "other_platform"
    )
    assert all(config is provider.meta.config for config in provider.configs)
    assert all(body.closed for body in provider.bodies)


@pytest.mark.parametrize(
    "change",
    [
        "retry_success",
        "retry_412",
        "409",
        "bare_412",
        "lost_ack",
        "null",
        "missing",
        "no_hooks",
        "no_events",
        "condition",
        "bucket",
        "key",
        "current_replaced",
    ],
)
async def test_uncertain_submission_never_confirms(
    accounting_factory, provider, change
):
    fails = False
    if change == "retry_success":
        provider.sends = 2
    elif change == "retry_412":
        provider.sends = 2
        provider.failure = s3_error("412", 412)
        fails = True
    elif change == "409":
        provider.failure = s3_error("ConditionalRequestConflict", 409)
        fails = True
    elif change == "bare_412":
        provider.failure = s3_error("412")
        fails = True
    elif change == "lost_ack":
        provider.failure = OSError("unknown ACK")
        fails = True
    elif change == "null":
        provider.version = "null"
    elif change == "missing":
        provider.version = None
    elif change == "no_hooks":
        provider.hooks = False
    elif change == "no_events":
        provider.hooks = False
        provider.meta.events = None
    elif change == "condition":
        provider.params_change = {"IfNoneMatch": None}
    elif change == "bucket":
        provider.params_change = {"Bucket": "different"}
    elif change == "key":
        provider.params_change = {"Key": "different"}
    else:
        provider.replace_current = True
    if fails:
        with pytest.raises(physical.BugAttachmentStorageError):
            await write(accounting_factory, provider)
    else:
        await write(accounting_factory, provider)
    (row,) = await facts(accounting_factory)
    assert row.state == "unknown" and row.current_delta_bytes is None


@pytest.mark.parametrize("where", ["version", "body", "client"])
async def test_incomplete_readback_or_native_close_never_confirms(
    accounting_factory, provider, where
):
    if where == "version":
        provider.wrong_read = True
    elif where == "body":

        class Broken(io.BytesIO):
            def close(self):
                super().close()
                raise OSError("body close failed")

        provider.body_factory = Broken
    else:

        def close():
            provider.closed = True
            raise OSError("client close failed")

        provider.close = close
    with pytest.raises(physical.BugAttachmentStorageError):
        await write(accounting_factory, provider)
    (row,) = await facts(accounting_factory)
    assert (
        row.physical_outcome == "written"
        and row.state == "unknown"
        and row.current_delta_bytes is None
    )


async def test_intent_failure_prevents_put_and_ledger_failure_is_nonfatal(
    accounting_factory, provider, monkeypatch
):
    original = media_cache_accounting.Recorder._begin

    async def broken(*args, **kwargs):
        raise OSError("ledger unavailable")

    monkeypatch.setattr(media_cache_accounting.Recorder, "_begin", broken)
    with pytest.raises(physical.BugAttachmentStorageError):
        await write(accounting_factory, provider)
    assert provider.puts == 0 and not await facts(accounting_factory)
    monkeypatch.setattr(media_cache_accounting.Recorder, "_begin", original)
    monkeypatch.setattr(media_cache_accounting.accounting, "publish", broken)
    await write(accounting_factory, provider)
    (row,) = await facts(accounting_factory)
    assert row.state == "pending"


async def test_new_cancel_during_ledger_drain_is_visible_over_old_business_failure(
    accounting_factory, monkeypatch
):
    rec = media_cache_accounting.Recorder(
        accounting_factory,
        "bug_attachment_uploads",
        uuid.uuid4(),
        endpoint(),
        "saved",
        purpose="other_platform",
    )
    await asyncio.to_thread(
        rec.begin, "saved/key", {"sha256": "a" * 64, "size_bytes": 3}
    )
    entered, release = asyncio.Event(), asyncio.Event()
    original = media_cache_accounting.accounting.publish

    async def publish(*args, **kwargs):
        entered.set()
        await release.wait()
        await original(*args, **kwargs)

    monkeypatch.setattr(media_cache_accounting.accounting, "publish", publish)
    previous = storage_access.mark_unsettled(HTTPException(403, "business revoked"))
    task = asyncio.create_task(accounting.finish(rec, previous))
    await asyncio.wait_for(entered.wait(), 3)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    result = await task
    assert (
        isinstance(result, asyncio.CancelledError)
        and result.storage_operation_unsettled
    )


async def test_ordinary_ledger_finish_error_preserves_business_result():
    rec = SimpleNamespace(finish=AsyncMock(side_effect=OSError("ledger unavailable")))
    assert await accounting.finish(rec, None) is None
    original = HTTPException(403, "revoked")
    assert await accounting.finish(rec, original) is original


@pytest.fixture
async def caller(db_session, annotator, monkeypatch):
    factory = async_sessionmaker(
        storage_access.engine(db_session), expire_on_commit=False
    )
    previous = {r.id for r in await facts(factory)}
    state = SimpleNamespace(
        db=db_session,
        authority=uploads.Authority(annotator[0].id, None, None),
        factory=factory,
    )
    try:
        yield state
    finally:
        owned = [r for r in await facts(factory) if r.id not in previous]
        for row in owned:
            # Synthetic close/settle failures own no live SDK handles after return.
            await clear_closed_test_operations(
                db_session,
                physical.LEGACY_LOCATION_ID,
                "bug_attachment_write_" + row.owner_id.hex,
            )
        async with factory() as db:
            if owned:
                await db.execute(
                    delete(Mutation).where(Mutation.id.in_([r.id for r in owned]))
                )
                await db.commit()


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "revoke",
        "ready_commit",
        "cancel",
        "soft_limit",
        "settle",
        "body_and_settle",
    ],
)
async def test_actual_upload_owner_preserves_facts_and_original_drain(
    caller, provider, monkeypatch, failure
):
    original_reauth = uploads.reauthorize
    calls = 0

    async def authorize(*args):
        nonlocal calls
        calls += 1
        if failure == "revoke" and provider.puts:
            raise HTTPException(403, {"code": "revoked"})
        return await original_reauth(*args)

    monkeypatch.setattr(uploads, "reauthorize", authorize)
    if failure == "ready_commit":
        original_commit = caller.db.commit

        async def commit():
            if any(
                isinstance(row, BugAttachmentUpload) and row.state == "ready"
                for row in caller.db.dirty
            ):
                raise OSError("ready commit failed")
            return await original_commit()

        monkeypatch.setattr(caller.db, "commit", commit)
    if failure in {"settle", "body_and_settle"}:

        async def broken_settle(permit):
            raise OSError("permit settlement failed")

        monkeypatch.setattr(storage_access.Permit, "_settle", broken_settle)
    if failure == "body_and_settle":
        provider.wrong_read = True
    if failure == "soft_limit":

        class SoftLimit(BaseException):
            pass

        provider.after_put = lambda: (_ for _ in ()).throw(SoftLimit())
    if failure == "cancel":
        provider.started, provider.release = threading.Event(), threading.Event()
    task = asyncio.create_task(
        uploads.upload(caller.db, caller.authority, RAW, "image/png", "image.png")
    )
    if failure == "cancel":
        assert await asyncio.to_thread(provider.started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not provider.closed
        provider.release.set()
    if failure:
        with pytest.raises(BaseException) as caught:
            await task
        if failure == "cancel":
            assert isinstance(caught.value, asyncio.CancelledError)
        elif failure == "revoke":
            assert caught.value.status_code == 403
        elif failure == "body_and_settle":
            assert (
                caught.value.detail["code"]
                == "bug_attachment_storage_integrity_mismatch"
            )
    else:
        await task
    assert provider.closed and all(body.closed for body in provider.bodies)
    (row,) = await facts(caller.factory)
    if failure in {"settle", "body_and_settle", "soft_limit"}:
        assert row.state == "unknown" and row.current_delta_bytes is None
    else:
        assert row.state == "confirmed" and row.current_delta_bytes == len(RAW)


@pytest.mark.parametrize("schema", [1, 2])
async def test_real_minio_production_upload_and_single_duplicate_condition(
    caller, monkeypatch, schema
):
    assert os.environ.get("AAP_WORKTREE_MODE") == "test"
    bucket = settings.minio_bug_reports_bucket
    assert re.fullmatch(r"aap-wt-[0-9a-f]{16}-test-bug-reports", bucket)
    service = storage.StorageService()
    service.client.put_bucket_versioning(
        Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
    )
    monkeypatch.setattr(physical, "storage_service", service)
    if schema == 2:
        original = physical.allocate

        async def allocate(*args):
            ref = await original(*args)
            return {**ref, "schema": 2, "binding_generation": 2}

        monkeypatch.setattr(physical, "allocate", allocate)

        async def target(*args):
            return Target(
                physical.LEGACY_LOCATION_ID,
                1,
                None,
                endpoint(),
                bucket,
                "",
                purpose="bug-reports",
                _legacy_client=service.client,
            )

        monkeypatch.setattr(physical, "io_target", target)
    key = None
    try:
        result = await uploads.upload(
            caller.db, caller.authority, RAW, "image/png", "actual.png"
        )
        key = result.storage_key
        (first,) = await facts(caller.factory)
        row = await caller.db.get(BugAttachmentUpload, first.owner_id)
        assert row.state == "ready" and first.current_delta_bytes == len(RAW)
        head = service.client.head_object(Bucket=bucket, Key=key)
        assert (
            first.evidence["returned_version"]
            == row.object_version_id
            == head["VersionId"]
        )
        rec = accounting.recorder(caller.db, uploads.snapshot(row))
        with pytest.raises(physical.BugAttachmentStorageError):
            await asyncio.to_thread(
                physical.write, uploads.snapshot(row), RAW, accounting=rec
            )
        await accounting.finish(rec, None)
        first, second = await facts(caller.factory)
        assert second.state == "confirmed" and second.current_delta_bytes == 0
        assert (
            first.evidence["final_send_count"]
            == second.evidence["final_send_count"]
            == 1
        )
    finally:
        if key:
            versions = service.client.list_object_versions(Bucket=bucket, Prefix=key)
            objects = [
                {"Key": item["Key"], "VersionId": item["VersionId"]}
                for field in ("Versions", "DeleteMarkers")
                for item in versions.get(field, [])
                if item["Key"] == key
            ]
            if objects:
                service.client.delete_objects(
                    Bucket=bucket, Delete={"Objects": objects}
                )
            assert not service.client.list_object_versions(
                Bucket=bucket, Prefix=key
            ).get("Versions")
        service.client.close()
