"""Actual cleanup owners settle marker observations after native resources."""

import asyncio
from datetime import timedelta
import hashlib
from types import SimpleNamespace
import threading
import uuid

from botocore.config import Config
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.export_media_bundle import ExportMediaBundle
from app.db.models.export_output import ExportOutput
from app.db.models.image_pyramid import ImagePyramidOutput
from app.db.models.storage_maintenance import StorageOperation
from app.db.models.video_derivative import VideoDerivativeOutput
from app.services import (
    export_media_retirement,
    export_media_storage,
    export_outputs,
    export_storage,
)
from app.services import image_pyramid_objects as pyramids, image_pyramid_storage
from app.services import key_removal_accounting as removal
from app.services import storage, storage_access, storage_usage_accounting as usage
from app.services import video_derivative_objects as videos, video_derivative_storage
from app.services.content_snapshot import ContentError
from app.db.models.storage_usage import StorageUsageMutation as Mutation
from tests import test_export_media_bundle_models as bundle_models
from tests.test_image_pyramid_storage import reference as pyramid_ref
from tests.test_key_removal_accounting import MarkerProvider
from tests.test_media_cache_targets import media_db as media_db
from tests.test_video_derivative_storage import reference as video_ref

KINDS = ("pyramid", "video", "zip", "zip_linked")
RAW = b"new"


@pytest.fixture
async def owners_db(media_db):
    for table in (
        "image_pyramid_assets",
        "image_pyramid_generations",
        "video_frame_cache",
        "video_chunks",
        "export_artifacts",
        "export_outputs",
        "export_media_bundles",
        "async_jobs",
        "storage_usage_mutations",
    ):
        await media_db.execute(
            text(f"CREATE TABLE {table} (LIKE public.{table} INCLUDING ALL)")
        )
    await media_db.commit()
    return media_db


class Objects(MarkerProvider):
    def __init__(self, endpoint):
        super().__init__(endpoint)
        self.meta.config = Config(retries={"max_attempts": 3})
        self.closed, self.close_failure = 0, None

    def close(self):
        self.closed += 1
        if self.close_failure is not None:
            raise self.close_failure


async def allocate_case(db, kind, *, count=1):
    identifier, old = uuid.uuid4(), export_outputs.now() - timedelta(days=10)
    bucket = storage.storage_service.media_cache_bucket
    bundle = None
    if kind == "pyramid":
        row = ImagePyramidOutput(
            id=identifier,
            asset_id=uuid.uuid4(),
            generation=1,
            lease_token=uuid.uuid4(),
            storage_ref=pyramid_ref(identifier, bucket=bucket),
            state="retiring",
            created_at=old,
            updated_at=old,
        )
        key = image_pyramid_storage.resolve(row).key("overview.webp")
        extra = image_pyramid_storage.resolve(row).key("manifest.json")
        producer, purpose = "image_pyramid_removals", "media_cache"
    elif kind == "video":
        row = VideoDerivativeOutput(
            id=identifier,
            dataset_item_id=uuid.uuid4(),
            kind="metadata",
            spec={},
            original_ref={},
            auth_ref={},
            storage_ref=video_ref(identifier, bucket=bucket),
            state="retiring",
            created_at=old,
            updated_at=old,
        )
        key = video_derivative_storage.resolve(row).key("poster.webp")
        extra = video_derivative_storage.resolve(row).key("metadata.json")
        producer, purpose = "video_derivative_removals", "media_cache"
    else:
        row = ExportOutput(
            id=identifier,
            project_id=uuid.uuid4(),
            job_id=uuid.uuid4(),
            media="image",
            format="coco",
            file_count=1,
            sha256=hashlib.sha256(RAW).hexdigest(),
            size_bytes=len(RAW),
            state="retired",
            created_at=old,
            expires_at=old + timedelta(days=7),
            retired_at=export_outputs.now(),
        )
        row.storage_ref = await export_storage.allocate(db, row)
        key = row.storage_ref["object_key"]
        extra = None
        producer, purpose = "export_zip_removals", "temporary"
        if kind == "zip_linked":
            values = bundle_models._values(
                project_id=row.project_id,
                job_id=row.job_id,
                zip_output_id=row.id,
                created_at=row.created_at,
                expires_at=row.expires_at,
            )
            values.update(
                object_count=0,
                size_bytes=0,
                inventory={"schema": 1, "objects": {}},
                logical_paths={"schema": 1, "targets": {"pointmask": {}}},
            )
            values["storage_ref"] = export_media_storage.make_ref(
                values["id"], bucket=row.storage_ref["bucket"]
            )
            bundle = ExportMediaBundle(**values)
            db.add(bundle)
            row.media_bundle_id = bundle.id
    db.add(row)
    await db.commit()
    return SimpleNamespace(
        db=db,
        kind=kind,
        row=row,
        id=identifier,
        bundle=bundle,
        keys=[key] + ([extra] if count == 2 else []),
        ref=dict(row.storage_ref),
        producer=producer,
        purpose=purpose,
    )


async def execute(case):
    if case.kind == "pyramid":
        return await pyramids.reconcile(case.db, None)
    if case.kind == "video":
        return await videos.reconcile(case.db)
    return await export_outputs._cleanup_one(case.db, case.id, {"removed": 0})


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
        row = await db.get(type(case.row), case.id)
        return row is not None and row.state != "deleted"


async def install(db, kind, monkeypatch, *, count=1):
    case = await allocate_case(db, kind, count=count)
    client = Objects(case.ref["endpoint_url"])
    for key in case.keys:
        client.seed("old-version", RAW, key=key)
    case.client = client
    loop = asyncio.get_running_loop()
    factory = async_sessionmaker(db.bind, expire_on_commit=False)

    async def durable(params):
        async with factory() as check:
            fact = await check.scalar(
                select(Mutation).where(
                    Mutation.owner_id == case.id, Mutation.object_key == params["Key"]
                )
            )
            assert (
                fact is not None
                and fact.state == "pending"
                and fact.expected_version is None
            )
            assert fact.producer == case.producer and fact.purpose == case.purpose
            assert (
                await check.scalar(select(func.count()).select_from(StorageOperation))
                == 1
            )

    def parameters(params, **kwargs):
        assert params["Bucket"] == case.ref["bucket"] and "VersionId" not in params
        asyncio.run_coroutine_threadsafe(durable(params), loop).result(timeout=5)

    client.meta.events.register("before-parameter-build.s3.DeleteObject", parameters)

    def owned(**kwargs):
        return SimpleNamespace(client=client)

    monkeypatch.setattr(storage, "StorageService", owned)
    monkeypatch.setattr(video_derivative_storage, "StorageService", owned)
    original = usage.publish

    async def publish(*args, **kwargs):
        assert client.closed == 1
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
async def test_cleanup_true_owner_confirms_marker_after_client_and_permit(
    owners_db, monkeypatch, kind
):
    case = await install(owners_db, kind, monkeypatch)
    assert await execute(case) == 1
    (fact,) = await facts(case)
    assert (fact.state, fact.physical_outcome, fact.current_delta_bytes) == (
        "confirmed",
        "unknown",
        -3,
    )
    assert fact.evidence["delete_marker_created"] and not await retained(case)
    if case.bundle is not None:
        assert fact.owner_id != case.bundle.id


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("resource", ("client", "permit"))
async def test_unknown_close_keeps_marker_fact_unconfirmed_and_receipt(
    owners_db, monkeypatch, kind, resource
):
    case = await install(owners_db, kind, monkeypatch)
    if resource == "client":
        case.client.close_failure = OSError("client_close_failed")
    else:

        async def failed(permit):
            raise OSError("permit_close_failed")

        monkeypatch.setattr(storage_access.Permit, "_settle", failed)
    try:
        result = await execute(case)
    except Exception:
        pass
    else:
        assert result == 0
    (fact,) = await facts(case)
    assert fact.state == "unknown" and fact.current_delta_bytes is None
    assert fact.physical_outcome == "unknown" and fact.evidence["delete_marker_created"]
    assert await retained(case)
    assert (
        await owners_db.scalar(select(func.count()).select_from(StorageOperation)) == 1
    )


@pytest.mark.parametrize("kind", KINDS)
async def test_later_guard_or_business_rejection_keeps_confirmed_marker(
    owners_db, monkeypatch, kind
):
    case = await install(owners_db, kind, monkeypatch)
    original = owners_db.scalar
    model = type(case.row)

    async def guarded(statement, *args, **kwargs):
        if case.client.closed and any(
            d.get("entity") is model for d in statement.column_descriptions
        ):
            return None
        return await original(statement, *args, **kwargs)

    monkeypatch.setattr(owners_db, "scalar", guarded)
    assert await execute(case) == 0
    (fact,) = await facts(case)
    assert fact.state == "confirmed" and fact.current_delta_bytes == -3
    assert await retained(case)


@pytest.mark.parametrize("kind", KINDS)
async def test_ordinary_publication_failure_preserves_business_success(
    owners_db, monkeypatch, kind
):
    case = await install(owners_db, kind, monkeypatch)

    async def failed(*args, **kwargs):
        assert case.client.closed == 1
        raise OSError("publication_unavailable")

    monkeypatch.setattr(usage, "publish", failed)
    assert await execute(case) == 1
    (fact,) = await facts(case)
    assert fact.state == "pending" and fact.current_delta_bytes is None
    assert not await retained(case)


@pytest.mark.parametrize("kind", KINDS)
async def test_new_publication_cancel_drains_and_propagates(
    owners_db, monkeypatch, kind
):
    case = await install(owners_db, kind, monkeypatch)
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
    assert fact.state == "confirmed" and fact.current_delta_bytes == -3
    assert await retained(case)


@pytest.mark.parametrize("kind", KINDS)
async def test_native_cancel_drains_before_close_permit_and_publication(
    owners_db, monkeypatch, kind
):
    case = await install(owners_db, kind, monkeypatch)
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
    assert fact.state == "confirmed" and fact.current_delta_bytes == -3
    assert case.client.closed == 1 and await retained(case)


@pytest.mark.parametrize("kind", ("pyramid", "video"))
async def test_partial_prefix_delete_keeps_first_marker_fact(
    owners_db, monkeypatch, kind
):
    case = await install(owners_db, kind, monkeypatch, count=2)
    original = case.client.delete_object

    def lost_ack(**params):
        case.client.lose_ack = len(case.client.calls) == 1
        return original(**params)

    case.client.delete_object = lost_ack
    assert await execute(case) == 0
    first, second = await facts(case)
    assert first.state == "confirmed" and first.current_delta_bytes == -3
    assert second.state == "unknown" and second.current_delta_bytes is None
    assert await retained(case)


@pytest.mark.parametrize("kind", KINDS)
async def test_real_minio_production_cleanup_owner_confirms_current_decrease(
    owners_db, monkeypatch, kind
):
    case = await allocate_case(owners_db, kind)
    seed = storage.StorageService().client
    original_create, original_publish = storage.StorageService, usage.publish
    loop = asyncio.get_running_loop()
    factory = async_sessionmaker(owners_db.bind, expire_on_commit=False)
    closed = []

    async def durable(params):
        async with factory() as check:
            fact = await check.scalar(
                select(Mutation).where(Mutation.owner_id == case.id)
            )
            assert (
                fact is not None
                and fact.state == "pending"
                and fact.producer == case.producer
            )
            assert "VersionId" not in params and fact.expected_version is None
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

    key = case.keys[0]
    try:
        seed.put_bucket_versioning(
            Bucket=case.ref["bucket"], VersioningConfiguration={"Status": "Enabled"}
        )
        version = seed.put_object(Bucket=case.ref["bucket"], Key=key, Body=RAW)[
            "VersionId"
        ]
        assert version and version != "null"
        monkeypatch.setattr(storage, "StorageService", owned)
        monkeypatch.setattr(video_derivative_storage, "StorageService", owned)
        monkeypatch.setattr(usage, "publish", publish)
        assert await execute(case) == 1
        (fact,) = await facts(case)
        assert (fact.state, fact.physical_outcome, fact.current_delta_bytes) == (
            "confirmed",
            "unknown",
            -3,
        )
        assert (
            fact.evidence["delete_marker_created"]
            and fact.evidence["final_send_count"] == 1
        )
        assert not await retained(case)
        actual = seed.list_object_versions(Bucket=case.ref["bucket"], Prefix=key)
        assert [x["VersionId"] for x in actual.get("Versions", [])] == [version]
        assert len(actual.get("DeleteMarkers", [])) == 1
        assert actual["DeleteMarkers"][0]["IsLatest"] is True
    finally:
        try:
            actual = seed.list_object_versions(Bucket=case.ref["bucket"], Prefix=key)
            for item in actual.get("Versions", []) + actual.get("DeleteMarkers", []):
                assert item["Key"] == key
                seed.delete_object(
                    Bucket=case.ref["bucket"], Key=key, VersionId=item["VersionId"]
                )
        finally:
            seed.close()


@pytest.mark.parametrize("kind", ("pyramid", "video"))
async def test_actual_permit_is_captured_before_original_guard_rejects(
    owners_db, monkeypatch, kind
):
    case = await install(owners_db, kind, monkeypatch)
    original_acquire, original_finish = storage_access.acquire, removal.finish
    factory = async_sessionmaker(owners_db.bind, expire_on_commit=False)
    settled = []

    async def raced(db, location, purpose, **kwargs):
        permit = await original_acquire(db, location, purpose, **kwargs)
        async with factory() as owned:
            owned.add(
                StorageOperation(
                    id=uuid.uuid4(),
                    location_id=location,
                    revision=permit.revision,
                    purpose=purpose,
                    node_id="b13:test",
                )
            )
            await owned.commit()
        return permit

    async def finish(recorder, failure, *, settled=False):
        assert recorder.owner_id == case.id
        assert failure is not None
        observed.append(settled)
        return await original_finish(recorder, failure, settled=settled)

    observed = settled
    monkeypatch.setattr(storage_access, "acquire", raced)
    monkeypatch.setattr(removal, "finish", finish)
    assert await execute(case) == 0
    assert settled == [True] and not await facts(case)
    assert not case.client.calls and case.client.closed == 0 and await retained(case)
    assert (
        await owners_db.scalar(select(func.count()).select_from(StorageOperation)) == 1
    )


@pytest.mark.parametrize("kind", ("pyramid", "video"))
async def test_batch_does_not_borrow_previous_candidate_settled_permit(
    owners_db, monkeypatch, kind
):
    first = await allocate_case(owners_db, kind)
    second = await allocate_case(owners_db, kind)
    client = Objects(first.ref["endpoint_url"])
    for case in (first, second):
        client.seed("old-version", RAW, key=case.keys[0])
    monkeypatch.setattr(
        storage, "StorageService", lambda **kwargs: SimpleNamespace(client=client)
    )
    monkeypatch.setattr(
        video_derivative_storage,
        "StorageService",
        lambda **kwargs: SimpleNamespace(client=client),
    )
    original_acquire, original_finish = storage_access.acquire, removal.finish
    finished = []
    prefix = "image_pyramid_" if kind == "pyramid" else "video_derivative_"

    async def denied(db, location, purpose, **kwargs):
        if purpose == prefix + second.id.hex:
            from app.services.content_snapshot import ContentError

            raise ContentError("storage_maintenance")
        return await original_acquire(db, location, purpose, **kwargs)

    async def finish(recorder, failure, *, settled=False):
        finished.append((recorder.owner_id, settled))
        return await original_finish(recorder, failure, settled=settled)

    monkeypatch.setattr(storage_access, "acquire", denied)
    monkeypatch.setattr(removal, "finish", finish)
    assert await execute(first) == 1
    assert finished == [(first.id, True), (second.id, False)]
    assert len(await facts(first)) == 1 and not await facts(second)
    assert not await retained(first) and await retained(second)


async def test_linked_zip_new_settlement_cancellation_replaces_prior_business_failure(
    owners_db, monkeypatch
):
    case = await install(owners_db, "zip_linked", monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    original_guard, original_settle = (
        export_media_retirement._guard,
        storage_access.Permit._settle,
    )

    async def guard(*args, **kwargs):
        await original_guard(*args, **kwargs)
        if case.client.closed:
            raise ContentError("business_changed_after_delete")

    async def settle(permit):
        entered.set()
        await release.wait()
        await original_settle(permit)

    monkeypatch.setattr(export_media_retirement, "_guard", guard)
    monkeypatch.setattr(storage_access.Permit, "_settle", settle)
    pending = asyncio.create_task(execute(case))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done() and case.client.closed == 1
        assert (
            await owners_db.scalar(select(func.count()).select_from(StorageOperation))
            == 1
        )
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
    (fact,) = await facts(case)
    assert fact.state == "confirmed" and fact.current_delta_bytes == -3
    assert await retained(case)
    assert (
        await owners_db.scalar(select(func.count()).select_from(StorageOperation)) == 0
    )
