from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import threading
from uuid import UUID

import pytest

from aap_assets_mask_content import gc_contract as byte
from aap_labeling_mask_gc import contract as c
from aap_labeling_mask_gc.coordinator import run_cycle

KEY_A = "raster-masks/sha256/a0/a0/" + "a0a0" + "a" * 60 + ".json"
KEY_B = "raster-masks/sha256/b0/b0/" + "b0b0" + "b" * 60 + ".json"
STAMP = datetime(2026, 1, 1, tzinfo=timezone.utc)
SWEEP_ID = UUID("00000000-0000-0000-0000-000000000001")
SWEEP_TOKEN = UUID("00000000-0000-0000-0000-000000000002")
WORKER_ID = UUID("00000000-0000-0000-0000-000000000003")
OP_A = UUID("00000000-0000-0000-0000-000000000004")
OP_B = UUID("00000000-0000-0000-0000-000000000005")


def _sweep(*, after_key=None, complete=False):
    return c.SweepClaim(
        sweep_id=SWEEP_ID,
        generation=1,
        lease_token=SWEEP_TOKEN,
        lease_expires_at=datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc),
        cutoff=datetime(2025, 12, 31, tzinfo=timezone.utc),
        after_key=after_key,
        complete=complete,
    )


def _claim(operation_id, key):
    return c.DeleteClaim(
        operation_id=operation_id,
        object_key=key,
        etag=f"etag-{operation_id.int}",
        last_modified=STAMP,
        byte_size=100,
        epoch=1,
        generation=1,
        lease_token=UUID(int=operation_id.int + 10),
        lease_expires_at=datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc),
    )


class FakeData:
    def __init__(self, claims=()):
        self.calls = []
        self.claims = tuple(claims)

    async def call(self, operation, request):
        self.calls.append((operation, request))
        if operation == "claim_mask_gc_sweep":
            return c.ClaimMaskGcSweepResult(sweep=_sweep())
        if operation == "stage_mask_gc_page":
            return c.StageMaskGcPageResult(
                sweep=_sweep(
                    after_key=request.page.after_key, complete=request.page.complete
                ),
                scanned=len(request.page.items),
                staged=len(request.page.items),
                skipped=0,
            )
        if operation == "claim_mask_gc_deletes":
            return c.ClaimMaskGcDeletesResult(items=self.claims)
        if operation == "finish_mask_gc_delete":
            state = (
                "completed"
                if request.receipt is not None and request.receipt.state == "completed"
                else request.receipt.state
                if request.receipt is not None
                and request.receipt.state in {"pending", "indeterminate"}
                else "pending"
            )
            return c.FinishMaskGcDeleteResult(
                operation_id=request.operation_id,
                state=state,
                completed=state == "completed",
            )
        raise AssertionError(operation)


class FakeStorage:
    def __init__(self, *, fail_scan=False, fail_execute_ids=(), fail_status_ids=()):
        self.fail_scan = fail_scan
        self.fail_execute_ids = set(fail_execute_ids)
        self.fail_status_ids = set(fail_status_ids)
        self.execute_ids = []
        self.status_ids = []
        self.page = byte.GcScanPage(items=(), after_key=KEY_B, complete=True)

    def scan(self, request):
        if self.fail_scan:
            raise byte.MaskGcFailure("storage_unavailable")
        return self.page

    def execute(self, request):
        self.execute_ids.append(request.operation_id)
        if request.operation_id in self.fail_execute_ids:
            raise byte.MaskGcFailure("storage_unavailable")
        return byte.DeleteOperationStatus(
            operation_id=request.operation_id,
            request=request,
            state="completed",
            outcome="deleted",
        )

    def status(self, request):
        self.status_ids.append(request.operation_id)
        if request.operation_id in self.fail_status_ids:
            raise byte.MaskGcFailure("storage_unavailable")
        expected = next(
            (
                claim
                for claim in (self.claim_a, self.claim_b)
                if claim.operation_id == request.operation_id
            ),
            None,
        )
        if expected is None:
            return byte.DeleteOperationStatus(
                operation_id=request.operation_id,
                request=None,
                state="not_found",
                outcome=None,
            )
        delete_request = byte.DeleteOperationRequest(
            operation_id=expected.operation_id,
            object_key=expected.object_key,
            etag=expected.etag,
            last_modified=expected.last_modified,
        )
        return byte.DeleteOperationStatus(
            operation_id=request.operation_id,
            request=delete_request,
            state="indeterminate",
            outcome=None,
        )


@pytest.mark.asyncio
async def test_stages_inventory_then_continues_after_ambiguous_item():
    claims = (_claim(OP_A, KEY_A), _claim(OP_B, KEY_B))
    data = FakeData(claims)
    storage = FakeStorage(fail_execute_ids={OP_A})
    storage.claim_a, storage.claim_b = claims

    stats = await run_cycle(data, storage, worker_id=WORKER_ID, delete_limit=2)

    names = [operation for operation, _request in data.calls]
    assert names[:3] == [
        "claim_mask_gc_sweep",
        "stage_mask_gc_page",
        "claim_mask_gc_deletes",
    ]
    assert storage.execute_ids == [OP_A, OP_B]
    assert storage.status_ids == [OP_A]
    assert [
        request.operation_id
        for name, request in data.calls
        if name == "finish_mask_gc_delete"
    ] == [OP_A, OP_B]
    assert stats.delete_claims == 2
    assert stats.indeterminate == 1
    assert stats.completed == 1
    assert stats.deleted == 1


@pytest.mark.asyncio
async def test_inventory_failure_does_not_skip_existing_delete_reconciliation():
    claim = _claim(OP_B, KEY_B)
    data = FakeData((claim,))
    storage = FakeStorage(fail_scan=True)

    stats = await run_cycle(data, storage, worker_id=WORKER_ID, delete_limit=1)

    names = [operation for operation, _request in data.calls]
    assert "claim_mask_gc_deletes" in names
    assert "finish_mask_gc_delete" in names
    assert stats.scan_errors == 1
    assert stats.completed == 1


@pytest.mark.asyncio
async def test_unavailable_status_is_durably_recorded_without_terminal_receipt():
    claim = _claim(OP_A, KEY_A)
    data = FakeData((claim,))
    storage = FakeStorage(
        fail_execute_ids={OP_A},
        fail_status_ids={OP_A},
    )

    stats = await run_cycle(data, storage, worker_id=WORKER_ID, delete_limit=1)

    finish = [
        request for name, request in data.calls if name == "finish_mask_gc_delete"
    ][0]
    assert finish.receipt is None
    assert finish.error_code == "peer_unavailable"
    assert stats.durable_uncertainties == 1
    assert stats.completed == 0


@pytest.mark.asyncio
async def test_missing_journal_after_two_failed_executes_records_uncertainty():
    claim = _claim(OP_A, KEY_A)
    data = FakeData((claim,))

    class MissingJournal(FakeStorage):
        def status(self, request):
            self.status_ids.append(request.operation_id)
            return byte.DeleteOperationStatus(
                operation_id=request.operation_id,
                request=None,
                state="not_found",
                outcome=None,
            )

    storage = MissingJournal(fail_execute_ids={OP_A})
    stats = await run_cycle(data, storage, worker_id=WORKER_ID, delete_limit=1)
    finish = [
        request for name, request in data.calls if name == "finish_mask_gc_delete"
    ][0]
    assert storage.execute_ids == [OP_A, OP_A]
    assert finish.receipt is None
    assert finish.error_code == "peer_unavailable"
    assert stats.durable_uncertainties == 1
    assert stats.finish_errors == 0


@pytest.mark.asyncio
async def test_mismatched_status_identity_records_uncertainty_and_moves_on():
    claims = (_claim(OP_A, KEY_A), _claim(OP_B, KEY_B))
    data = FakeData(claims)

    class WrongReceiptStorage(FakeStorage):
        def status(self, request):
            self.status_ids.append(request.operation_id)
            wrong = byte.DeleteOperationRequest(
                operation_id=request.operation_id,
                object_key=KEY_B,
                etag="wrong-etag",
                last_modified=STAMP,
            )
            return byte.DeleteOperationStatus(
                operation_id=request.operation_id,
                request=wrong,
                state="completed",
                outcome="deleted",
            )

    storage = WrongReceiptStorage(fail_execute_ids={OP_A})
    storage.claim_a, storage.claim_b = claims

    stats = await run_cycle(data, storage, worker_id=WORKER_ID, delete_limit=2)

    finishes = [
        request for name, request in data.calls if name == "finish_mask_gc_delete"
    ]
    assert finishes[0].receipt is None
    assert finishes[0].error_code == "invalid_response"
    assert finishes[1].operation_id == OP_B
    assert stats.completed == 1


@pytest.mark.asyncio
async def test_cancellation_keeps_admission_until_sync_byte_call_finishes():
    started = threading.Event()
    release = threading.Event()

    class SlowStorage(FakeStorage):
        def scan(self, request):
            started.set()
            release.wait(timeout=2)
            return super().scan(request)

    data = FakeData()
    storage = SlowStorage()
    admission = asyncio.Lock()

    async def admitted_cycle():
        async with admission:
            return await run_cycle(data, storage, worker_id=WORKER_ID)

    cycle = asyncio.create_task(admitted_cycle())
    assert await asyncio.to_thread(started.wait, 2)
    cycle.cancel()
    await asyncio.sleep(0)
    assert admission.locked()
    assert not cycle.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await cycle
    assert not admission.locked()
