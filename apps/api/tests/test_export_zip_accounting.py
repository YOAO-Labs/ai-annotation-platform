"""ZIP net changes need exclusive SDK events and complete version-set evidence."""

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import io
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from botocore.config import Config
from botocore.exceptions import ClientError
from botocore.hooks import HierarchicalEmitter
import pytest
from sqlalchemy import select

from app.config import settings
from app.db.models.storage_usage import StorageUsageMutation as Mutation
from app.services import (
    export_outputs,
    export_storage,
    export_zip_accounting as zip_usage,
)
from app.services import media_cache_accounting, storage, storage_access
from app.services.managed_s3_target import Target
from app.workers import lidar_export
from tests import live_provider_guard
from tests.test_export_storage import record, reference, endpoint
from tests.test_storage_usage_accounting import accounting_factory as accounting_factory

NOW = datetime(2026, 10, 26, tzinfo=timezone.utc)


class Provider:
    def __init__(self):
        self.meta = SimpleNamespace(
            endpoint_url=endpoint(),
            events=HierarchicalEmitter(),
            config=Config(retries={"max_attempts": 3}),
        )
        self.versions, self.closed, self.uploads = [], False, 0
        self.sends, self.version, self.hooks = 1, "new", True
        self.fail_list = self.lose_ack = self.pre_complete_failure = False
        self.read_version = None
        self.after_upload = None
        self.started = self.release = None

    def seed(self, data, version="old", marker=False):
        self.versions.append({"data": data, "version": version, "marker": marker})

    def head_object(self, **params):
        if not self.versions or self.versions[-1]["marker"]:
            headers = {}
            if self.versions:
                headers = {
                    "x-amz-delete-marker": "true",
                    "x-amz-version-id": self.versions[-1]["version"],
                }
            raise ClientError(
                {
                    "Error": {"Code": "NoSuchKey"},
                    "ResponseMetadata": {"HTTPStatusCode": 404, "HTTPHeaders": headers},
                },
                "HeadObject",
            )
        obj = self.versions[-1]
        return {
            "ResponseMetadata": {"HTTPStatusCode": 200},
            "VersionId": obj["version"],
            "ContentLength": len(obj["data"]),
            "ETag": hashlib.sha256(obj["data"]).hexdigest(),
        }

    def list_object_versions(self, **params):
        if self.fail_list:
            raise OSError("no history permission")
        result = {
            "ResponseMetadata": {"HTTPStatusCode": 200},
            "IsTruncated": False,
            "Versions": [],
            "DeleteMarkers": [],
        }
        for i, obj in enumerate(self.versions):
            row = {
                "Key": params["Prefix"],
                "VersionId": obj["version"],
                "LastModified": NOW,
                "IsLatest": i == len(self.versions) - 1,
            }
            if obj["marker"]:
                result["DeleteMarkers"].append(row)
            else:
                result["Versions"].append(
                    {
                        **row,
                        "Size": len(obj["data"]),
                        "ETag": hashlib.sha256(obj["data"]).hexdigest(),
                    }
                )
        return result

    def get_object(self, **params):
        return {
            **self.head_object(**params),
            "VersionId": self.read_version or self.versions[-1]["version"],
            "Body": io.BytesIO(self.versions[-1]["data"]),
        }

    def upload_file(self, filename, bucket, key, *, ExtraArgs):
        assert ExtraArgs == {"ContentType": "application/zip"}
        self.uploads += 1
        if self.started:
            self.started.set()
            self.release.wait(5)
        if self.pre_complete_failure:
            raise OSError("parts failed before complete")
        operation = "PutObject"
        model = SimpleNamespace(name=operation)
        if self.hooks:
            self.meta.events.emit(
                f"before-parameter-build.s3.{operation}",
                params={"Bucket": bucket, "Key": key},
                model=model,
            )
            for _ in range(self.sends):
                self.meta.events.emit(f"before-send.s3.{operation}")
        self.seed(Path(filename).read_bytes(), "new-" + str(self.uploads))
        if self.after_upload:
            self.after_upload(self)
        if self.lose_ack:
            raise OSError("lost final reply")
        version = (
            self.versions[-1]["version"] if self.version == "new" else self.version
        )
        if self.hooks:
            self.meta.events.emit(
                f"after-call.s3.{operation}",
                model=model,
                http_response=SimpleNamespace(status_code=200),
                parsed={
                    "ResponseMetadata": {"HTTPStatusCode": 200},
                    "VersionId": version,
                },
            )

    def close(self):
        self.closed = True


@pytest.fixture
def provider(monkeypatch):
    client = Provider()
    shared = SimpleNamespace(
        meta=SimpleNamespace(endpoint_url=endpoint(), config=client.meta.config)
    )
    monkeypatch.setattr(
        export_storage, "storage_service", SimpleNamespace(client=shared)
    )
    options = []

    def create(**kwargs):
        options.append(kwargs)
        return SimpleNamespace(client=client)

    monkeypatch.setattr(storage, "StorageService", create)
    client.options = options
    return client


def output(data=b"ZIP reviewed bytes", **changes):
    created = datetime.now(timezone.utc)
    return record(
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        created_at=created,
        expires_at=created + timedelta(days=7),
        **changes,
    )


def recorder(factory, row):
    return media_cache_accounting.Recorder(
        factory,
        "export_zip_outputs",
        row.id,
        row.storage_ref["endpoint_url"],
        row.storage_ref["bucket"],
        purpose="temporary",
    )


async def facts(factory):
    async with factory() as db:
        return list(
            await db.scalars(
                select(Mutation).order_by(Mutation.started_at, Mutation.id)
            )
        )


async def write(factory, provider, tmp_path, data=b"ZIP reviewed bytes", *, row=None):
    row = row or output(data)
    path = tmp_path / "result.zip"
    path.write_bytes(data)
    rec = recorder(factory, row)
    failure = None
    try:
        await asyncio.to_thread(export_storage.write, row, path, accounting=rec)
    except BaseException as exc:
        failure = exc
    assert provider.closed
    failure = await zip_usage.finish(rec, failure)
    if failure:
        raise failure
    return row


async def test_new_equal_overwrite_and_shrink_use_explicit_net_change(
    accounting_factory, provider, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        media_cache_accounting.Attempt,
        "verified",
        lambda *_: pytest.fail("ZIP must not infer full size"),
    )
    row = await write(accounting_factory, provider, tmp_path)
    await write(accounting_factory, provider, tmp_path, row=row)
    await write(accounting_factory, provider, tmp_path, b"ZIP", row=output(b"ZIP"))
    rows = await facts(accounting_factory)
    assert [r.current_delta_bytes for r in rows] == [
        len(b"ZIP reviewed bytes"),
        0,
        3 - len(b"ZIP reviewed bytes"),
    ]
    assert all(r.state == "confirmed" and r.purpose == "temporary" for r in rows)
    assert len({r.id for r in rows}) == 3 and all(r.owner_id == row.id for r in rows)
    assert all(r.object_key == reference()["object_key"] for r in rows)
    assert all(p["client_config"] is provider.meta.config for p in provider.options)


@pytest.mark.parametrize(
    "change",
    [
        "retry",
        "null",
        "missing",
        "no_hooks",
        "no_events",
        "read_version",
        "no_history",
        "old_null",
        "set_changed",
        "head_list",
    ],
)
async def test_observation_uncertainty_keeps_successful_zip_unknown(
    accounting_factory, provider, tmp_path, change
):
    if change == "retry":
        provider.sends = 2
    elif change == "null":
        provider.version = "null"
    elif change == "missing":
        provider.version = None
    elif change == "no_hooks":
        provider.hooks = False
    elif change == "no_events":
        provider.meta.events = None
        provider.hooks = False
    elif change == "read_version":
        provider.read_version = "different"
    elif change == "no_history":
        provider.fail_list = True
    elif change == "old_null":
        provider.seed(b"old", "null")
    elif change == "set_changed":
        provider.seed(b"old")
        provider.after_upload = lambda p: p.versions.pop(0)
    else:
        original = provider.list_object_versions

        def inconsistent(**params):
            page = original(**params)
            for item in page["Versions"]:
                item["IsLatest"] = False
            return page

        provider.list_object_versions = inconsistent
    await write(accounting_factory, provider, tmp_path)
    (row,) = await facts(accounting_factory)
    assert row.state == "unknown" and row.current_delta_bytes is None
    assert provider.uploads == 1


@pytest.mark.parametrize("where", ["before_complete", "lost_ack"])
async def test_managed_upload_failure_still_has_one_durable_fallback_intent(
    accounting_factory, provider, tmp_path, where
):
    provider.pre_complete_failure = where == "before_complete"
    provider.lose_ack = where == "lost_ack"
    with pytest.raises(export_storage.ExportStorageError):
        await write(accounting_factory, provider, tmp_path)
    (row,) = await facts(accounting_factory)
    assert row.state == "unknown" and row.current_delta_bytes is None
    assert provider.uploads == 1


async def test_intent_failure_prevents_managed_call_and_publish_failure_is_nonfatal(
    accounting_factory, provider, tmp_path, monkeypatch
):
    async def broken(*args, **kwargs):
        raise OSError("intent failed")

    original = media_cache_accounting.Recorder._begin
    monkeypatch.setattr(media_cache_accounting.Recorder, "_begin", broken)
    with pytest.raises(export_storage.ExportStorageError):
        await write(accounting_factory, provider, tmp_path)
    assert provider.uploads == 0 and not await facts(accounting_factory)
    monkeypatch.setattr(media_cache_accounting.Recorder, "_begin", original)
    monkeypatch.setattr(media_cache_accounting.accounting, "publish", broken)
    await write(accounting_factory, provider, tmp_path)
    (row,) = await facts(accounting_factory)
    assert row.state == "pending" and row.current_delta_bytes is None


def test_exact_snapshot_ignores_other_prefix_keys_and_validates_markers(provider):
    key = reference()["object_key"]
    provider.seed(b"old")
    original = provider.list_object_versions

    def unrelated(**params):
        page = original(**params)
        page["Versions"].append({"Key": key + "unrelated", "VersionId": "null"})
        return page

    provider.list_object_versions = unrelated
    assert zip_usage.snapshot(provider, "bucket", key).size == 3
    provider.seed(b"", "deleted", marker=True)
    observed = zip_usage.snapshot(provider, "bucket", key)
    assert observed.size == 0 and observed.current == ("marker", "deleted")


@pytest.mark.parametrize(
    "change", ["duplicate", "truncated", "moving_head", "unsafe_404", "marker_mismatch"]
)
def test_snapshot_incomplete_or_inconsistent_never_proves_absence(provider, change):
    key = reference()["object_key"]
    provider.seed(b"old")
    original = provider.list_object_versions

    def listing(**params):
        page = original(**params)
        if change == "duplicate":
            page["Versions"] *= 2
        elif change == "truncated":
            page.update(
                IsTruncated=True, NextKeyMarker=key, NextVersionIdMarker="repeated"
            )
        elif change == "moving_head":
            provider.seed(b"moved", "other")
        return page

    provider.list_object_versions = listing
    if change == "unsafe_404":

        def head(**params):
            raise ClientError(
                {
                    "Error": {"Code": "AccessDenied"},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                },
                "HeadObject",
            )

        provider.head_object = head
    elif change == "marker_mismatch":
        provider.seed(b"", "marker", marker=True)

        def head(**params):
            raise ClientError(
                {
                    "Error": {"Code": "NoSuchKey"},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                },
                "HeadObject",
            )

        provider.head_object = head
    assert zip_usage.snapshot(provider, "bucket", key) is None


async def test_pending_intent_is_committed_before_upload_and_publish_cancel_drains(
    accounting_factory, provider, tmp_path, monkeypatch
):
    original_upload = provider.upload_file
    loop = asyncio.get_running_loop()

    async def pending():
        (fact,) = await facts(accounting_factory)
        assert fact.state == "pending" and fact.current_delta_bytes is None

    def upload(*args, **kwargs):
        asyncio.run_coroutine_threadsafe(pending(), loop).result(3)
        return original_upload(*args, **kwargs)

    provider.upload_file = upload
    entered, release = asyncio.Event(), asyncio.Event()
    original_publish = media_cache_accounting.accounting.publish

    async def publish(*args, **kwargs):
        entered.set()
        await release.wait()
        await original_publish(*args, **kwargs)

    monkeypatch.setattr(media_cache_accounting.accounting, "publish", publish)
    task = asyncio.create_task(write(accounting_factory, provider, tmp_path))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and provider.closed
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    (fact,) = await facts(accounting_factory)
    assert fact.state == "confirmed"


async def test_unsettled_native_client_close_retains_written_fact_without_delta(
    accounting_factory, provider, tmp_path
):
    def close():
        provider.closed = True
        raise OSError("unsettled native close")

    provider.close = close
    with pytest.raises(export_storage.ExportStorageError) as error:
        await write(accounting_factory, provider, tmp_path)
    assert error.value.storage_operation_unsettled
    (fact,) = await facts(accounting_factory)
    assert fact.physical_outcome == "written" and fact.current_delta_bytes is None


async def test_unexpected_ledger_finish_error_preserves_business_result():
    accounting = SimpleNamespace(
        finish=AsyncMock(side_effect=OSError("ledger unavailable"))
    )
    assert await zip_usage.finish(accounting, None) is None
    original = ValueError("business failure")
    assert await zip_usage.finish(accounting, original) is original


async def test_finish_cancellation_still_propagates_after_recorder_drains():
    interrupted = asyncio.CancelledError()
    accounting = SimpleNamespace(finish=AsyncMock(side_effect=interrupted))
    assert await zip_usage.finish(accounting, None) is interrupted


@pytest.mark.parametrize("owner", ["ordinary", "independent"])
@pytest.mark.parametrize("interrupt", [False, True])
async def test_both_async_owners_preserve_guards_and_drain_before_accounting(
    accounting_factory, provider, tmp_path, monkeypatch, owner, interrupt
):
    data = b"ZIP reviewed bytes"
    row = output(data, state="pending")
    path = tmp_path / "owned.zip"
    path.write_bytes(data)
    db = SimpleNamespace(bind=accounting_factory.kw["bind"], commit=AsyncMock())
    order = []

    class Permit:
        revision = 3

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            assert provider.closed
            order.append("settled")

    async def acquire(*args, **kwargs):
        order.append("acquired")
        return Permit()

    async def settle(*args):
        assert provider.closed
        order.append("settled")

    monkeypatch.setattr(export_storage, "io_target", AsyncMock(return_value=None))
    monkeypatch.setattr(export_outputs, "_locked", AsyncMock(return_value=row))
    monkeypatch.setattr(storage_access, "acquire", acquire)
    monkeypatch.setattr(storage_access, "check", AsyncMock())
    monkeypatch.setattr(lidar_export.transfer, "_acquire", acquire)
    monkeypatch.setattr(lidar_export.transfer, "_settle", settle)
    monkeypatch.setattr(lidar_export.bundles, "check_claim", AsyncMock())
    monkeypatch.setattr(
        lidar_export.transfer, "_session", lambda db: accounting_factory()
    )
    original = media_cache_accounting.accounting.publish

    async def publish(*args, **kwargs):
        assert provider.closed and order[-1] == "settled"
        return await original(*args, **kwargs)

    monkeypatch.setattr(media_cache_accounting.accounting, "publish", publish)

    async def guard(_db):
        order.append("guard")

    async def execute():
        if owner == "ordinary":
            return await export_outputs.write(db, row, path)
        rec = zip_usage.recorder(db, row)
        return await lidar_export._io(
            db,
            row,
            lambda: export_storage.write(row, path, accounting=rec),
            guard=guard,
            accounting=rec,
        )

    if interrupt:
        provider.started, provider.release = threading.Event(), threading.Event()
    task = asyncio.create_task(execute())
    if interrupt:
        assert await asyncio.to_thread(provider.started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not provider.closed
        provider.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await task
    (fact,) = await facts(accounting_factory)
    assert fact.current_delta_bytes == len(data) and fact.state == "confirmed"
    assert order.count("acquired") == order.count("settled") == 1
    if owner == "independent":
        assert order[0] == "guard"


@pytest.mark.parametrize("schema,size", [(1, 123), (2, 9 * 1024**2)])
async def test_actual_sdk_single_put_and_managed_multipart(
    accounting_factory, tmp_path, monkeypatch, schema, size
):
    bucket = live_provider_guard.owned_bucket(
        settings.minio_export_bucket, purpose="export"
    )
    service = storage.StorageService()
    service.client.put_bucket_versioning(
        Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
    )
    monkeypatch.setattr(export_storage, "storage_service", service)
    data = b"z" * size
    ref = reference(bucket=bucket, output_id=uuid.uuid4())
    if schema == 2:
        ref.update(schema=2, binding_generation=2)
    row = output(
        data, id=uuid.UUID(ref["object_key"].rsplit("/", 1)[-1][:-4]), storage_ref=ref
    )
    path = tmp_path / "actual.zip"
    path.write_bytes(data)
    target = (
        Target(
            export_storage.LEGACY_LOCATION_ID,
            1,
            None,
            endpoint(),
            bucket,
            "",
            purpose="export",
            _legacy_client=service.client,
        )
        if schema == 2
        else None
    )
    try:
        for _ in range(2):
            rec = recorder(accounting_factory, row)
            await asyncio.to_thread(
                export_storage.write, row, path, target=target, accounting=rec
            )
            await rec.finish(settled=True)
        rows = await facts(accounting_factory)
        assert [r.current_delta_bytes for r in rows] == [size, 0]
        assert all(r.evidence["final_send_count"] == 1 for r in rows)
        head = service.client.head_object(Bucket=bucket, Key=ref["object_key"])
        assert rows[-1].evidence["returned_version"] == head["VersionId"]
        assert head["ContentLength"] == size
    finally:
        versions = service.client.list_object_versions(
            Bucket=bucket, Prefix=ref["object_key"]
        )
        objects = [
            {"Key": item["Key"], "VersionId": item["VersionId"]}
            for field in ("Versions", "DeleteMarkers")
            for item in versions.get(field, [])
            if item["Key"] == ref["object_key"]
        ]
        if objects:
            service.client.delete_objects(Bucket=bucket, Delete={"Objects": objects})
        assert not service.client.list_object_versions(
            Bucket=bucket, Prefix=ref["object_key"]
        ).get("Versions")
        service.client.close()
