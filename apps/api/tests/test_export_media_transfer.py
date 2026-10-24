"""Real reader/storage algorithms over fake providers; no DB or service I/O."""

import asyncio
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import tempfile
import threading
import uuid

from billiard.exceptions import SoftTimeLimitExceeded
import pytest

from app.services import export_media_storage as physical
from app.services import export_media_transfer as transfer
from app.services import managed_original_reader as reader
from app.services import storage_access
from app.services.content_snapshot import ContentError
from tests.test_export_media_storage import Client, RAW
from tests.test_managed_original_reader import Provider


class BaseLimit(BaseException):
    pass


@dataclass(frozen=True)
class Source:
    endpoint_url: str
    bucket: str
    object_key: str
    sha256: str
    size_bytes: int
    location_id: uuid.UUID
    maintenance_revision: int


class Snapshot:
    def __init__(self):
        self.id, self.created_at = uuid.uuid4(), datetime.now(timezone.utc)
        self.expires_at = self.created_at + timedelta(days=7)
        self.relative = str(uuid.uuid4()) + ".bin"
        self._inventory = {
            "schema": 1,
            "objects": {
                physical.prefix_for(self.id) + self.relative: {
                    "sha256": hashlib.sha256(RAW).hexdigest(),
                    "size_bytes": len(RAW),
                }
            },
        }
        self._ref = physical.make_ref(self.id, bucket="saved-export-media")
        self.inventory_reads = self.ref_reads = 0

    @property
    def inventory(self):
        self.inventory_reads += 1
        raise AssertionError("whole inventory was copied for a single object")

    @property
    def storage_ref(self):
        self.ref_reads += 1
        return deepcopy(self._ref)


class Runtime:
    def __init__(self, monkeypatch):
        self.base, self.engine = object(), object()
        self.snapshot = Snapshot()
        self.source = Source(
            physical._runtime_endpoint(),
            "saved-originals",
            "original/source.pcd",
            hashlib.sha256(RAW).hexdigest(),
            len(RAW),
            uuid.uuid4(),
            7,
        )
        self.sessions, self.permits, self.stages, self.events = [], [], [], []
        self.guard_calls = self.claim_calls = self.resolve_calls = 0
        self.revoke_at = self.source_changes_at = None
        self.provider, self.client = Provider(), Client()
        self.provider.raw = self.provider.actual = RAW
        self.provider.meta.endpoint_url = self.source.endpoint_url
        self.provider.client_close_hook = lambda: self.physical(
            "source_client_close", 1
        )
        self.output_constructs = 0
        self.stage_close_failure = self.settle_failure = None
        self.blocked, self.release = threading.Event(), threading.Event()
        self.block_phase, self.did_block = None, False
        runtime = self

        class Session:
            def __init__(self, *, bind, expire_on_commit, autoflush):
                assert bind is runtime.engine
                assert not expire_on_commit and not autoflush
                self.closed = False
                runtime.sessions.append(self)

            async def close(self):
                await asyncio.sleep(0)
                self.closed = True

        class Permit:
            def __init__(self, location, revision, purpose):
                self.location_id, self.revision, self.purpose = (
                    location,
                    revision,
                    purpose,
                )
                self.settled = False

            async def _settle(self):
                runtime.events.append("settle")
                assert all(stage.file.closed for stage in runtime.stages)
                await asyncio.sleep(0)
                if runtime.settle_failure:
                    raise runtime.settle_failure
                self.settled = True

        async def acquire(owned, location, purpose, *, expected_revision=None):
            assert owned in self.sessions and not owned.closed
            assert purpose == transfer.purpose(self.snapshot)
            if expected_revision is not None:
                assert location == self.source.location_id and expected_revision == 7
                revision = 7
            else:
                assert location == physical.LEGACY_LOCATION_ID
                revision = 11
            permit = Permit(location, revision, purpose)
            self.permits.append(permit)
            return permit

        async def gate(owned, location, *, expected_revision):
            assert owned in self.sessions and not owned.closed
            assert expected_revision == (
                7 if location == self.source.location_id else 11
            )
            return expected_revision

        async def check_claim(owned, snapshot):
            assert (
                owned in self.sessions
                and not owned.closed
                and snapshot is self.snapshot
            )
            self.claim_calls += 1

        original_stage = reader.BinaryStage

        def stage(*args, **kwargs):
            result = original_stage(*args, **kwargs)
            self.stages.append(result)
            actual_close = result.close

            def close():
                self.physical("stage_close", 1)
                self.block("stage_close")
                actual_close()
                if self.stage_close_failure:
                    raise storage_access.mark_unsettled(self.stage_close_failure)

            result.close = close
            return result

        def source_client(**kwargs):
            self.physical("source_constructor", 1)
            assert kwargs == {"timeout_seconds": 30}
            return self.provider

        def output_client():
            self.physical("output_constructor", 2)
            self.output_constructs += 1
            return self.client

        original_put, original_close = self.client.put_object, self.client.close

        def put(**params):
            self.physical("put", 2)
            assert params["Bucket"] == "saved-export-media"
            assert not self.stages[-1].file.closed
            self.block("put")
            return original_put(**params)

        def output_close():
            self.physical("output_client_close", 2)
            self.block("output_client_close")
            return original_close()

        self.client.put_object, self.client.close = put, output_close
        self.provider.head_hook = lambda: self.block("capture")
        monkeypatch.setattr(transfer, "AsyncSession", Session)
        monkeypatch.setattr(storage_access, "engine", lambda _db: self.engine)
        monkeypatch.setattr(storage_access, "acquire", acquire)
        monkeypatch.setattr(storage_access, "check", gate)
        monkeypatch.setattr(transfer.bundles, "check_claim", check_claim)
        monkeypatch.setattr(reader, "BinaryStage", stage)
        monkeypatch.setattr(reader, "create_client", source_client)
        monkeypatch.setattr(physical, "create_client", output_client)
        monkeypatch.setattr(transfer, "_slots", threading.BoundedSemaphore(2))

    def physical(self, event, minimum_permits):
        assert all(session.closed for session in self.sessions)
        assert sum(not permit.settled for permit in self.permits) >= minimum_permits
        assert transfer._slots._value <= 1
        self.events.append(event)

    def block(self, phase):
        if self.block_phase == phase and not self.did_block:
            self.did_block = True
            self.blocked.set()
            assert self.release.wait(3)

    async def guard(self, owned):
        assert owned in self.sessions and not owned.closed
        self.guard_calls += 1
        if self.revoke_at == self.guard_calls:
            raise ContentError("access_revoked", 403)

    async def resolve(self, owned, binding):
        assert owned in self.sessions and not owned.closed
        assert binding == {"frozen": "binding"}
        self.resolve_calls += 1
        return (
            replace(self.source, object_key="different/original.pcd")
            if self.source_changes_at == self.resolve_calls
            else replace(self.source)
        )

    def inspect(self, file):
        self.physical("inspect", 1)
        assert not file.closed and file.read() == RAW
        self.block("inspect")
        return {"point_count": 4}

    async def copy(self, *, inspect=True):
        return await transfer.copy_source(
            self.base,
            self.snapshot,
            binding={"frozen": "binding"},
            relative=self.snapshot.relative,
            resolve_source=self.resolve,
            guard=self.guard,
            inspect=self.inspect if inspect else None,
        )


@pytest.fixture
def runtime(monkeypatch):
    value = Runtime(monkeypatch)
    try:
        yield value
    finally:
        value.release.set()
        for stage in value.stages:
            if not stage.file.closed:
                stage.file.close()


async def until(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(wait(), 2)


async def test_copy_uses_saved_owners_real_conditional_reader_and_full_output_readback(
    runtime, monkeypatch
):
    monkeypatch.setattr(physical.settings, "minio_export_bucket", "new-bucket-trap")
    result = await runtime.copy()
    assert result.original_sha == runtime.source.sha256 and result.size == len(RAW)
    assert result.inspection == {"point_count": 4}
    result.inspection.clear()
    assert result.inspection == {"point_count": 4}
    assert result.gate_revision == 11 and result.url == "signed-saved-object"
    assert result.url not in repr(result)
    assert runtime.snapshot.inventory_reads == 0 and runtime.snapshot.ref_reads == 1
    assert runtime.claim_calls == runtime.guard_calls == runtime.resolve_calls == 7
    assert all(session.closed for session in runtime.sessions)
    assert len({id(session) for session in runtime.sessions}) == len(runtime.sessions)
    assert all(permit.settled for permit in runtime.permits)
    assert {permit.purpose for permit in runtime.permits} == {
        "export_media_" + runtime.snapshot.id.hex
    }
    assert (
        runtime.events.index("output_client_close")
        < runtime.events.index("stage_close")
        < runtime.events.index("settle")
    )
    assert runtime.provider.closed == runtime.client.close_count == 1
    assert all(body.closed for body in runtime.provider.bodies + runtime.client.bodies)
    assert all(
        params["Bucket"] == "saved-originals"
        for method, params in runtime.provider.calls
        if method in {"get", "head"}
    )
    assert any(
        method == "get" and params["IfMatch"] == '"aap-impossible-etag"'
        for method, params in runtime.provider.calls
    )
    assert all(
        params["Bucket"] == "saved-export-media"
        for method, params in runtime.client.calls
        if method != "sign"
    )
    assert len(runtime.stages) == 1 and runtime.stages[0].file.closed
    assert transfer._slots._value == 2


@pytest.mark.parametrize("boundary", range(1, 8))
async def test_guard_revocation_at_every_boundary_never_returns_delivery(
    runtime, boundary
):
    runtime.revoke_at = boundary
    with pytest.raises(ContentError) as failure:
        await runtime.copy()
    assert failure.value.code == "access_revoked"
    assert all(stage.file.closed for stage in runtime.stages)
    assert all(permit.settled for permit in runtime.permits)
    assert all(session.closed for session in runtime.sessions)
    assert transfer._slots._value == 2
    if boundary < 6:
        assert not runtime.client.calls


async def test_current_source_identity_change_with_same_sha_is_rejected(runtime):
    runtime.source_changes_at = 3
    with pytest.raises(ContentError) as failure:
        await runtime.copy()
    assert failure.value.code == "export_media_source_changed"
    assert not runtime.output_constructs and all(
        permit.settled for permit in runtime.permits
    )
    assert transfer._slots._value == 2


@pytest.mark.parametrize(
    "phase", ["capture", "inspect", "put", "output_client_close", "stage_close"]
)
async def test_repeated_cancellation_waits_for_real_work_close_and_keeps_admission(
    runtime, phase
):
    runtime.block_phase = phase
    operation = asyncio.create_task(runtime.copy())
    try:
        await until(runtime.blocked.is_set)
        operation.cancel()
        await asyncio.sleep(0)
        operation.cancel()
        await asyncio.sleep(0)
        assert not operation.done()
        assert transfer._slots._value == 1
        assert not any(permit.settled for permit in runtime.permits)
        runtime.release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
    finally:
        runtime.release.set()
    assert all(stage.file.closed for stage in runtime.stages)
    assert all(permit.settled for permit in runtime.permits)
    assert transfer._slots._value == 2


@pytest.mark.parametrize("interrupt", [SoftTimeLimitExceeded, BaseLimit])
@pytest.mark.parametrize("phase", ["capture", "inspect", "put"])
async def test_ordinary_and_base_soft_limits_drain_repeated_interruptions(
    runtime, monkeypatch, interrupt, phase
):
    runtime.block_phase = phase
    original_submit, original_shield = transfer._submit, asyncio.shield

    def submit(call, *args, **kwargs):
        pending = original_submit(call, *args, **kwargs)
        pending.test_phase = (
            "capture"
            if call is reader._capture
            else "put"
            if call.__name__ == "write"
            else call.__name__
        )
        return pending

    count = 0

    async def shield(pending):
        nonlocal count
        if getattr(pending, "test_phase", None) == phase and count < 2:
            count += 1
            await until(runtime.blocked.is_set)
            raise interrupt()
        return await original_shield(pending)

    monkeypatch.setattr(transfer, "_submit", submit)
    monkeypatch.setattr(transfer.asyncio, "shield", shield)
    operation = asyncio.create_task(runtime.copy())
    try:
        await until(lambda: count == 2)
        assert not operation.done() and transfer._slots._value == 1
        runtime.release.set()
        with pytest.raises(interrupt):
            await operation
    finally:
        runtime.release.set()
    assert all(stage.file.closed for stage in runtime.stages)
    assert all(permit.settled for permit in runtime.permits)
    assert transfer._slots._value == 2


@pytest.mark.parametrize("unknown_close", [False, True])
async def test_late_capture_stage_is_closed_before_interruption_can_return(
    runtime, monkeypatch, unknown_close
):
    def late(spec, maximum, seconds, deadline, stop, release, **options):
        runtime.physical("late_capture", 1)
        assert maximum == physical.MAX_OBJECT_BYTES and seconds == 300
        assert options == {
            "transport_timeout_seconds": 30,
            "retain_on_close_error": True,
        }
        runtime.blocked.set()
        assert runtime.release.wait(3)
        assert stop.is_set()
        file = tempfile.TemporaryFile()
        file.write(RAW)
        file.seek(0)
        return reader.BinaryStage(
            file,
            spec.sha256,
            spec.size_bytes,
            {},
            _release=release,
            _retain_on_close_error=True,
        )

    monkeypatch.setattr(reader, "_capture", late)
    if unknown_close:
        runtime.stage_close_failure = OSError("unknown late stage close")
    operation = asyncio.create_task(runtime.copy())
    try:
        await until(runtime.blocked.is_set)
        operation.cancel()
        await asyncio.sleep(0)
        assert not operation.done() and transfer._slots._value == 1
        runtime.release.set()
        with pytest.raises(asyncio.CancelledError) as failure:
            await operation
    finally:
        runtime.release.set()
    assert len(runtime.stages) == 1 and runtime.stages[0].file.closed
    assert (
        bool(getattr(failure.value, "storage_operation_unsettled", False))
        == unknown_close
    )
    assert transfer._slots._value == (1 if unknown_close else 2)
    assert all(permit.settled != unknown_close for permit in runtime.permits)
    assert not runtime.output_constructs


@pytest.mark.parametrize(
    "where",
    [
        "source_constructor",
        "source_body",
        "source_close",
        "output_constructor",
        "output_close",
        "stage_close",
    ],
)
@pytest.mark.parametrize("exception", [SoftTimeLimitExceeded, BaseLimit])
async def test_unknown_constructor_or_close_keeps_both_ledger_and_admission(
    runtime, monkeypatch, where, exception
):
    failure = exception()
    if where == "source_constructor":

        def source(**kwargs):
            raise failure

        monkeypatch.setattr(reader, "create_client", source)
    elif where == "source_body":
        runtime.provider.body_close_error = failure
    elif where == "source_close":
        runtime.provider.client_close_error = failure
    elif where == "output_constructor":

        def output():
            raise failure

        monkeypatch.setattr(physical, "create_client", output)
    elif where == "output_close":
        runtime.client.close_failure = failure
    else:
        runtime.stage_close_failure = failure
    with pytest.raises(BaseException) as caught:
        await runtime.copy()
    assert caught.value.storage_operation_unsettled
    assert runtime.permits and not any(permit.settled for permit in runtime.permits)
    assert transfer._slots._value == 1
    assert all(stage.file.closed for stage in runtime.stages)


async def test_two_slots_remain_held_through_output_work_and_third_source_gets_no_bytes(
    runtime,
):
    admissions = [transfer.Admission(), transfer.Admission()]
    try:
        with pytest.raises(ContentError) as failure:
            await runtime.copy()
        assert failure.value.status == 429
        assert not runtime.provider.calls and not runtime.output_constructs
        assert all(permit.settled for permit in runtime.permits)
    finally:
        for admission in admissions:
            admission.finish(False)
    assert transfer._slots._value == 2


async def test_no_inspection_and_single_entry_owner_are_supported(runtime):
    result = await runtime.copy(inspect=False)
    assert result.inspection is None and result.size == len(RAW)
    assert runtime.snapshot.inventory_reads == 0


async def test_settlement_failure_keeps_remaining_responsibility_and_admission(runtime):
    runtime.settle_failure = OSError("database settlement uncertain")
    with pytest.raises(OSError) as failure:
        await runtime.copy()
    assert failure.value.storage_operation_unsettled
    assert not any(permit.settled for permit in runtime.permits)
    assert transfer._slots._value == 1
    assert all(stage.file.closed for stage in runtime.stages)


async def test_known_late_settlement_success_continues_remaining_cleanup(runtime):
    runtime.block_phase = "put"
    entered, release = asyncio.Event(), asyncio.Event()
    operation = asyncio.create_task(runtime.copy())
    try:
        await until(runtime.blocked.is_set)
        permit = runtime.permits[-1]
        original = permit._settle

        async def slow_settlement():
            entered.set()
            await release.wait()
            await original()

        permit._settle = slow_settlement
        runtime.release.set()
        await asyncio.wait_for(entered.wait(), 2)
        operation.cancel()
        await asyncio.sleep(0)
        operation.cancel()
        await asyncio.sleep(0)
        assert not operation.done() and transfer._slots._value == 1
        release.set()
        with pytest.raises(asyncio.CancelledError) as failure:
            await operation
        assert not getattr(failure.value, "storage_operation_unsettled", False)
    finally:
        runtime.release.set()
        release.set()
    assert all(permit.settled for permit in runtime.permits)
    assert transfer._slots._value == 2


async def test_late_acquired_permit_is_settled_before_cancellation_returns(
    runtime, monkeypatch
):
    entered, release = asyncio.Event(), asyncio.Event()
    original = storage_access.acquire

    async def slow_acquire(*args, **kwargs):
        permit = await original(*args, **kwargs)
        entered.set()
        await release.wait()
        return permit

    monkeypatch.setattr(storage_access, "acquire", slow_acquire)
    operation = asyncio.create_task(runtime.copy())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        operation.cancel()
        await asyncio.sleep(0)
        assert not operation.done() and not runtime.provider.calls
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
    finally:
        release.set()
    assert len(runtime.permits) == 1 and runtime.permits[0].settled
    assert all(session.closed for session in runtime.sessions)
    assert transfer._slots._value == 2


async def test_output_gate_change_refuses_constructor_and_settles_known_capture(
    runtime, monkeypatch
):
    original = storage_access.check

    async def changed(owned, location, *, expected_revision):
        if expected_revision == 11:
            raise ContentError("storage_maintenance_changed")
        return await original(owned, location, expected_revision=expected_revision)

    monkeypatch.setattr(storage_access, "check", changed)
    with pytest.raises(ContentError) as failure:
        await runtime.copy()
    assert failure.value.code == "storage_maintenance_changed"
    assert not runtime.output_constructs
    assert all(stage.file.closed for stage in runtime.stages)
    assert all(permit.settled for permit in runtime.permits)
    assert transfer._slots._value == 2


@pytest.mark.parametrize("fault", ["unknown_relative", "different_sha"])
async def test_unregistered_or_mismatched_output_cannot_trigger_source_io(
    runtime, fault
):
    if fault == "unknown_relative":
        runtime.snapshot.relative = str(uuid.uuid4()) + ".bin"
    else:
        runtime.source = replace(runtime.source, sha256="f" * 64)
    with pytest.raises(ContentError):
        await runtime.copy()
    assert not runtime.permits and not runtime.provider.calls
    assert transfer._slots._value == 2


async def read_saved(runtime, read=None):
    return await transfer.read_copy(
        runtime.base,
        runtime.snapshot,
        relative=runtime.snapshot.relative,
        guard=runtime.guard,
        read=read or (lambda file: file.read()),
    )


async def test_read_copy_uses_only_saved_owner_full_reader_and_building_guard(
    runtime, monkeypatch
):
    monkeypatch.setattr(physical.settings, "minio_export_bucket", "new-bucket-trap")

    def read(file):
        runtime.physical("read_copy", 1)
        assert not file.closed and transfer._slots._value == 1
        return file.read()

    assert await read_saved(runtime, read) == RAW
    assert runtime.resolve_calls == runtime.output_constructs == 0
    assert runtime.claim_calls == runtime.guard_calls == 5
    assert runtime.snapshot.inventory_reads == 0
    assert len(runtime.permits) == 1 and runtime.permits[0].settled
    assert runtime.permits[0].purpose == transfer.purpose(runtime.snapshot)
    assert runtime.provider.closed == 1 and not runtime.client.calls
    assert all(stage.file.closed for stage in runtime.stages)
    assert all(session.closed for session in runtime.sessions)
    for method, params in runtime.provider.calls:
        if method in {"head", "get"}:
            assert params["Bucket"] == "saved-export-media"
            assert (
                params["Key"]
                == physical.prefix_for(runtime.snapshot.id) + runtime.snapshot.relative
            )
    assert transfer._slots._value == 2


@pytest.mark.parametrize("boundary", range(1, 6))
async def test_read_copy_rechecks_authority_at_every_boundary(runtime, boundary):
    runtime.revoke_at = boundary
    with pytest.raises(ContentError) as failure:
        await read_saved(runtime)
    assert failure.value.code == "access_revoked"
    assert runtime.resolve_calls == 0
    assert all(permit.settled for permit in runtime.permits)
    assert all(stage.file.closed for stage in runtime.stages)
    assert transfer._slots._value == 2


async def test_read_copy_repeated_cancellation_keeps_stage_until_callback_really_finishes(
    runtime,
):
    runtime.block_phase = "read_copy"

    def read(file):
        runtime.physical("read_copy", 1)
        runtime.block("read_copy")
        assert not file.closed
        return file.read()

    operation = asyncio.create_task(read_saved(runtime, read))
    try:
        await until(runtime.blocked.is_set)
        operation.cancel()
        await asyncio.sleep(0)
        operation.cancel()
        await asyncio.sleep(0)
        assert not operation.done() and not runtime.stages[0].file.closed
        assert transfer._slots._value == 1 and not runtime.permits[0].settled
        runtime.release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
    finally:
        runtime.release.set()
    assert runtime.stages[0].file.closed and runtime.permits[0].settled
    assert transfer._slots._value == 2


@pytest.mark.parametrize("where", ["constructor", "client_close", "stage_close"])
async def test_read_copy_unknown_physical_handles_keep_owner_and_slot(
    runtime, monkeypatch, where
):
    if where == "constructor":

        def fail(**_kwargs):
            raise SoftTimeLimitExceeded()

        monkeypatch.setattr(reader, "create_client", fail)
    elif where == "client_close":
        runtime.provider.client_close_error = BaseLimit()
    else:
        runtime.stage_close_failure = OSError("unknown saved-copy stage close")
    with pytest.raises(BaseException) as failure:
        await read_saved(runtime)
    assert failure.value.storage_operation_unsettled
    assert len(runtime.permits) == 1 and not runtime.permits[0].settled
    assert transfer._slots._value == 1
    assert all(stage.file.closed for stage in runtime.stages)


async def test_read_copy_rejects_tampered_bytes_before_callback(runtime):
    runtime.provider.actual = b"x" * len(RAW)
    called = []
    with pytest.raises(ContentError) as failure:
        await read_saved(runtime, lambda file: called.append(file.read()))
    assert failure.value.code == "content_mismatch"
    assert called == [] and runtime.resolve_calls == 0
    assert runtime.permits[0].settled and transfer._slots._value == 2


async def test_read_copy_requires_live_building_claim_and_never_reads_original(
    runtime, monkeypatch
):
    async def reject_claim(owned, snapshot):
        raise ContentError("export_media_bundle_lease_lost")

    monkeypatch.setattr(transfer.bundles, "check_claim", reject_claim)
    with pytest.raises(ContentError) as failure:
        await read_saved(runtime)
    assert failure.value.code == "export_media_bundle_lease_lost"
    assert not runtime.permits and not runtime.provider.calls
    assert runtime.resolve_calls == 0 and transfer._slots._value == 2
