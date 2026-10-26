"""Cache copies keep canonical authority, saved ownership and physical admission apart."""

from contextlib import asynccontextmanager, contextmanager
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import asyncio
import hashlib
import io
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from fastapi import HTTPException
from PIL import Image
import pytest
from sqlalchemy import delete, select

from app.db.models.original_cache import (
    OriginalCacheEntry as Entry,
    OriginalCacheReadLease,
)
from app.db.models.storage_maintenance import StorageMaintenanceGate, StorageOperation
from app.services import content_snapshot as snapshots
from app.services import dataset_workbench, original_cache as cache
from app.services import media_cache_storage, storage_access
from app.services import storage_migration_copy as copy
from app.services import storage_migration_read_publications as publications
from app.services.content_snapshot import ContentError
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.managed_original_retirement import managed_storage_identity
from app.services.original_read_plan import ReadRoute


def image_bytes():
    with io.BytesIO() as output, Image.new("RGB", (8, 6), "navy") as image:
        image.save(output, "PNG")
        return output.getvalue()


def entry(data, shape="schema2"):
    created = datetime.now(timezone.utc)
    endpoint = media_cache_storage._runtime_endpoint()
    location = LEGACY_LOCATION_ID
    root, generation = "", 1
    if shape == "managed":
        location, endpoint = uuid.UUID(int=20), "http://saved-cache.test:9000"
        root, generation = "tenant/_aap_managed/media-cache/", 2
    record = SimpleNamespace(
        id=uuid.UUID(int=10),
        scope_location_id=uuid.UUID(int=30),
        source_id=uuid.UUID(int=40),
        asset_id=uuid.UUID(int=50),
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        source_revision=2,
        policy_revision=3,
        location_revision=4,
        location_id=location,
        bucket="saved-cache-a",
        binding_generation=generation,
        storage_identity=managed_storage_identity(endpoint, "saved-cache-a"),
        state="ready",
        uncertain_write=False,
        created_at=created,
        verified_at=created,
        last_accessed_at=created,
        expires_at=created + timedelta(hours=1),
        writer_until=created,
        retain_until=created + timedelta(days=7),
        next_check_at=created,
        error_code=None,
        storage_ref=None,
    )
    if shape != "null":
        record.storage_ref = {
            "schema": 2,
            "purpose": "media-cache",
            "location_id": str(location),
            "config_revision": 1,
            "binding_generation": generation,
            "endpoint_url": endpoint,
            "bucket": record.bucket,
            "prefix": root + cache.logical_prefix(record),
            "key_prefix": root,
            "storage_identity": record.storage_identity,
        }
    return record, endpoint


def metadata(route):
    return {
        "ResponseMetadata": {"HTTPStatusCode": 200},
        "ContentLength": route.size_bytes,
        "ContentType": "image/png",
        "VersionId": route.target_version_id,
        "ETag": route.target_etag,
        "Metadata": {
            "aap-copy-plan": route.creation_plan_uuid,
            "aap-copy-object": route.source_object_id,
            "aap-copy-nonce": route.creation_nonce,
        },
    }


def route_for(record, endpoint):
    route = ReadRoute(
        publication_id=uuid.UUID(int=60),
        publication_revision=5,
        source_object_id="a" * 64,
        source_location_id=record.location_id,
        source_endpoint_url=endpoint,
        source_bucket=record.bucket,
        source_object_key=cache.object_key(record),
        source_version_id="a-version-not-in-entry-contract",
        sha256=record.sha256,
        size_bytes=record.size_bytes,
        source_etag='"a-etag"',
        is_latest=True,
        target_location_id=uuid.UUID(int=70),
        target_location_revision=6,
        target_connection_id=uuid.UUID(int=80),
        target_connection_revision=7,
        target_endpoint_url="http://actual-cache-b.test:9000",
        target_bucket="shared-migration",
        target_object_key="_aap_migration/sealed/cache-payload",
        target_version_id="b-exact-version",
        target_etag='"b-etag"',
        target_content_sha256="0" * 64,
        creation_nonce="cache-copy-nonce",
        creation_plan_uuid=str(uuid.UUID(int=90)),
        maintenance_revision=8,
    )
    return replace(
        route, target_content_sha256=copy.digest(copy.content_target(metadata(route)))
    )


@dataclass(frozen=True)
class SavedTarget:
    endpoint_url: str
    bucket: str
    key_prefix: str = ""


class Session:
    def __init__(self, world):
        self.world, self.transaction, self.closed = world, False, False
        self.selected = None

    async def scalar(self, _query):
        self.transaction = True
        self.selected = deepcopy(self.world.entry)
        return self.selected

    async def get(self, model, identity, **_kwargs):
        self.transaction = True
        if model is Entry:
            return deepcopy(self.world.entry)
        if model is OriginalCacheReadLease:
            return self.world.leases.get(identity)
        if model is cache.StorageLocation:
            return SimpleNamespace(id=identity)
        if model is cache.DatasetAssetLocation:
            return SimpleNamespace(
                role="original_cache",
                object_key=self.world.route.source_object_key,
                reference_state="present",
            )
        raise AssertionError(model)

    def add(self, row):
        assert isinstance(row, OriginalCacheReadLease)
        self.world.leases[row.id] = row

    async def execute(self, statement):
        assert str(statement).startswith("DELETE FROM original_cache_read_leases")
        self.world.leases.clear()

    async def commit(self):
        self.transaction = False
        if self.selected is not None:
            self.world.entry.last_accessed_at = self.selected.last_accessed_at


class Permit:
    def __init__(self, world, location, purpose, revision):
        self.world, self.location_id = world, location
        self.purpose, self.revision, self._settled = purpose, revision, False

    async def __aenter__(self):
        self.world.active.append(self)
        return self

    async def __aexit__(self, _kind, failure, _traceback):
        if not getattr(failure, "storage_operation_unsettled", False):
            self._settled = True
            self.world.active.remove(self)


class Body(io.BytesIO):
    def __init__(self, data, world):
        super().__init__(data)
        self.world = world

    def close(self):
        if self.world.close_failure == "body":
            raise OSError("unknown physical body close")
        super().close()


class Target:
    def __init__(self, world):
        self.world = world
        self.meta = SimpleNamespace(endpoint_url=world.route.target_endpoint_url)

    @contextmanager
    def client(self):
        world = self.world
        assert not any(
            session.transaction for session in world.sessions if not session.closed
        )
        assert {(permit.location_id, permit.purpose) for permit in world.active} == {
            (world.route.target_location_id, world.route.purpose),
            (world.route.target_location_id, cache.read_purpose(world.entry.id)),
            (world.configured.location_id, "original_cache_read"),
        }
        world.opens += 1
        try:
            yield self
        finally:
            assert not world.settled.is_set()
            world.closes += 1
            if world.close_failure == "client":
                raise OSError("unknown physical client close")

    def get_object(self, **params):
        route = self.world.route
        assert params == {
            "Bucket": route.target_bucket,
            "Key": route.target_object_key,
            "VersionId": route.target_version_id,
            "IfMatch": route.target_etag,
        }
        if self.world.read_started is not None:
            self.world.read_started.set()
            assert self.world.read_release.wait(5)
        self.world.gets.append(params)
        body = Body(self.world.data, self.world)
        self.world.bodies.append(body)
        if self.world.after_get is not None:
            self.world.after_get()
        return {**metadata(route), "Body": body}


class World:
    def __init__(self, monkeypatch, shape="schema2"):
        self.data = image_bytes()
        self.entry, endpoint = entry(self.data, shape)
        self.route = route_for(self.entry, endpoint)
        self.configured = cache.Scope(
            self.entry.scope_location_id,
            self.entry.sha256,
            self.entry.size_bytes,
            self.entry.source_revision,
            self.entry.policy_revision,
            self.entry.location_revision,
            2 * len(self.data),
            3600,
            9,
        )
        self.leases, self.sessions, self.permits, self.active = {}, [], [], []
        self.opens = self.closes = 0
        self.bodies, self.gets = [], []
        self.settled, self.close_failure, self.after_get = None, None, None
        self.withdrawn, self.mapped, self.block_canonical = False, True, False
        self.read_started, self.read_release = None, None
        self.target = Target(self)
        self.lookup = AsyncMock(side_effect=self.lookup_route)
        self.retire = AsyncMock()
        monkeypatch.setattr(cache, "scope", self.scope)
        monkeypatch.setattr(publications, "lookup", self.lookup)
        monkeypatch.setattr(publications, "target_for_route", self.target_for_route)
        monkeypatch.setattr(publications, "recheck", self.recheck)
        monkeypatch.setattr(storage_access, "acquire", self.acquire)
        monkeypatch.setattr(storage_access, "check", self.source_gate)
        monkeypatch.setattr(cache, "retire", self.retire)
        monkeypatch.setattr(
            media_cache_storage,
            "target_for",
            AsyncMock(return_value=SavedTarget(endpoint, self.entry.bucket)),
        )
        monkeypatch.setattr(
            media_cache_storage,
            "target_for_ref",
            AsyncMock(return_value=SavedTarget(endpoint, self.entry.bucket)),
        )
        monkeypatch.setattr(
            cache,
            "make_storage",
            lambda: (_ for _ in ()).throw(AssertionError("A SDK")),
        )
        routed = cache.routed_object

        def track(record, admission, settled):
            self.settled = settled
            return routed(record, admission, settled)

        monkeypatch.setattr(cache, "routed_object", track)

    @asynccontextmanager
    async def factory(self):
        session = Session(self)
        self.sessions.append(session)
        try:
            yield session
        finally:
            session.closed, session.transaction = True, False

    async def scope(self, _db, source, asset, **_kwargs):
        assert source == self.entry.source_id and asset == self.entry.asset_id
        if self.block_canonical:
            raise ContentError("storage_maintenance")
        return self.configured

    async def source_gate(self, _db, location, **_kwargs):
        assert location == self.entry.location_id
        raise ContentError("storage_maintenance")

    async def lookup_route(self, db, **params):
        db.transaction = True
        assert params == {
            "source_location_id": self.entry.location_id,
            "endpoint_url": self.route.source_endpoint_url,
            "bucket": self.entry.bucket,
            "object_key": cache.object_key(self.entry),
            "sha256": self.entry.sha256,
            "size_bytes": self.entry.size_bytes,
            "source_version_id": publications.ANY_VERSION,
        }
        return self.route if self.mapped else None

    async def target_for_route(self, db, route):
        await self.recheck(db, route)
        return self.target

    async def recheck(self, db, route, **_kwargs):
        assert route is self.route
        db.transaction = True
        if self.withdrawn:
            raise ContentError("read_publication_changed")

    async def acquire(self, _db, location, purpose, *, expected_revision, node_id=None):
        assert node_id is None
        expected = (
            (self.configured.location_id, 9)
            if purpose == "original_cache_read"
            else (self.route.target_location_id, 8)
        )
        assert (location, expected_revision) == expected
        permit = Permit(self, location, purpose, expected_revision)
        self.permits.append(permit)
        return permit

    async def read(self, **kwargs):
        return await cache.read(
            self.factory, self.entry.source_id, self.entry.asset_id, **kwargs
        )


@pytest.mark.parametrize("shape", ["schema2", "managed", "null"])
async def test_ready_copy_reads_exact_b_while_canonical_and_saved_owners_stay_separate(
    monkeypatch, shape
):
    world = World(monkeypatch, shape)
    logical = f"original-cache/v1/{world.entry.asset_id}/{world.entry.sha256}/{world.entry.id}"
    assert world.route.source_object_key == (
        logical
        if shape == "null"
        else ("tenant/_aap_managed/media-cache/" if shape == "managed" else "")
        + logical
        + "/payload"
    )
    before = cache._read_facts(world.entry)
    slots = snapshots._slots._value
    authorized = AsyncMock()
    result = await world.read(on_acquired=authorized)
    try:
        assert result.file.read() == world.data
        assert result.sha256 == world.entry.sha256
        assert (result.width, result.height) == (8, 6)
        assert cache._read_facts(world.entry) == before
        assert world.entry.state == "ready" and not world.leases
        assert not hasattr(world.entry, "_target")
        assert world.settled.is_set() and world.opens == world.closes == 1
        assert all(body.closed for body in world.bodies)
        assert all(permit._settled for permit in world.permits)
        assert len(world.permits) == 3
        authorized.assert_awaited_once()
    finally:
        result.close()
    assert snapshots._slots._value == slots


@pytest.mark.parametrize(
    "field,value",
    [
        ("location_id", uuid.UUID(int=99)),
        ("bucket", "other-owner"),
        ("binding_generation", 99),
        ("storage_identity", "f" * 64),
    ],
)
async def test_saved_cache_scalar_mismatch_rejects_before_publication_lookup(
    monkeypatch, field, value
):
    world = World(monkeypatch)
    setattr(world.entry, field, value)
    async with world.factory() as db:
        with pytest.raises(media_cache_storage.MediaCacheStorageError):
            await cache.prepare_read(db, world.entry)
    world.lookup.assert_not_awaited()
    assert not world.opens


@pytest.mark.parametrize("blocked", ["canonical", "unmapped-cache", "authority"])
async def test_canonical_authority_and_missing_mapping_keep_original_refusals(
    monkeypatch, blocked
):
    world = World(monkeypatch)
    world.block_canonical = blocked == "canonical"
    world.mapped = blocked != "unmapped-cache"
    authorized = AsyncMock(
        side_effect=HTTPException(403, "authority revoked")
        if blocked == "authority"
        else None
    )
    with pytest.raises(HTTPException if blocked == "authority" else ContentError):
        await world.read(on_acquired=authorized)
    assert not world.opens and not world.leases
    assert world.entry.state == "ready"
    world.retire.assert_not_awaited()
    assert all(permit._settled for permit in world.permits)


@pytest.mark.parametrize("phase", ["lookup", "after-physical"])
async def test_publication_control_failure_never_retires_good_cache(monkeypatch, phase):
    world = World(monkeypatch)
    if phase == "lookup":
        world.withdrawn = True

        async def withdrawn(*_args, **_kwargs):
            raise ContentError("read_publication_changed")

        world.lookup.side_effect = withdrawn
    else:
        world.after_get = lambda: setattr(world, "withdrawn", True)
    slots = snapshots._slots._value
    with pytest.raises(ContentError) as failure:
        await world.read()
    assert failure.value.code == "read_publication_changed"
    world.retire.assert_not_awaited()
    assert world.entry.state == "ready" and not world.leases
    assert all(body.closed for body in world.bodies)
    assert all(permit._settled for permit in world.permits)
    assert snapshots._slots._value == slots


@pytest.mark.parametrize("failure", ["body", "client"])
async def test_unknown_target_close_keeps_lease_and_unique_b_read_responsibility(
    monkeypatch, failure
):
    world = World(monkeypatch)
    world.close_failure = failure
    with pytest.raises(ContentError) as error:
        await world.read()
    assert error.value.storage_operation_unsettled
    assert world.leases and not world.settled.is_set()
    assert any(
        permit.location_id == world.route.target_location_id
        and permit.purpose == cache.read_purpose(world.entry.id)
        and not permit._settled
        for permit in world.active
    )
    world.retire.assert_not_awaited()
    # Fake provider handles have no external lifetime; close them explicitly.
    world.close_failure = None
    for body in world.bodies:
        body.close()


async def test_repeated_cancellation_drains_real_b_handle_before_release(monkeypatch):
    world = World(monkeypatch)
    world.read_started, world.read_release = threading.Event(), threading.Event()
    slots = snapshots._slots._value
    pending = asyncio.create_task(world.read())
    try:
        assert await asyncio.to_thread(world.read_started.wait, 5)
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done() and world.leases
        assert not world.settled.is_set()
        assert all(not permit._settled for permit in world.permits)
        world.read_release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
    finally:
        world.read_release.set()
        if not pending.done():
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
    assert world.opens == world.closes == 1
    assert all(body.closed for body in world.bodies)
    assert world.settled.is_set() and not world.leases
    assert all(permit._settled for permit in world.permits)
    assert snapshots._slots._value == slots


@pytest.mark.parametrize("change", ["owner", "policy", "expiry"])
async def test_ready_cache_delivery_rechecks_saved_owner_policy_and_expiry(
    monkeypatch, change
):
    world = World(monkeypatch)

    def mutate():
        if change == "owner":
            world.entry.bucket = "different-cache-owner"
        elif change == "policy":
            world.configured = replace(world.configured, policy_revision=99)
        else:
            world.entry.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    world.after_get = mutate
    assert await world.read() is None
    assert not world.leases and all(body.closed for body in world.bodies)
    world.retire.assert_not_awaited()


async def test_workbench_qualifies_each_saved_member_and_still_checks_canonical_gate(
    monkeypatch,
):
    raw = image_bytes()
    first, _endpoint = entry(raw)
    second = deepcopy(first)
    second.id = uuid.UUID(int=11)
    second.storage_ref["prefix"] = cache.logical_prefix(second)
    db = SimpleNamespace(scalars=AsyncMock(return_value=[first, second]))
    monkeypatch.setattr(
        dataset_workbench,
        "observations",
        lambda *_args, **_kwargs: select(
            Entry.id.label("original_cache_entry_id")
        ).subquery(),
    )
    prepare = AsyncMock(
        side_effect=[object(), ContentError("read_publication_changed")]
    )
    gate = AsyncMock()
    monkeypatch.setattr(cache, "prepare_read", prepare)
    monkeypatch.setattr(storage_access, "check", gate)
    assert await dataset_workbench.available_cache_ids(db, uuid.uuid4(), object()) == [
        first.id
    ]
    assert prepare.await_count == 2
    gate.assert_awaited_once_with(db, first.scope_location_id)


async def test_sql_durable_b_purpose_blocks_gc_after_read_lease_expiry(db_session):
    record, _endpoint = entry(image_bytes())
    record.id = uuid.uuid4()
    record.asset_id = uuid.uuid4()
    record.storage_ref["prefix"] = cache.logical_prefix(record)
    target = uuid.uuid4()
    lease_id, operation_id = uuid.uuid4(), uuid.uuid4()
    db_session.add(Entry(**vars(record)))
    db_session.add(
        StorageMaintenanceGate(location_id=target, state="active", revision=1)
    )
    await db_session.flush()
    db_session.add(
        OriginalCacheReadLease(
            id=lease_id,
            entry_id=record.id,
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
    )
    await db_session.flush()
    assert not await cache.readers(db_session, record.id)
    db_session.add(
        StorageOperation(
            id=operation_id,
            location_id=target,
            revision=1,
            purpose=cache.read_purpose(record.id),
            node_id="original-cache-migration-test",
        )
    )
    await db_session.flush()
    try:
        assert await cache.readers(db_session, record.id)
        await db_session.execute(
            delete(StorageOperation).where(StorageOperation.id == operation_id)
        )
        assert not await cache.readers(db_session, record.id)
    finally:
        await db_session.execute(
            delete(StorageOperation).where(StorageOperation.id == operation_id)
        )
        await db_session.execute(
            delete(OriginalCacheReadLease).where(OriginalCacheReadLease.id == lease_id)
        )
        await db_session.execute(delete(Entry).where(Entry.id == record.id))
        await db_session.execute(
            delete(StorageMaintenanceGate).where(
                StorageMaintenanceGate.location_id == target
            )
        )
