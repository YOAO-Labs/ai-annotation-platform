"""Published annotation reads retain saved ownership and business bookends."""

from contextlib import asynccontextmanager, nullcontext
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import uuid

from fastapi import HTTPException
import pytest
from sqlalchemy import delete

from app.db.models.raster_mask_object import RasterMaskPublication
from app.db.models.storage_maintenance import StorageMaintenanceGate, StorageOperation
from app.services import (
    comment_attachment_storage as comments,
    comment_attachment_uploads as comment_owners,
    guide_asset_storage as guides,
    guide_asset_uploads as guide_owners,
    raster_mask_objects as rle_owners,
    raster_object_storage as rle,
    storage_access,
    storage_migration_artifact_reads as migration_reads,
    storage_migration_copy as copy,
    storage_migration_read_publications as publications,
)
from app.services.content_snapshot import ContentError
from app.services.managed_original_retirement import managed_storage_identity
from app.services.original_read_plan import ReadRoute
from tests import test_comment_attachment_storage as comment_facts
from tests import test_guide_asset_storage as guide_facts
from tests import test_raster_object_storage as rle_facts


def saved(kind):
    if kind == "rle":
        return rle, rle_facts.record(), rle_facts.RAW, rle_facts.RLE
    if kind == "gzip":
        data = gzip.compress(rle_facts.RAW, mtime=1)
        return rle, rle_facts.record(data, "gzip"), data, rle_facts.RLE
    if kind == "comment":
        created = datetime.now(timezone.utc)
        return (
            comments,
            comment_facts.record(
                created_at=created,
                expires_at=created + timedelta(days=90),
                state="ready",
            ),
            comment_facts.RAW,
            comment_facts.RAW,
        )
    return guides, guide_facts.record(), guide_facts.RAW, guide_facts.RAW


def metadata(route):
    return {
        "ResponseMetadata": {"HTTPStatusCode": 200},
        "VersionId": route.target_version_id,
        "ETag": route.target_etag,
        "ContentLength": route.size_bytes,
        "ContentType": "application/octet-stream",
        "Metadata": {
            "aap-copy-plan": route.creation_plan_uuid,
            "aap-copy-object": route.source_object_id,
            "aap-copy-nonce": route.creation_nonce,
        },
    }


def route_for(storage, receipt, data):
    owner = storage.resolve(receipt)
    route = ReadRoute(
        publication_id=uuid.uuid4(),
        publication_revision=3,
        source_object_id="a" * 64,
        source_location_id=uuid.UUID(owner.ref["location_id"]),
        source_endpoint_url=owner.ref["endpoint_url"],
        source_bucket=owner.bucket,
        source_object_key=owner.object_key,
        source_version_id="source-version",
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        source_etag='"source-etag"',
        is_latest=True,
        target_location_id=uuid.uuid4(),
        target_location_revision=4,
        target_connection_id=uuid.uuid4(),
        target_connection_revision=5,
        target_endpoint_url="http://target.invalid:9000",
        target_bucket="published-copy",
        target_object_key="_aap_migration/frozen/annotation",
        target_version_id="target-version",
        target_etag='"target-etag"',
        target_content_sha256="b" * 64,
        creation_nonce="copy-nonce",
        creation_plan_uuid=str(uuid.uuid4()),
        maintenance_revision=7,
    )
    return replace(
        route, target_content_sha256=copy.digest(copy.content_target(metadata(route)))
    )


class Provider:
    def __init__(self, route, data):
        self.route, self.data = route, data
        self.meta = SimpleNamespace(endpoint_url=route.target_endpoint_url)
        self.calls, self.bodies = [], []
        self.body_factory = io.BytesIO

    def head_object(self, **kwargs):
        assert kwargs == {
            "Bucket": self.route.target_bucket,
            "Key": self.route.target_object_key,
            "VersionId": self.route.target_version_id,
        }
        self.calls.append(("head", kwargs))
        return metadata(self.route)

    def get_object(self, **kwargs):
        assert kwargs == {
            "Bucket": self.route.target_bucket,
            "Key": self.route.target_object_key,
            "VersionId": self.route.target_version_id,
            "IfMatch": self.route.target_etag,
        }
        self.calls.append(("get", kwargs))
        body = self.body_factory(self.data)
        self.bodies.append(body)
        return {**metadata(self.route), "Body": body}


@pytest.mark.parametrize("kind", ["rle", "gzip", "comment", "guide"])
def test_routed_read_keeps_real_target_version_and_saved_receipt(kind):
    storage, receipt, data, expected = saved(kind)
    original = deepcopy(receipt.storage_ref)
    route = route_for(storage, receipt, data)
    provider = Provider(route, data)
    reader = migration_reads.ReadClient(provider, route)
    try:
        # Ordinary creation verification still requires the saved endpoint.
        with pytest.raises(ValueError):
            storage.read(provider, receipt)
        assert provider.calls == []
        assert storage.read_routed(reader, receipt) == expected
        assert [operation for operation, _ in provider.calls] == ["head", "get", "head"]
        assert all(body.closed for body in provider.bodies)
        assert receipt.storage_ref == original
    finally:
        reader.close()


@pytest.mark.parametrize("kind", ["rle", "gzip", "comment", "guide"])
@pytest.mark.parametrize(
    "change",
    [
        {"source_location_id": uuid.UUID(int=999)},
        {"source_endpoint_url": "http://other.invalid"},
        {"source_bucket": "other-bucket"},
        {"source_object_key": "other-key"},
        {"sha256": "f" * 64},
        {"size_bytes": 10000},
    ],
)
def test_routed_entry_requires_full_saved_source_identity_before_io(kind, change):
    storage, receipt, data, _ = saved(kind)
    route = replace(route_for(storage, receipt, data), **change)
    provider = Provider(route, data)
    with pytest.raises(ValueError):
        storage.read_routed(migration_reads.ReadClient(provider, route), receipt)
    assert provider.calls == []


@pytest.mark.parametrize("kind", ["rle", "gzip", "comment", "guide"])
def test_routed_read_hashes_entire_original_bytes_and_closes_on_corruption(kind):
    storage, receipt, data, _ = saved(kind)
    route = route_for(storage, receipt, data)
    provider = Provider(route, bytes([data[0] ^ 1]) + data[1:])
    reader = migration_reads.ReadClient(provider, route)
    try:
        with pytest.raises(ValueError) as rejected:
            storage.read_routed(reader, receipt)
        assert "integrity_mismatch" in rejected.value.code
        assert all(body.closed for body in provider.bodies)
    finally:
        reader.close()


@pytest.mark.parametrize("kind", ["rle", "gzip", "comment", "guide"])
def test_routed_reader_preserves_unknown_body_close_responsibility(kind):
    storage, receipt, data, _ = saved(kind)
    route = route_for(storage, receipt, data)
    provider = Provider(route, data)

    class UnknownClose(io.BytesIO):
        def close(self):
            raise RuntimeError("close outcome unknown")

    provider.body_factory = UnknownClose
    reader = migration_reads.ReadClient(provider, route)
    try:
        with pytest.raises(ValueError) as rejected:
            storage.read_routed(reader, receipt)
        assert rejected.value.storage_operation_unsettled is True
        with pytest.raises(RuntimeError) as close:
            reader.close()
        assert close.value.storage_operation_unsettled is True
    finally:
        for body in provider.bodies:
            io.BytesIO.close(body)


@pytest.mark.parametrize("kind", ["comment", "guide"])
@pytest.mark.parametrize("revoke_at", [None, "acquired", "final"])
async def test_owner_read_keeps_authority_bookends_without_source_gate(
    monkeypatch, kind, revoke_at
):
    storage, receipt, data, _ = saved(kind)
    owners = comment_owners if kind == "comment" else guide_owners
    route = route_for(storage, receipt, data)
    provider = Provider(route, data)
    original = deepcopy(receipt.storage_ref)
    proof, actor, context = object(), object(), object()
    db = SimpleNamespace(commit=AsyncMock())
    lock = AsyncMock(return_value=(actor, proof))
    binding = AsyncMock(
        return_value=(context, receipt) if kind == "comment" else receipt
    )
    checkpoints = 0

    async def authorize(*args):
        nonlocal checkpoints
        checkpoints += 1
        if (revoke_at == "acquired" and checkpoints == 2) or (
            revoke_at == "final" and checkpoints == 4
        ):
            raise HTTPException(403, {"code": "original_authority_revoked"})
        return actor

    async def mapped_run(db, selected, purpose, callback, *, on_acquired):
        assert selected is route
        expected = (
            comment_owners.read_purpose(receipt.id)
            if kind == "comment"
            else guide_owners.purpose("read", receipt.id)
        )
        assert purpose == expected
        await on_acquired(SimpleNamespace(revision=route.maintenance_revision))
        reader = migration_reads.ReadClient(provider, route)
        try:
            return callback(reader)
        finally:
            reader.close()

    resolve = AsyncMock(return_value=route)
    recheck = AsyncMock()
    monkeypatch.setattr(owners, "lock_authority", lock)
    monkeypatch.setattr(owners, "reauthorize", authorize)
    monkeypatch.setattr(owners, "_read_binding", binding)
    monkeypatch.setattr(
        owners, "check_deadlines" if kind == "comment" else "check_clock", Mock()
    )
    monkeypatch.setattr(migration_reads, "resolve", resolve)
    monkeypatch.setattr(migration_reads, "run", mapped_run)
    monkeypatch.setattr(publications, "recheck", recheck)
    source_gate = AsyncMock(
        side_effect=AssertionError("source Gate must remain stopped")
    )
    source_transport = AsyncMock(side_effect=AssertionError("must not open source"))
    monkeypatch.setattr(storage_access, "check", source_gate)
    monkeypatch.setattr(storage, "io_target", source_transport)
    call = owners.read(
        db,
        actor,
        object(),
        receipt.annotation_id if kind == "comment" else receipt.project_id,
        "saved-key",
    )
    if revoke_at is not None:
        with pytest.raises(HTTPException) as rejected:
            await call
        assert rejected.value.status_code == 403
        assert len(provider.calls) == (0 if revoke_at == "acquired" else 3)
    else:
        returned, actual = await call
        assert returned.storage_ref == original
        assert actual == data
        assert binding.await_count == 3
        assert checkpoints == 5
        recheck.assert_awaited_once_with(db, route, lock=True)
    owner = storage.resolve(receipt)
    resolve.assert_awaited_once_with(
        db, owner.ref, owner.object_key, receipt.sha256, receipt.size_bytes
    )
    source_gate.assert_not_awaited()
    source_transport.assert_not_awaited()
    assert all(body.closed for body in provider.bodies)


@asynccontextmanager
async def session(db):
    yield db


def patch_rle_session(monkeypatch, row):
    db = SimpleNamespace(
        scalar=AsyncMock(return_value=row),
        execute=AsyncMock(return_value=SimpleNamespace(rowcount=1)),
        commit=AsyncMock(),
        add=Mock(),
        flush=AsyncMock(),
        info={},
        no_autoflush=nullcontext(),
    )
    monkeypatch.setattr(rle_owners, "_session", session)
    monkeypatch.setattr(rle_owners, "_publish_lock", AsyncMock())
    monkeypatch.setattr(rle_owners, "_owners", AsyncMock(return_value={row.id: row}))
    return db


async def test_rle_dedup_and_load_use_winners_stored_gzip_and_source_guard_fact(
    monkeypatch,
):
    _, row, data, expected = saved("gzip")
    row.state = "ready"
    row.published_at = row.last_used_at = row.created_at
    receipt = rle.snapshot(row)
    route = route_for(rle, receipt, data)
    provider = Provider(route, data)
    db = patch_rle_session(monkeypatch, row)
    resolve = AsyncMock(return_value=route)
    source_gate = AsyncMock(side_effect=AssertionError("no active source Gate"))
    monkeypatch.setattr(migration_reads, "resolve", resolve)
    monkeypatch.setattr(publications, "recheck", AsyncMock())
    monkeypatch.setattr(
        storage_access,
        "state",
        AsyncMock(return_value={"state": "stopped", "revision": 12}),
    )
    monkeypatch.setattr(rle_owners, "_gate", source_gate)

    async def mapped_run(db, selected, purpose, callback, *, on_acquired):
        assert selected is route
        assert purpose == rle_owners.purpose("read", receipt.id)
        await on_acquired(SimpleNamespace(revision=route.maintenance_revision))
        reader = migration_reads.ReadClient(provider, selected)
        try:
            return callback(reader)
        finally:
            reader.close()

    monkeypatch.setattr(migration_reads, "run", mapped_run)
    # Same canonical RLE, different wrapper bytes: reuse the saved owner's hash.
    different_wrapper = gzip.compress(rle_facts.RAW, mtime=2)
    assert different_wrapper != data
    reference = rle.reference(receipt)
    result = await rle_owners.store(reference, different_wrapper, db=db)
    assert result == reference
    assert await rle_owners.load(reference, db=db) == expected
    for call in resolve.await_args_list:
        assert call.args == (
            db,
            rle.resolve(receipt).ref,
            rle.resolve(receipt).object_key,
            receipt.stored_sha256,
            receipt.stored_size_bytes,
        )
    publication = db.add.call_args.args[0]
    assert isinstance(publication, RasterMaskPublication)
    assert publication.gate_revision == 12 != route.maintenance_revision
    guard = next(
        iter(db.info[rle_owners.PUBLICATIONS_INFO][receipt.logical_key].values())
    )
    assert guard.revision == publication.gate_revision
    assert guard.read_route is route
    assert all(operation in ("head", "get") for operation, _ in provider.calls)
    assert all(body.closed for body in provider.bodies)
    source_gate.assert_not_awaited()


async def test_rle_competing_candidate_transfers_guard_to_routed_winner(monkeypatch):
    _, winner, data, _ = saved("gzip")
    winner.state = "ready"
    winner.published_at = winner.last_used_at = winner.created_at
    winner_receipt = rle.snapshot(winner)
    candidate = rle_facts.record(data, "gzip")
    candidate.id, candidate.state = uuid.uuid4(), "pending"
    endpoint, bucket, prefix = (
        "http://new-writes.invalid:9000",
        "new-writes",
        "_aap_managed/annotations/",
    )
    candidate.storage_ref = {
        "schema": 2,
        "purpose": "annotations",
        "location_id": str(uuid.uuid4()),
        "config_revision": 1,
        "binding_generation": 1,
        "endpoint_url": endpoint,
        "bucket": bucket,
        "key_prefix": prefix,
        "object_key": prefix + rle.key_for(candidate.id, "gzip"),
        "storage_identity": managed_storage_identity(endpoint, bucket),
    }
    candidate_receipt = rle.snapshot(candidate)
    route = route_for(rle, winner_receipt, data)
    provider = Provider(route, data)
    db = patch_rle_session(monkeypatch, winner)
    monkeypatch.setattr(
        rle_owners,
        "_owners",
        AsyncMock(return_value={candidate.id: candidate, winner.id: winner}),
    )
    monkeypatch.setattr(migration_reads, "resolve", AsyncMock(return_value=route))
    monkeypatch.setattr(publications, "recheck", AsyncMock())
    monkeypatch.setattr(
        storage_access,
        "state",
        AsyncMock(return_value={"state": "stopped", "revision": 12}),
    )

    async def candidate_gate(db, receipt, revision=None):
        assert receipt.id == candidate.id
        assert revision == 2
        return 2

    async def mapped_run(db, selected, purpose, callback, *, on_acquired):
        assert selected is route
        assert purpose == rle_owners.purpose("read", winner.id)
        await on_acquired(SimpleNamespace(revision=route.maintenance_revision))
        reader = migration_reads.ReadClient(provider, selected)
        try:
            return callback(reader)
        finally:
            reader.close()

    monkeypatch.setattr(rle_owners, "_gate", candidate_gate)
    monkeypatch.setattr(migration_reads, "run", mapped_run)
    guard = rle_owners._Guard(uuid.uuid4(), candidate_receipt, 2)
    actual, replacement = await rle_owners._publish(
        db, candidate_receipt, 2, guard, True, object()
    )
    assert actual == winner_receipt
    assert candidate.state == "retired" and candidate.retired_at is not None
    assert replacement.receipt == winner_receipt
    assert replacement.revision == 12 != route.maintenance_revision
    assert replacement.read_route is route
    assert [operation for operation, _ in provider.calls] == ["head", "get", "head"]
    assert all(body.closed for body in provider.bodies)


async def test_rle_without_route_preserves_source_gate_rejection(monkeypatch):
    _, row, _, _ = saved("gzip")
    gate = AsyncMock(side_effect=ContentError("storage_maintenance"))
    monkeypatch.setattr(migration_reads, "resolve", AsyncMock(return_value=None))
    monkeypatch.setattr(rle_owners, "_gate", gate)
    with pytest.raises(ContentError, match="storage_maintenance"):
        await rle_owners._read_plan(object(), rle.snapshot(row))
    gate.assert_awaited_once()


@pytest.mark.parametrize("withdrawn", [False, True])
async def test_rle_guard_binding_rechecks_route_and_retains_guard_on_withdrawal(
    monkeypatch, withdrawn
):
    _, row, data, _ = saved("gzip")
    row.state = "ready"
    receipt = rle.snapshot(row)
    route = route_for(rle, receipt, data)
    guard = rle_owners._Guard(uuid.uuid4(), receipt, 12, route)
    publication = RasterMaskPublication(
        id=guard.id, object_id=receipt.id, gate_revision=12
    )
    db = SimpleNamespace(
        info={rle_owners.PUBLICATIONS_INFO: {receipt.logical_key: {guard.id: guard}}},
        scalars=AsyncMock(
            side_effect=[
                SimpleNamespace(all=lambda: [publication]),
                SimpleNamespace(all=lambda: [row]),
            ]
        ),
        execute=AsyncMock(),
        no_autoflush=nullcontext(),
    )
    source_gate = AsyncMock(
        side_effect=AssertionError("must not substitute source Gate")
    )
    recheck = AsyncMock(
        side_effect=ContentError("read_publication_changed") if withdrawn else None
    )
    monkeypatch.setattr(rle_owners, "_gate", source_gate)
    monkeypatch.setattr(publications, "recheck", recheck)
    if withdrawn:
        with pytest.raises(ContentError, match="read_publication_changed"):
            await rle_owners.bind_publications(db, rle.reference(receipt))
        db.execute.assert_not_awaited()
    else:
        assert await rle_owners.bind_publications(db, rle.reference(receipt)) == 1
        db.execute.assert_awaited_once()
    recheck.assert_awaited_once_with(db, route, lock=True)
    source_gate.assert_not_awaited()
    assert db.info[rle_owners.PUBLICATIONS_INFO][receipt.logical_key][guard.id] is guard


@pytest.mark.parametrize("kind", ["rle", "comment", "guide"])
async def test_sql_target_owner_read_permit_protects_original_cleanup(db_session, kind):
    source, target, identifier = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    receipt = SimpleNamespace(id=identifier, storage_ref={"location_id": str(source)})
    purpose = (
        rle_owners.purpose("read", identifier)
        if kind == "rle"
        else comment_owners.read_purpose(identifier)
        if kind == "comment"
        else guide_owners.purpose("read", identifier)
    )
    db_session.add(StorageMaintenanceGate(location_id=target, revision=7))
    await db_session.flush()
    operation = StorageOperation(
        id=uuid.uuid4(),
        location_id=target,
        revision=7,
        purpose=purpose,
        node_id="annotation-read-test",
    )
    db_session.add(operation)
    await db_session.flush()
    check = (
        rle_owners._guarded
        if kind == "rle"
        else comment_owners._has_read
        if kind == "comment"
        else guide_owners._has_operation
    )
    assert await check(db_session, receipt)
    if kind == "rle":
        assert not await check(db_session, receipt, exclude_operation=operation.id)
    elif kind == "guide":
        assert not await check(db_session, receipt, excluding=operation.id)
    try:
        await db_session.execute(
            delete(StorageOperation).where(StorageOperation.id == operation.id)
        )
        assert not await check(db_session, receipt)
    finally:
        await db_session.execute(
            delete(StorageOperation).where(StorageOperation.id == operation.id)
        )
        await db_session.execute(
            delete(StorageMaintenanceGate).where(
                StorageMaintenanceGate.location_id == target
            )
        )
