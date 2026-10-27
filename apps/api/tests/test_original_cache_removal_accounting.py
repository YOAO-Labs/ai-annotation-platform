"""Production cache purge owns each key-only window until native/permit close."""

import asyncio
from contextlib import contextmanager
from datetime import timedelta
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import uuid

import pytest
from sqlalchemy import delete, event, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models.original_cache import (
    OriginalCacheEntry as Entry,
    OriginalCacheReadLease as ReadLease,
)
from app.db.models.storage_maintenance import StorageOperation
from app.db.models.storage_usage import StorageUsageMutation as Mutation
from app.services import original_cache as cache
from app.services import storage_access
from app.services import storage_usage_accounting as usage
from app.services.managed_original_retirement import managed_storage_identity
from tests.test_key_removal_accounting import MarkerProvider
from tests.test_original_cache import cache_world as cache_world

REAL_STORAGE = cache.make_storage


class CacheProvider(MarkerProvider):
    def __init__(self, world):
        super().__init__(world.endpoint)
        self.client, self.media_cache_bucket = self, world.bucket
        self.closed, self.close_failure = False, None
        self.started = self.release = None

    def delete_object(self, **params):
        if self.started is not None:
            self.started.set()
            assert self.release.wait(5)
        return super().delete_object(**params)

    def close(self):
        self.closed = True
        if self.close_failure:
            raise self.close_failure


@pytest.fixture
async def removal_world(cache_world, monkeypatch):
    world = cache_world
    storage = cache.make_storage()
    try:
        entry, status = await cache.reserve(
            world.factory, world.source_id, world.asset_id, storage
        )
    finally:
        storage.client.close()
    assert status == "reserved"
    async with world.factory() as db:
        current = await db.get(Entry, entry.id)
        current.state, current.uncertain_write = "deleting", False
        current.next_check_at = cache.now() - timedelta(seconds=1)
        await db.commit()
    world.entry, world.key = entry, cache.object_key(entry)
    world.provider = CacheProvider(world)
    world.provider.seed("old-version", world.data, key=world.key)
    world.permits = []
    acquire = storage_access.acquire

    async def acquired(*args, **kwargs):
        permit = await acquire(*args, **kwargs)
        world.permits.append(permit)
        return permit

    monkeypatch.setattr(storage_access, "acquire", acquired)
    monkeypatch.setattr(cache, "make_storage", lambda: world.provider)
    try:
        yield world
    finally:
        if world.provider.release is not None:
            world.provider.release.set()
        async with world.factory() as db:
            await db.execute(delete(Mutation).where(Mutation.owner_id == entry.id))
            if world.permits:
                await db.execute(
                    delete(StorageOperation).where(
                        StorageOperation.id.in_([permit.id for permit in world.permits])
                    )
                )
            await db.commit()


async def mutations(world):
    async with world.factory() as db:
        return list(
            await db.scalars(
                select(Mutation).where(Mutation.owner_id == world.entry.id)
            )
        )


async def current_entry(world):
    async with world.factory() as db:
        return await db.get(Entry, world.entry.id)


async def historical_entry(world, **overrides):
    # Saved addresses are immutable. Insert a historical row directly rather
    # than weakening the production UPDATE trigger to fabricate legacy data.
    async with world.factory() as db:
        entry = await db.get(Entry, world.entry.id)
        values = {
            column.name: getattr(entry, column.name)
            for column in Entry.__table__.columns
        }
        await db.delete(entry)
        await db.flush()
        entry = Entry(**{**values, "storage_ref": None, **overrides})
        db.add(entry)
        await db.commit()
        return entry


@pytest.mark.parametrize("before,delta", [("object", -1), ("marker", 0), ("absent", 0)])
async def test_purge_marker_delta_is_durable_and_published_after_actual_resources(
    removal_world, monkeypatch, before, delta
):
    world, published = removal_world, []
    provider = world.provider
    if before == "marker":
        provider.seed("old-marker", marker=True, key=world.key)
    elif before == "absent":
        provider.rows.clear()
    provider.seed("neighbor", b"keep", key=world.key + "/neighbor")
    loop = asyncio.get_running_loop()

    async def pending():
        rows = await mutations(world)
        assert len(rows) == 1 and rows[0].state == "pending"
        assert rows[0].expected_version is None
        assert rows[0].object_key == world.key
        assert await current_entry(world) is not None

    def before_send(**kwargs):
        assert not provider.closed and not world.permits[0]._settled
        asyncio.run_coroutine_threadsafe(pending(), loop).result(5)

    provider.meta.events.register("before-send.s3.DeleteObject", before_send)
    publish = usage.publish

    async def publishing(*args, **kwargs):
        assert provider.closed and world.permits[0]._settled
        assert await current_entry(world) is not None
        async with world.factory() as db:
            assert await db.get(StorageOperation, world.permits[0].id) is None
        published.append(kwargs["settled"])
        return await publish(*args, **kwargs)

    monkeypatch.setattr(usage, "publish", publishing)
    assert await cache.purge(world.factory, world.entry.id) == "removed"
    row = (await mutations(world))[0]
    assert row.state == "confirmed"
    assert row.current_delta_bytes == delta * len(world.data)
    assert row.producer == "original_cache_removals" and row.purpose == "media_cache"
    assert row.physical_outcome == "unknown"
    assert row.evidence["delete_marker_created"] is True
    assert published == [True]
    assert provider.calls == [{"Bucket": world.bucket, "Key": world.key}]
    assert provider.rows[world.key + "/neighbor"][0]["data"] == b"keep"


@pytest.mark.parametrize(
    "problem",
    ["null", "no_ack_version", "retry", "no_hooks", "list_denied", "lost_ack"],
)
async def test_purge_keeps_old_result_when_observation_cannot_prove_delta(
    removal_world, problem
):
    world, provider = removal_world, removal_world.provider
    if problem == "null":
        provider.rows[world.key][0]["version"] = "null"
        provider.marker = "null"
    elif problem == "no_ack_version":
        provider.ack_version = None
    elif problem == "retry":
        provider.sends = 2
    elif problem == "no_hooks":
        provider.hooks = False
    elif problem == "list_denied":
        provider.fail_list = True
    else:
        provider.lose_ack = True
    expected = "deferred" if problem == "lost_ack" else "removed"
    assert await cache.purge(world.factory, world.entry.id) == expected
    row = (await mutations(world))[0]
    assert row.state == "unknown" and row.current_delta_bytes is None
    assert row.physical_outcome == "unknown" and provider.closed


@pytest.mark.parametrize(
    "reason", ["absent", "future", "reader", "writer", "ready", "identity"]
)
async def test_early_return_creates_no_recorder_or_intent(
    removal_world, monkeypatch, reason
):
    world = removal_world
    if reason == "identity":
        await historical_entry(world, storage_identity="f" * 64)
    async with world.factory() as db:
        entry = await db.get(Entry, world.entry.id)
        if reason == "absent":
            await db.delete(entry)
        elif reason == "future":
            entry.next_check_at = cache.now() + timedelta(seconds=60)
        elif reason == "reader":
            db.add(
                ReadLease(
                    id=uuid.uuid4(),
                    entry_id=entry.id,
                    expires_at=cache.now() + timedelta(seconds=60),
                )
            )
        elif reason == "writer":
            entry.state, entry.writer_until = (
                "uploading",
                cache.now() + timedelta(seconds=60),
            )
        elif reason == "ready":
            entry.state = "ready"
        await db.commit()
    construct = Mock(side_effect=AssertionError("no physical delete"))
    monkeypatch.setattr(cache.removal, "Recorder", construct)
    assert await cache.purge(world.factory, world.entry.id) == (
        "deferred" if reason == "identity" else "skipped"
    )
    assert not construct.called and not world.provider.calls
    assert await mutations(world) == []


async def test_legacy_null_ref_uses_verified_saved_target_and_key(
    removal_world, monkeypatch
):
    world, constructed = removal_world, []
    entry = await historical_entry(world)
    key = cache.object_key(entry)
    world.provider.rows = {}
    world.provider.seed("legacy-version", world.data, key=key)
    recorder = cache.removal.Recorder

    def construct(factory, producer, identity, endpoint, bucket, **kwargs):
        constructed.append((factory, producer, identity, endpoint, bucket, kwargs))
        return recorder(factory, producer, identity, endpoint, bucket, **kwargs)

    monkeypatch.setattr(cache.removal, "Recorder", construct)
    assert await cache.purge(world.factory, world.entry.id) == "removed"
    assert constructed == [
        (
            world.factory,
            "original_cache_removals",
            world.entry.id,
            world.endpoint.rstrip("/"),
            world.bucket,
            {"purpose": "media_cache"},
        )
    ]
    row = (await mutations(world))[0]
    assert row.object_key == key and row.current_delta_bytes == -len(world.data)


async def test_intent_failure_prevents_delete_and_keeps_deferred_outcome(
    removal_world, monkeypatch
):
    world = removal_world
    monkeypatch.setattr(
        cache.removal.Recorder,
        "_begin",
        AsyncMock(side_effect=OSError("intent unavailable")),
    )
    assert await cache.purge(world.factory, world.entry.id) == "deferred"
    assert not world.provider.calls and world.provider.closed
    assert world.permits[0]._settled and await mutations(world) == []
    assert (await current_entry(world)).error_code == "storage_cleanup_failed"


@pytest.mark.parametrize("fault", ["close", "permit", "pending_permit"])
async def test_actual_unsettled_resources_prevent_confirmation(
    removal_world, monkeypatch, fault
):
    world = removal_world
    if fault == "close":
        world.provider.close_failure = OSError("client close unavailable")
    else:
        acquire = storage_access.acquire

        async def acquired(*args, **kwargs):
            permit = await acquire(*args, **kwargs)
            permit._settle = (
                AsyncMock(side_effect=OSError("permit close unavailable"))
                if fault == "permit"
                else AsyncMock()
            )
            return permit

        monkeypatch.setattr(storage_access, "acquire", acquired)
    assert await cache.purge(world.factory, world.entry.id) == (
        "removed" if fault == "pending_permit" else "deferred"
    )
    row = (await mutations(world))[0]
    assert row.state == "unknown" and row.current_delta_bytes is None
    assert world.provider.closed and not world.permits[0]._settled


@pytest.mark.parametrize("business_failure", [False, True])
async def test_ordinary_publication_failure_preserves_original_result(
    removal_world, monkeypatch, business_failure
):
    world = removal_world
    world.provider.lose_ack = business_failure
    monkeypatch.setattr(
        usage, "publish", AsyncMock(side_effect=OSError("ledger unavailable"))
    )
    assert await cache.purge(world.factory, world.entry.id) == (
        "deferred" if business_failure else "removed"
    )
    assert world.provider.closed and world.permits[0]._settled
    assert (await mutations(world))[0].state == "pending"


@pytest.mark.parametrize("business_failure", [False, True])
async def test_new_publication_cancel_propagates_after_real_settlement(
    removal_world, monkeypatch, business_failure
):
    world = removal_world
    world.provider.lose_ack = business_failure
    monkeypatch.setattr(
        cache.removal.Recorder,
        "finish",
        AsyncMock(side_effect=asyncio.CancelledError("new ledger cancellation")),
    )
    with pytest.raises(asyncio.CancelledError, match="new ledger cancellation"):
        await cache.purge(world.factory, world.entry.id)
    assert world.provider.closed and world.permits[0]._settled
    assert await current_entry(world) is not None


async def test_repeated_cancel_drains_real_native_before_client_permit_and_publish(
    removal_world,
):
    world = removal_world
    world.provider.started, world.provider.release = (
        threading.Event(),
        threading.Event(),
    )
    pending = asyncio.create_task(cache.purge(world.factory, world.entry.id))
    try:
        assert await asyncio.to_thread(world.provider.started.wait, 5)
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done() and not world.provider.closed
        assert not world.permits[0]._settled
    finally:
        world.provider.release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
    assert world.provider.closed and world.permits[0]._settled
    assert (await mutations(world))[0].current_delta_bytes == -len(world.data)
    assert await current_entry(world) is not None


async def test_retained_uncertain_owner_still_publishes_observed_fact(removal_world):
    world = removal_world
    async with world.factory() as db:
        entry = await db.get(Entry, world.entry.id)
        entry.uncertain_write, entry.retain_until = (
            True,
            cache.now() + timedelta(days=1),
        )
        await db.commit()
    assert await cache.purge(world.factory, world.entry.id) == "retained"
    assert (await mutations(world))[0].current_delta_bytes == -len(world.data)
    assert await current_entry(world) is not None


async def test_business_delete_failure_does_not_rollback_completed_physical_fact(
    removal_world,
):
    world = removal_world

    def refuse_business_delete(session, *args):
        if any(
            isinstance(row, Entry) and row.id == world.entry.id
            for row in session.deleted
        ):
            raise RuntimeError("business publication refused")

    event.listen(Session, "before_flush", refuse_business_delete)
    try:
        with pytest.raises(RuntimeError, match="business publication refused"):
            await cache.purge(world.factory, world.entry.id)
    finally:
        event.remove(Session, "before_flush", refuse_business_delete)
    assert (await mutations(world))[0].current_delta_bytes == -len(world.data)
    assert await current_entry(world) is not None


async def test_rejected_permit_never_submits_or_creates_intent(
    removal_world, monkeypatch
):
    world = removal_world
    monkeypatch.setattr(
        storage_access,
        "acquire",
        AsyncMock(side_effect=OSError("admission unavailable")),
    )
    finished = []
    finish = cache.removal.Recorder.finish

    async def finishing(self, *, settled):
        finished.append(settled)
        return await finish(self, settled=settled)

    monkeypatch.setattr(cache.removal.Recorder, "finish", finishing)
    assert await cache.purge(world.factory, world.entry.id) == "deferred"
    assert finished == [False] and not world.provider.calls
    assert await mutations(world) == []


async def test_repeated_cancellation_during_publication_drains_ledger(
    removal_world, monkeypatch
):
    world = removal_world
    entered, release = asyncio.Event(), asyncio.Event()
    publish = usage.publish

    async def publishing(*args, **kwargs):
        entered.set()
        await release.wait()
        return await publish(*args, **kwargs)

    monkeypatch.setattr(usage, "publish", publishing)
    pending = asyncio.create_task(cache.purge(world.factory, world.entry.id))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done()
        assert world.provider.closed and world.permits[0]._settled
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
    assert (await mutations(world))[0].current_delta_bytes == -len(world.data)
    assert await current_entry(world) is not None


async def test_external_physical_uses_owned_without_changing_other_callers():
    observed, client = [], SimpleNamespace()

    @contextmanager
    def transport(**kwargs):
        observed.append(kwargs)
        yield client

    record = SimpleNamespace(
        location_id=object(), bucket="saved", _target=SimpleNamespace(client=transport)
    )
    with cache.physical_storage(record) as storage:
        assert storage.client is client
    with cache.physical_storage(record, owned=True) as storage:
        assert storage.client is client
    assert observed == [{"read_timeout": 15}, {"read_timeout": 15, "owned": True}]


@pytest.mark.parametrize("versioned", [False, True])
async def test_actual_minio_production_purge_keeps_original_versions_and_current_delta(
    removal_world, monkeypatch, versioned
):
    world, storage = removal_world, REAL_STORAGE()
    client, bucket, key = storage.client, world.bucket, world.key
    if not versioned:
        # Keep this owned bucket unversioned even when another case has already
        # enabled the cache bucket. Historical A rows may retain their bucket.
        bucket = settings.minio_import_bucket
        entry = await historical_entry(
            world,
            bucket=bucket,
            storage_identity=managed_storage_identity(world.endpoint, bucket),
        )
        key = cache.object_key(entry)
    saved_factory, sent, published = cache.make_storage, [], []
    publish = usage.publish
    loop = asyncio.get_running_loop()
    owned_clients = []

    def construct():
        owned = REAL_STORAGE()
        owned_clients.append(owned.client)
        original_close = owned.client.close

        def close():
            original_close()
            owned._closed = True

        owned.client.close = close

        async def inspect():
            rows = await mutations(world)
            assert len(rows) == 1 and rows[0].state == "pending"

        def before_send(**kwargs):
            asyncio.run_coroutine_threadsafe(inspect(), loop).result(5)
            sent.append(True)

        owned.client.meta.events.register("before-send.s3.DeleteObject", before_send)
        owned._closed = False
        owned_clients[-1]._cache_test_storage = owned
        return owned

    async def publishing(*args, **kwargs):
        assert all(value._cache_test_storage._closed for value in owned_clients)
        assert world.permits[0]._settled
        async with world.factory() as db:
            assert await db.get(StorageOperation, world.permits[0].id) is None
        assert await current_entry(world) is not None
        published.append(kwargs["settled"])
        return await publish(*args, **kwargs)

    try:
        if versioned:
            await asyncio.to_thread(
                client.put_bucket_versioning,
                Bucket=bucket,
                VersioningConfiguration={"Status": "Enabled"},
            )
        response = await asyncio.to_thread(
            client.put_object, Bucket=bucket, Key=key, Body=world.data
        )
        monkeypatch.setattr(cache, "make_storage", construct)
        monkeypatch.setattr(usage, "publish", publishing)
        assert await cache.purge(world.factory, world.entry.id) == "removed"
        row = (await mutations(world))[0]
        assert sent == [True] and published == [True]
        if versioned:
            assert row.state == "confirmed" and row.current_delta_bytes == -len(
                world.data
            )
            page = await asyncio.to_thread(
                client.list_object_versions, Bucket=bucket, Prefix=key
            )
            assert [value["VersionId"] for value in page["Versions"]] == [
                response["VersionId"]
            ]
            assert len(page["DeleteMarkers"]) == 1
            assert (
                page["DeleteMarkers"][0]["VersionId"]
                == row.evidence["returned_version"]
            )
        else:
            assert row.state == "unknown" and row.current_delta_bytes is None
        assert row.physical_outcome == "unknown" and await current_entry(world) is None
    finally:
        monkeypatch.setattr(cache, "make_storage", saved_factory)
        page = await asyncio.to_thread(
            client.list_object_versions, Bucket=bucket, Prefix=key
        )
        versions = [
            {"Key": value["Key"], "VersionId": value["VersionId"]}
            for value in [*page.get("Versions", []), *page.get("DeleteMarkers", [])]
            if value["Key"] == key
        ]
        if versions:
            await asyncio.to_thread(
                client.delete_objects, Bucket=bucket, Delete={"Objects": versions}
            )
        client.close()
