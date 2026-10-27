"""Saved Mask staging uses one exact copy without reopening upload permission."""

import asyncio
from contextlib import asynccontextmanager, contextmanager
from copy import deepcopy
from dataclasses import replace
import hashlib
import io
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.services import import_storage, storage_access
from app.services import storage_migration_copy as copy
from app.services import storage_migration_read_publications as publications
from app.services.content_snapshot import ContentError
from app.services.mask_formats import service
from app.schemas.mask_format import MaskFormatPlan
from tests.test_mask_import_storage import external_ref
from tests.test_storage_migration_artifact_reads import response, route

RAW = b"saved bytes"
SHA = hashlib.sha256(RAW).hexdigest()


class Body(io.BytesIO):
    def __init__(self, world):
        super().__init__(world.payload)
        self.world = world
        self.started, self.resume = threading.Event(), threading.Event()

    def read(self, size=-1):
        if self.world.block_read:
            self.started.set()
            assert self.resume.wait(5)
        return super().read(size)

    def close(self):
        self.world.events.append("body-close")
        if self.world.fail_body_close:
            raise RuntimeError("private close failure")
        super().close()


class World:
    def __init__(self, monkeypatch, *, schema=2):
        self.key = f"mask-formats/{uuid.uuid4()}/{uuid.uuid4()}/{uuid.uuid4()}/a.json"
        prefix, self.relative = service.staged_parts(self.key)
        self.ref = (
            external_ref(prefix) if schema == 2 else import_storage.legacy_ref(prefix)
        )
        self.owner = import_storage.resolve_ref(self.ref, expected_prefix=prefix)
        self.route = replace(
            route(),
            source_location_id=self.owner.location_id,
            source_endpoint_url=self.ref["endpoint_url"],
            source_bucket=self.owner.bucket,
            source_object_key=self.owner.key(self.relative),
            sha256=SHA,
            size_bytes=len(RAW),
        )
        self.route = replace(
            self.route,
            target_content_sha256=copy.digest(
                copy.content_target(response(self.route))
            ),
        )
        self.payload = RAW
        self.events, self.calls, self.permits, self.rechecks = [], [], [], []
        self.revoked = self.block_read = False
        self.fail_body_close = self.fail_client_close = False
        self.head_changes, self.get_changes = {}, {}
        self.body = None
        self.lookup = AsyncMock(return_value=self.route)
        self.db = SimpleNamespace(rollback=AsyncMock())
        self.target = SimpleNamespace(client=self.client)

        @asynccontextmanager
        async def session():
            yield self.db

        async def recheck(db, selected, **kwargs):
            assert selected == self.route
            self.rechecks.append(kwargs)
            if self.revoked:
                raise ContentError("read_publication_changed")

        async def acquire(db, identifier, purpose, *, expected_revision, node_id=None):
            assert identifier == self.route.target_location_id
            assert expected_revision == self.route.maintenance_revision
            assert node_id is None
            world = self

            class Permit:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, kind, error, traceback):
                    self.unsettled = bool(
                        getattr(error, "storage_operation_unsettled", False)
                    )
                    self.settled = not self.unsettled
                    world.events.append("permit-exit")

            permit = Permit()
            permit.purpose, permit.settled, permit.unsettled = purpose, False, False
            self.permits.append(permit)
            return permit

        monkeypatch.setattr(service.storage_access, "engine", lambda _: object())
        monkeypatch.setattr(service, "async_sessionmaker", lambda *_a, **_k: session)
        monkeypatch.setattr(publications, "lookup", self.lookup)
        monkeypatch.setattr(publications, "recheck", recheck)
        monkeypatch.setattr(
            publications, "target_for_route", AsyncMock(return_value=self.target)
        )
        monkeypatch.setattr(storage_access, "acquire", acquire)
        self.old_target = AsyncMock(side_effect=AssertionError("A was opened"))
        self.old_gate = AsyncMock(side_effect=AssertionError("A Gate was checked"))
        monkeypatch.setattr(import_storage, "io_target", self.old_target)
        monkeypatch.setattr(import_storage, "check_location", self.old_gate)
        monkeypatch.setattr(
            service.storage_service,
            "verify_upload",
            lambda *_a, **_k: pytest.fail("legacy A verify was opened"),
        )

    @contextmanager
    def client(self):
        self.events.append("client-open")
        try:
            yield self
        finally:
            self.events.append("client-close")
            if self.fail_client_close:
                raise storage_access.mark_unsettled(RuntimeError("private close"))

    def head_object(self, **params):
        self.calls.append(("head", params))
        return response(self.route, **self.head_changes)

    def get_object(self, **params):
        self.calls.append(("get", params))
        self.body = Body(self)
        return response(self.route, Body=self.body, **self.get_changes)

    async def consume(self, observation=None):
        async with service.materialize_staged_object(
            self.db,
            object_key=self.key,
            expected_sha256=SHA,
            staging_ref=self.ref,
            observation=observation,
        ) as staged:
            assert Path(staged.local_path).read_bytes() == RAW
            return staged


@pytest.mark.parametrize("schema", [1, 2])
async def test_saved_stage_reads_exact_b_and_cleans_local_file_without_a(
    monkeypatch, schema
):
    world = World(monkeypatch, schema=schema)
    original, observation = deepcopy(world.ref), {}
    staged = await world.consume(observation)
    assert staged.sha256 == SHA and staged.size_bytes == len(RAW)
    assert world.ref == original and not Path(staged.local_path).exists()
    assert world.lookup.await_args.kwargs == {
        "source_location_id": world.owner.location_id,
        "endpoint_url": world.ref["endpoint_url"],
        "bucket": world.owner.bucket,
        "object_key": world.owner.key(world.relative),
        "sha256": SHA,
        "size_bytes": None,
        "source_version_id": publications.ANY_VERSION,
    }
    assert world.calls == [
        (
            "head",
            {
                "Bucket": world.route.target_bucket,
                "Key": world.route.target_object_key,
                "VersionId": world.route.target_version_id,
            },
        ),
        (
            "get",
            {
                "Bucket": world.route.target_bucket,
                "Key": world.route.target_object_key,
                "VersionId": world.route.target_version_id,
                "IfMatch": world.route.target_etag,
            },
        ),
    ]
    assert observation == {"revision": None, "read_route": world.route}
    assert world.body.closed and all(permit.settled for permit in world.permits)
    assert {permit.purpose for permit in world.permits} == {
        world.route.purpose,
        "mask_format_staging",
    }
    assert world.events[-3:] == ["client-close", "permit-exit", "permit-exit"]
    world.old_target.assert_not_awaited()
    world.old_gate.assert_not_awaited()


async def test_no_mapping_keeps_original_stopped_gate(monkeypatch):
    world = World(monkeypatch)
    world.lookup.return_value = None
    world.old_target.side_effect = None
    world.old_target.return_value = object()
    world.old_gate.side_effect = ContentError("storage_maintenance_unavailable")
    with pytest.raises(service.MaskFormatError) as caught:
        await world.consume()
    assert caught.value.reason == "storage_maintenance_unavailable"
    assert world.permits == [] and world.calls == []
    world.old_gate.assert_awaited_once_with(world.db, world.owner)


async def test_no_mapping_keeps_saved_active_source_and_original_read_permit(
    monkeypatch,
):
    world = World(monkeypatch)
    world.lookup.return_value = None
    world.old_target.side_effect = world.old_gate.side_effect = None
    world.old_target.return_value, world.old_gate.return_value = world.target, 7

    async def run(db, location_id, purpose, callback, *args, **kwargs):
        assert location_id == world.owner.location_id
        assert purpose == "mask_format_staging"
        assert kwargs == {"expected_revision": 7, "timeout": service.STAGING_IO_SECONDS}
        return callback(*args)

    monkeypatch.setattr(storage_access, "run", run)
    observation = {}
    staged = await world.consume(observation)
    assert not Path(staged.local_path).exists()
    assert observation == {"revision": 7, "read_route": None}
    assert world.permits == [] and world.body.closed
    assert world.calls[0] == (
        "head",
        {"Bucket": world.owner.bucket, "Key": world.owner.key(world.relative)},
    )


@pytest.mark.parametrize("stage,attribute", [("head", "VersionId"), ("get", "ETag")])
async def test_wrong_b_response_is_rejected_before_local_delivery(
    monkeypatch, stage, attribute
):
    world = World(monkeypatch)
    getattr(world, stage + "_changes")[attribute] = "foreign-latest"
    with pytest.raises(service.MaskFormatError) as caught:
        await world.consume()
    assert caught.value.reason == "read_publication_changed"
    assert all(permit.settled for permit in world.permits)
    assert world.body is None or world.body.closed


@pytest.mark.parametrize("failure", ["digest", "quota", "stream_quota"])
async def test_mapped_stage_preserves_full_digest_and_bounded_materialization(
    monkeypatch, failure
):
    world = World(monkeypatch)
    if failure == "digest":
        world.payload = b"Saved bytes"
    else:
        monkeypatch.setattr(
            service.settings,
            "mask_format_temp_quota_bytes",
            5 if failure == "quota" else len(RAW),
        )
        if failure == "stream_quota":
            world.payload += b"extra"
    with pytest.raises(service.MaskFormatError) as caught:
        await world.consume()
    assert caught.value.reason == (
        "staged_object_digest_conflict"
        if failure == "digest"
        else "resource_budget_exceeded"
    )
    assert all(permit.settled for permit in world.permits)
    assert world.body is None or world.body.closed


@pytest.mark.parametrize("failure", ["body", "client"])
async def test_unknown_close_keeps_both_physical_responsibilities(monkeypatch, failure):
    world = World(monkeypatch)
    setattr(world, "fail_" + failure + "_close", True)
    try:
        with pytest.raises(RuntimeError) as caught:
            await world.consume()
        assert caught.value.storage_operation_unsettled
        assert all(permit.unsettled and not permit.settled for permit in world.permits)
    finally:
        world.fail_body_close = False
        if world.body is not None and not world.body.closed:
            world.body.close()


@pytest.mark.parametrize("interruption", ["cancel", "withdraw"])
async def test_interruption_drains_real_read_before_closing_and_no_local_delivery(
    monkeypatch, interruption
):
    world = World(monkeypatch)
    world.block_read = True
    delivered = []

    async def consume():
        delivered.append(await world.consume())

    pending = asyncio.create_task(consume())
    try:
        for _ in range(100):
            if world.body is not None:
                break
            await asyncio.sleep(0.01)
        assert world.body is not None
        assert await asyncio.to_thread(world.body.started.wait, 2)
        if interruption == "cancel":
            pending.cancel()
            await asyncio.sleep(0)
            pending.cancel()
        else:
            world.revoked = True
        await asyncio.sleep(0.02)
        assert not pending.done() and not world.body.closed
        assert not any(permit.settled for permit in world.permits)
    finally:
        if world.body is not None:
            world.body.resume.set()
    with pytest.raises(
        asyncio.CancelledError if interruption == "cancel" else service.MaskFormatError
    ) as caught:
        await pending
    if interruption == "withdraw":
        assert caught.value.reason == "read_publication_changed"
    assert world.body.closed and delivered == []
    assert all(permit.settled for permit in world.permits)


async def test_read_proof_cannot_reopen_upload_gate_or_bind_neighbor_address(
    monkeypatch,
):
    world = World(monkeypatch)
    await service._check_staging_read(
        world.db, world.key, world.ref, read_route=world.route, lock=True
    )
    assert world.rechecks[-1] == {"lock": True}
    world.old_gate.side_effect = ContentError("storage_maintenance_unavailable")
    with pytest.raises(service.MaskFormatError) as caught:
        await service.check_staging(world.db, world.key, world.ref)
    assert caught.value.reason == "storage_maintenance_unavailable"
    with pytest.raises(service.MaskFormatError) as caught:
        await service._check_staging_read(
            world.db,
            world.key,
            world.ref,
            read_route=replace(
                world.route, source_object_key=world.route.source_object_key + "x"
            ),
        )
    assert caught.value.reason == "read_publication_changed"


@pytest.mark.parametrize("change", [None, "withdraw", "owner"])
async def test_preflight_rechecks_captured_publication_and_saved_owner_before_batch(
    monkeypatch, change
):
    world = World(monkeypatch)
    _, project_id, user_id, _, _ = world.key.split("/")
    project = SimpleNamespace(id=uuid.UUID(project_id), data_type="image")
    user_id = uuid.UUID(user_id)
    plan = MaskFormatPlan(
        format_id="saved-format",
        direction="import",
        adapter_version="1",
        manifest_version="1",
        media_type="image",
        loss_class="lossless",
        mapping_digest="a" * 64,
        options_digest="b" * 64,
        estimated_objects=0,
        estimated_files=1,
        estimated_bytes=len(RAW),
        plan_digest="c" * 64,
    )
    rows, owner_facts = [], ["saved-owner"]

    async def source(db, object_key, actual_project, actual_user, *, lock=False):
        assert (object_key, actual_project, actual_user) == (
            world.key,
            project.id,
            user_id,
        )
        return world.ref, tuple(owner_facts)

    async def parse(db, *, staged, **kwargs):
        assert Path(staged.local_path).read_bytes() == RAW
        return plan

    async def flush():
        rows[-1].id = uuid.uuid4()

    async def before_publish():
        if change == "withdraw":
            world.revoked = True
            world.lookup.return_value = replace(world.route, publication_revision=99)
        elif change == "owner":
            owner_facts.append("changed")

    monkeypatch.setattr(service, "_upload_source", source)
    monkeypatch.setattr(
        service.registry,
        "get",
        lambda _: SimpleNamespace(
            descriptor=SimpleNamespace(
                import_capability=SimpleNamespace(supported=True),
                media_types=["image"],
                adapter_version="1",
                manifest_version="1",
            ),
            preflight_import=parse,
        ),
    )
    world.db.commit = AsyncMock()
    world.db.add = rows.append
    world.db.flush = flush

    async def preflight():
        return await service.preflight_import(
            world.db,
            project=project,
            user_id=user_id,
            format_id="saved-format",
            staged_object_key=world.key,
            staged_sha256=SHA,
            mapping={},
            options={},
            before_publish=before_publish,
        )

    if change is None:
        result = await preflight()
        assert len(rows) == 1 and rows[0].status == "staged"
        assert rows[0].staging_ref == world.ref and rows[0].staged_sha256 == SHA
        assert rows[0].plan_digest == result.plan.plan_digest == plan.plan_digest
        assert world.rechecks[-1] == {"lock": True}
    else:
        with pytest.raises(service.MaskFormatError) as caught:
            await preflight()
        assert caught.value.reason == (
            "read_publication_changed"
            if change == "withdraw"
            else "staged_object_owner_changed"
        )
        assert rows == []
    assert world.lookup.await_count == 1
    assert world.body.closed and all(permit.settled for permit in world.permits)
