"""Real owner/permit boundaries for thumbnail, avatar and attachment cleanup."""

import asyncio
from contextlib import contextmanager
from datetime import timedelta
import hashlib
from types import SimpleNamespace
import threading
import uuid

from botocore.exceptions import ClientError
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.avatar_upload import AvatarUpload
from app.db.models.bug_attachment_upload import BugAttachmentUpload
from app.db.models.storage_maintenance import StorageOperation
from app.db.models.storage_usage import StorageUsageMutation as Mutation
from app.db.models.thumbnail_output import ThumbnailOutput
from app.services import avatar_storage, avatar_uploads
from app.services import bug_attachment_storage as bug_storage
from app.services import bug_attachment_uploads as bug_uploads
from app.services import media_cache_storage, object_removal_accounting as removal
from app.services import storage, storage_access, storage_usage_accounting as usage
from app.services import thumbnail_cleanup as thumbnails, thumbnail_outputs as outputs
from app.services.content_snapshot import ContentError
from app.services.dataset_assets import LEGACY_LOCATION_ID
from tests.test_export_media_removal_accounting import MemoryRecorder
from tests.test_media_cache_targets import media_db as media_db
from tests.test_object_removal_storage import Objects

KINDS = ("thumbnail", "avatar", "bug_attachment")
RAW = b"new"


class ThumbnailObjects(Objects):
    def head_object(self, **params):
        if "VersionId" not in params:
            return super().head_object(**params)
        row = next(
            (
                row
                for row in self.rows.get(params["Key"], [])
                if row["version"] == params["VersionId"]
            ),
            None,
        )
        if row is None or row["marker"]:
            raise ClientError(
                {
                    "Error": {"Code": "NoSuchVersion"},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                },
                "HeadObject",
            )
        return {
            "ResponseMetadata": {"HTTPStatusCode": 200},
            "VersionId": row["version"],
            "ContentLength": len(row["data"]),
            "ETag": hashlib.sha256(row["data"]).hexdigest(),
        }


def thumbnail_record(*, data=RAW, version="v2"):
    identifier = uuid.uuid4()
    endpoint, bucket = media_cache_storage._runtime_endpoint(), "saved-thumbnails"
    ref = dict(
        schema=2,
        purpose="media-cache",
        location_id=str(LEGACY_LOCATION_ID),
        config_revision=1,
        binding_generation=1,
        endpoint_url=endpoint,
        bucket=bucket,
        prefix=outputs.prefix_for(identifier),
        key_prefix="",
        storage_identity=media_cache_storage.managed_storage_identity(endpoint, bucket),
    )
    return SimpleNamespace(
        id=identifier,
        storage_ref=ref,
        version_id=version,
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        etag=hashlib.sha256(data).hexdigest(),
    )


@pytest.mark.parametrize("case,delta", [("current", 9), ("older", 0), ("null", None)])
def test_thumbnail_actual_delete_keeps_condition_and_original_absence(case, delta):
    data, version = (
        (b"old contents", "v1")
        if case == "older"
        else (RAW, "null" if case == "null" else "v2")
    )
    output = thumbnail_record(data=data, version=None if case == "null" else version)
    client = ThumbnailObjects(output.storage_ref["endpoint_url"])
    key = outputs.owner(output).key(outputs.RELATIVE_KEY)
    if case != "null":
        client.seed("v1", b"old contents", key=key)
    client.seed("null" if case == "null" else "v2", RAW, key=key)
    recorder = MemoryRecorder()
    recorder.bucket = output.storage_ref["bucket"]
    options = []

    @contextmanager
    def opened(**kwargs):
        options.append(kwargs)
        try:
            yield client
        finally:
            client.close()

    assert (
        thumbnails.remove(SimpleNamespace(client=opened), output, accounting=recorder)
        == 1
    )
    assert options == [{"read_timeout": 30, "owned": True}]
    assert client.calls == [
        {
            "Bucket": recorder.bucket,
            "Key": key,
            "VersionId": version,
            "IfMatch": output.etag,
        }
    ]
    assert client.gets[-1] == {
        "Bucket": recorder.bucket,
        "Key": key,
        "VersionId": version,
    }
    assert client.closed == 1 and all(body.closed for body in client.bodies)
    (fact,) = recorder.attempts
    assert fact.evidence.get("current_delta_bytes") == delta


@pytest.mark.parametrize("fault", ["missing", "hash", "intent"])
def test_thumbnail_predelete_failures_do_not_submit_or_mark_unsettled(fault):
    output = thumbnail_record()
    client = ThumbnailObjects(output.storage_ref["endpoint_url"])
    key = outputs.owner(output).key(outputs.RELATIVE_KEY)
    if fault != "missing":
        client.seed("v2", RAW, key=key)
    recorder = MemoryRecorder()
    recorder.bucket = output.storage_ref["bucket"]
    if fault == "hash":
        output.sha256 = "0" * 64
    if fault == "intent":

        def failed(*args):
            raise OSError("intent_not_durable")

        recorder.begin = failed

    @contextmanager
    def opened(**kwargs):
        try:
            yield client
        finally:
            client.close()

    if fault == "missing":
        assert (
            thumbnails.remove(
                SimpleNamespace(client=opened), output, accounting=recorder
            )
            == 0
        )
    else:
        with pytest.raises((ContentError, OSError)) as error:
            thumbnails.remove(
                SimpleNamespace(client=opened), output, accounting=recorder
            )
        assert not getattr(error.value, "storage_operation_unsettled", False)
    assert not recorder.attempts and not client.calls and client.closed == 1


@pytest.fixture
async def owners_db(media_db):
    connection = await media_db.connection()
    for table in (
        AvatarUpload.__table__,
        BugAttachmentUpload.__table__,
        Mutation.__table__,
    ):
        await connection.run_sync(table.create)
    await media_db.commit()
    return media_db


def test_thumbnail_failed_absence_keeps_attempt_unknown():
    output = thumbnail_record()
    client = ThumbnailObjects(output.storage_ref["endpoint_url"])
    key = outputs.owner(output).key(outputs.RELATIVE_KEY)
    client.seed("v2", RAW, key=key)
    recorder = MemoryRecorder()
    recorder.bucket = output.storage_ref["bucket"]
    original_get = client.get_object

    def reappeared(**params):
        if client.calls:
            client.seed("v2", RAW, key=key)
        return original_get(**params)

    client.get_object = reappeared

    @contextmanager
    def opened(**kwargs):
        try:
            yield client
        finally:
            client.close()

    with pytest.raises(ContentError, match="thumbnail_cleanup_unproven") as error:
        thumbnails.remove(SimpleNamespace(client=opened), output, accounting=recorder)
    assert error.value.storage_operation_unsettled and client.closed == 1
    (attempt,) = recorder.attempts
    assert attempt.evidence["physical_outcome"] == "unknown"
    assert "current_delta_bytes" not in attempt.evidence
    assert all(body.closed for body in client.bodies)


async def allocate_case(db, kind, *, raw=RAW, version="v2", persist=True):
    now, identifier = outputs.now(), uuid.uuid4()
    if kind == "thumbnail":
        ref = await media_cache_storage.allocate(db, outputs.prefix_for(identifier))
        row = ThumbnailOutput(
            id=identifier,
            owner_id=uuid.uuid4(),
            owner_kind="dataset_thumbnail",
            input_ref={
                "source": {},
                "encoded": {
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "size_bytes": len(raw),
                },
            },
            storage_ref=ref,
            state="retiring",
            sha256=hashlib.sha256(raw).hexdigest(),
            size_bytes=len(raw),
            etag=hashlib.sha256(raw).hexdigest(),
            version_id=version,
            writer_until=now - timedelta(hours=3),
            ready_at=now - timedelta(hours=3),
            retired_at=now - timedelta(hours=2),
            next_check_at=now - timedelta(hours=1),
            created_at=now - timedelta(hours=4),
            updated_at=now - timedelta(hours=2),
        )
        key = outputs.owner(row).key(outputs.RELATIVE_KEY)
    elif kind == "avatar":
        row = AvatarUpload(
            token=identifier.hex,
            owner_id=uuid.uuid4(),
            storage_ref=await avatar_storage.allocate(db, identifier.hex),
            sha256=hashlib.sha256(raw).hexdigest(),
            size_bytes=len(raw),
            object_version_id=version,
            state="retired",
            retired_at=now,
        )
        key = row.storage_ref["object_key"]
    else:
        row = BugAttachmentUpload(
            id=identifier,
            owner_id=uuid.uuid4(),
            sha256=hashlib.sha256(raw).hexdigest(),
            size_bytes=len(raw),
            object_version_id=version,
            state="retired",
            retired_at=now,
            mime_type="image/png",
            file_name="evidence.png",
            created_at=now - timedelta(days=1),
            expires_at=now + timedelta(days=1),
        )
        row.storage_ref = await bug_storage.allocate(db, row)
        key = row.storage_ref["object_key"]
    if persist:
        db.add(row)
    await db.commit()
    return SimpleNamespace(
        row=row,
        id=identifier,
        key=key,
        kind=kind,
        producer=kind + "_removals",
        db=db,
        ref=dict(row.storage_ref),
    )


async def execute(case):
    if case.kind == "thumbnail":
        return await thumbnails.reconcile(case.db)
    if case.kind == "avatar":
        return await avatar_uploads._cleanup_one(case.db, case.id.hex)
    return await bug_uploads._cleanup_one(case.db, case.id)


async def facts(case):
    async with async_sessionmaker(case.db.bind, expire_on_commit=False)() as db:
        return list(
            await db.scalars(
                select(Mutation)
                .where(Mutation.owner_id == case.id)
                .order_by(Mutation.started_at)
            )
        )


async def retained(case):
    async with async_sessionmaker(case.db.bind, expire_on_commit=False)() as db:
        row = await db.get(
            type(case.row), case.id.hex if case.kind == "avatar" else case.id
        )
        return row is not None and row.state != "deleted"


async def install_case(db, kind, monkeypatch):
    case = await allocate_case(db, kind)
    client = ThumbnailObjects(case.ref["endpoint_url"])
    client.seed("v1", b"old contents", key=case.key)
    client.seed("v2", RAW, key=case.key)
    case.client = client
    loop = asyncio.get_running_loop()
    factory = async_sessionmaker(db.bind, expire_on_commit=False)

    async def durable(params):
        async with factory() as check:
            fact = await check.scalar(
                select(Mutation).where(Mutation.owner_id == case.id)
            )
            assert (
                fact is not None and fact.state == "pending" and fact.kind == "delete"
            )
            assert fact.producer == case.producer and fact.expected_version == "v2"
            assert fact.purpose == (
                "media_cache" if kind == "thumbnail" else "other_platform"
            )
            assert (
                await check.scalar(select(func.count()).select_from(StorageOperation))
                == 1
            )

    def parameters(params, **kwargs):
        assert params["Bucket"] == case.ref["bucket"] and params["Key"] == case.key
        asyncio.run_coroutine_threadsafe(durable(params), loop).result(timeout=5)

    client.meta.events.register("before-parameter-build.s3.DeleteObject", parameters)

    def owned(**kwargs):
        return SimpleNamespace(client=client)

    monkeypatch.setattr(storage, "StorageService", owned)
    original = usage.publish

    async def publish(*args, **kwargs):
        assert client.closed == 1 and all(body.closed for body in client.bodies)
        if kwargs["settled"]:
            async with factory() as check:
                assert (
                    await check.scalar(
                        select(func.count()).select_from(StorageOperation)
                    )
                    == 0
                )
        return await original(*args, **kwargs)

    monkeypatch.setattr(usage, "publish", publish)
    return case


@pytest.mark.parametrize("kind", KINDS)
async def test_actual_owner_confirms_only_after_client_and_real_permit(
    owners_db, monkeypatch, kind
):
    case = await install_case(owners_db, kind, monkeypatch)
    assert await execute(case) == 1
    (fact,) = await facts(case)
    assert (fact.state, fact.physical_outcome, fact.current_delta_bytes) == (
        "confirmed",
        "deleted",
        9,
    )
    assert not await retained(case)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("resource", ["client", "permit"])
async def test_owner_unknown_close_keeps_evidence_and_business_receipt(
    owners_db, monkeypatch, kind, resource
):
    case = await install_case(owners_db, kind, monkeypatch)
    if resource == "client":
        case.client.close_failure = OSError("client_close_failed")
    else:

        async def unsettled(permit):
            raise OSError("permit_close_failed")

        monkeypatch.setattr(storage_access.Permit, "_settle", unsettled)
    if kind == "thumbnail" and resource == "permit":
        assert await execute(case) == 0
    else:
        with pytest.raises(Exception):
            await execute(case)
    (fact,) = await facts(case)
    assert (fact.state, fact.physical_outcome, fact.current_delta_bytes) == (
        "unknown",
        "deleted",
        None,
    )
    assert await retained(case)
    assert (
        await owners_db.scalar(select(func.count()).select_from(StorageOperation)) == 1
    )


@pytest.mark.parametrize("kind", KINDS)
async def test_postphysical_business_guard_preserves_confirmed_fact(
    owners_db, monkeypatch, kind
):
    case = await install_case(owners_db, kind, monkeypatch)
    module, name = (
        (thumbnails, "eligible")
        if kind == "thumbnail"
        else (avatar_uploads, "_has_reference")
        if kind == "avatar"
        else (bug_uploads, "_has_read")
    )
    original = getattr(module, name)

    async def guarded(*args, **kwargs):
        if case.client.closed:
            return kind != "thumbnail"
        return await original(*args, **kwargs)

    monkeypatch.setattr(module, name, guarded)
    assert await execute(case) == 0
    (fact,) = await facts(case)
    assert fact.state == "confirmed" and fact.current_delta_bytes == 9
    assert await retained(case)


@pytest.mark.parametrize("kind", KINDS)
async def test_ordinary_publication_failure_keeps_owner_business_success(
    owners_db, monkeypatch, kind
):
    case = await install_case(owners_db, kind, monkeypatch)

    async def unavailable(*args, **kwargs):
        assert case.client.closed == 1
        raise OSError("ledger_unavailable")

    monkeypatch.setattr(usage, "publish", unavailable)
    assert await execute(case) == 1
    (fact,) = await facts(case)
    assert fact.state == "pending" and fact.current_delta_bytes is None
    assert not await retained(case)


@pytest.mark.parametrize("kind", KINDS)
async def test_fresh_publication_cancellation_drains_then_propagates(
    owners_db, monkeypatch, kind
):
    case = await install_case(owners_db, kind, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    original = usage.publish

    async def blocked(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(usage, "publish", blocked)
    pending = asyncio.create_task(execute(case))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
    (fact,) = await facts(case)
    assert fact.state == "confirmed" and fact.current_delta_bytes == 9
    assert await retained(case)


@pytest.mark.parametrize("kind", KINDS)
async def test_native_cancellation_drains_before_client_permit_and_ledger(
    owners_db, monkeypatch, kind
):
    case = await install_case(owners_db, kind, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    original = case.client.delete_object

    def blocked(**params):
        entered.set()
        assert release.wait(5)
        return original(**params)

    case.client.delete_object = blocked
    pending = asyncio.create_task(execute(case))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done() and case.client.closed == 0
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
    (fact,) = await facts(case)
    assert fact.state == "confirmed" and fact.current_delta_bytes == 9
    assert case.client.closed == 1 and await retained(case)


@pytest.mark.parametrize("failure", ["first_delete", "second_admission"])
async def test_thumbnail_batch_keeps_candidate_recorder_and_permit_separate(
    owners_db, monkeypatch, failure
):
    first = await allocate_case(owners_db, "thumbnail")
    second = await allocate_case(owners_db, "thumbnail")
    client = ThumbnailObjects(first.ref["endpoint_url"])
    for case in (first, second):
        client.seed("v1", b"old contents", key=case.key)
        client.seed("v2", RAW, key=case.key)
    monkeypatch.setattr(
        storage, "StorageService", lambda **kwargs: SimpleNamespace(client=client)
    )
    finished = []
    original_finish = removal.finish

    async def finish(recorder, original, *, settled=True):
        finished.append((recorder.owner_id, settled))
        return await original_finish(recorder, original, settled=settled)

    monkeypatch.setattr(removal, "finish", finish)
    if failure == "first_delete":
        original_delete = client.delete_object

        def refused(**params):
            if params["Key"] == first.key:
                raise ClientError(
                    {
                        "Error": {"Code": "AccessDenied"},
                        "ResponseMetadata": {"HTTPStatusCode": 403},
                    },
                    "DeleteObject",
                )
            return original_delete(**params)

        client.delete_object = refused
    else:
        original_acquire = storage_access.acquire

        async def acquire(db, location, purpose, **kwargs):
            if purpose == outputs.purpose(second.id):
                raise ContentError("storage_maintenance")
            return await original_acquire(db, location, purpose, **kwargs)

        monkeypatch.setattr(storage_access, "acquire", acquire)
    assert await thumbnails.reconcile(owners_db) == 1
    assert finished == [(first.id, True), (second.id, failure == "first_delete")]
    first_facts, second_facts = await facts(first), await facts(second)
    if failure == "first_delete":
        assert len(first_facts) == len(second_facts) == 1
        assert (
            first_facts[0].state == "unknown"
            and first_facts[0].current_delta_bytes is None
        )
        assert (
            second_facts[0].state == "confirmed"
            and second_facts[0].current_delta_bytes == 9
        )
        assert await retained(first) and not await retained(second)
    else:
        assert len(first_facts) == 1 and not second_facts
        assert first_facts[0].state == "confirmed" and not await retained(first)
        assert await retained(second)


@pytest.mark.parametrize("kind", KINDS)
async def test_real_minio_cleanup_owner_preserves_current_fallback_net(
    owners_db, monkeypatch, kind
):
    case = await allocate_case(owners_db, kind, persist=False)
    seed = storage.StorageService().client
    original_create, original_publish = storage.StorageService, usage.publish
    loop = asyncio.get_running_loop()
    factory = async_sessionmaker(owners_db.bind, expire_on_commit=False)
    closed = []
    versions = set()

    async def durable(params):
        async with factory() as check:
            fact = await check.scalar(
                select(Mutation).where(Mutation.owner_id == case.id)
            )
            assert fact is not None and fact.state == "pending"
            assert (
                fact.expected_version == params["VersionId"]
                and fact.producer == case.producer
            )
            assert (
                await check.scalar(select(func.count()).select_from(StorageOperation))
                == 1
            )

    def owned(**kwargs):
        result = original_create(**kwargs)
        original_close = result.client.close

        def close():
            original_close()
            closed.append(True)

        def parameters(params, **kwargs):
            asyncio.run_coroutine_threadsafe(durable(params), loop).result(timeout=5)

        result.client.close = close
        result.client.meta.events.register(
            "before-parameter-build.s3.DeleteObject", parameters
        )
        return result

    async def publish(*args, **kwargs):
        assert closed == [True] and kwargs["settled"] is True
        async with factory() as check:
            assert (
                await check.scalar(select(func.count()).select_from(StorageOperation))
                == 0
            )
        return await original_publish(*args, **kwargs)

    try:
        seed.put_bucket_versioning(
            Bucket=case.ref["bucket"], VersioningConfiguration={"Status": "Enabled"}
        )
        for raw in (b"old contents", RAW):
            current = seed.put_object(
                Bucket=case.ref["bucket"],
                Key=case.key,
                Body=raw,
                ContentType="image/webp" if kind != "bug_attachment" else "image/png",
            )
            versions.add(current["VersionId"])
        assert len(versions) == 2 and "null" not in versions
        if kind == "thumbnail":
            case.row.version_id, case.row.etag = current["VersionId"], current["ETag"]
        else:
            case.row.object_version_id = current["VersionId"]
        owners_db.add(case.row)
        await owners_db.commit()
        monkeypatch.setattr(storage, "StorageService", owned)
        monkeypatch.setattr(usage, "publish", publish)
        assert await execute(case) == 1
        (fact,) = await facts(case)
        assert (fact.state, fact.physical_outcome, fact.current_delta_bytes) == (
            "confirmed",
            "deleted",
            9,
        )
        assert (
            fact.expected_version == current["VersionId"]
            and fact.evidence["final_send_count"] == 1
        )
        assert not await retained(case)
        head = seed.head_object(Bucket=case.ref["bucket"], Key=case.key)
        assert head["ContentLength"] == 12 and head["VersionId"] != current["VersionId"]
    finally:
        try:
            remaining = seed.list_object_versions(
                Bucket=case.ref["bucket"], Prefix=case.key
            )
            for item in remaining.get("Versions", []) + remaining.get(
                "DeleteMarkers", []
            ):
                assert item["Key"] == case.key
                seed.delete_object(
                    Bucket=case.ref["bucket"], Key=case.key, VersionId=item["VersionId"]
                )
        finally:
            seed.close()
