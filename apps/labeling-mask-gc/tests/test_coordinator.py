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
    def __init__(self, claims=(), *, fail_inspect=False):
        self.calls = []
        self.claims = tuple(claims)
        self.fail_inspect = fail_inspect

    async def call(self, operation, request):
        self.calls.append((operation, request))
        if operation == "inspect_mask_gc_page":
            if self.fail_inspect:
                raise c.MaskGcFailure("storage_unavailable")
            return c.InspectMaskGcPageResult(
                scanned=len(request.page.items),
                eligible=0,
                retained=len(request.page.items),
                referenced=7 if request.include_reference_count else None,
            )
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
    def __init__(
        self,
        *,
        fail_scan=False,
        fail_execute_ids=(),
        fail_status_ids=(),
        pages=None,
    ):
        self.fail_scan = fail_scan
        self.fail_execute_ids = set(fail_execute_ids)
        self.fail_status_ids = set(fail_status_ids)
        self.execute_ids = []
        self.status_ids = []
        self.scan_requests = []
        self.pages = tuple(pages or ())
        self.page_index = 0
        self.page = byte.GcScanPage(items=(), after_key=KEY_B, complete=True)

    def scan(self, request):
        self.scan_requests.append(request)
        if self.fail_scan:
            raise byte.MaskGcFailure("storage_unavailable")
        if self.pages:
            page = self.pages[self.page_index]
            self.page_index += 1
            return page
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
        "inspect_mask_gc_page",
        "stage_mask_gc_page",
    ]
    assert names[3] == "claim_mask_gc_deletes"
    assert storage.execute_ids == [OP_A, OP_B]
    assert storage.status_ids == [OP_A]
    assert [
        request.operation_id
        for name, request in data.calls
        if name == "finish_mask_gc_delete"
    ] == [OP_A, OP_B]
    assert stats.delete_claims == 2
    assert stats.eligible == stats.staged == 0
    assert stats.referenced == 7
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
async def test_inspection_failure_does_not_stop_durable_staging_or_reconciliation():
    claim = _claim(OP_B, KEY_B)
    data = FakeData((claim,), fail_inspect=True)
    storage = FakeStorage()

    stats = await run_cycle(data, storage, worker_id=WORKER_ID, delete_limit=1)

    names = [operation for operation, _request in data.calls]
    assert "stage_mask_gc_page" in names
    assert "claim_mask_gc_deletes" in names
    assert stats.data_errors == 1
    assert stats.scan_pages == 1
    assert stats.referenced is None


@pytest.mark.asyncio
async def test_dry_run_continues_with_physical_cursor_without_mutating_calls():
    candidate_a = byte.GcCandidate(
        object_key=KEY_A,
        etag="a-etag",
        last_modified=STAMP,
        byte_size=10,
    )
    candidate_b = byte.GcCandidate(
        object_key=KEY_B,
        etag="b-etag",
        last_modified=STAMP,
        byte_size=20,
    )
    cursor_a = KEY_A + "~cursor"
    cursor_b = KEY_B + "~cursor"
    pages = (
        byte.GcScanPage(items=(candidate_a,), after_key=cursor_a, complete=False),
        byte.GcScanPage(items=(candidate_b,), after_key=cursor_b, complete=False),
    )

    class PreviewData(FakeData):
        async def call(self, operation, request):
            self.calls.append((operation, request))
            assert operation == "inspect_mask_gc_page"
            count = len(request.page.items)
            retained = (
                count
                if request.page.items and request.page.items[0].object_key == KEY_A
                else 0
            )
            return c.InspectMaskGcPageResult(
                scanned=count,
                eligible=count - retained,
                retained=retained,
                referenced=9 if request.include_reference_count else None,
            )

    data = PreviewData()
    storage = FakeStorage(pages=pages)
    stats = await run_cycle(
        data,
        storage,
        worker_id=WORKER_ID,
        scan_pages=2,
        delete_limit=1,
        dry_run=True,
    )

    assert stats.dry_run is True
    assert (stats.scanned, stats.eligible, stats.skipped, stats.staged) == (2, 1, 1, 0)
    assert stats.referenced == 9
    assert stats.next_after_key == cursor_b
    assert stats.scan_complete is False
    assert [request.after_key for request in storage.scan_requests] == [None, cursor_a]
    assert [name for name, _request in data.calls] == [
        "inspect_mask_gc_page",
        "inspect_mask_gc_page",
    ]
    assert [request.include_reference_count for _name, request in data.calls] == [
        True,
        False,
    ]
    assert storage.execute_ids == []
    assert storage.status_ids == []

    final = byte.GcScanPage(items=(), after_key=None, complete=True)
    continuation = FakeStorage(pages=(final,))
    complete_stats = await run_cycle(
        data,
        continuation,
        worker_id=WORKER_ID,
        scan_pages=1,
        dry_run=True,
        after_key=cursor_b,
    )
    assert continuation.scan_requests[0].after_key == cursor_b
    assert complete_stats.scan_complete is True
    assert complete_stats.next_after_key is None
    assert [name for name, _request in data.calls] == [
        "inspect_mask_gc_page",
        "inspect_mask_gc_page",
        "inspect_mask_gc_page",
    ]


@pytest.mark.asyncio
async def test_normal_eligible_uses_staged_count_and_reports_durable_checkpoint():
    candidate = byte.GcCandidate(
        object_key=KEY_A,
        etag="opaque-etag",
        last_modified=STAMP,
        byte_size=10,
    )
    cursor = KEY_A + "~physical-cursor"
    storage = FakeStorage(
        pages=(byte.GcScanPage(items=(candidate,), after_key=cursor, complete=False),)
    )
    data = FakeData()

    stats = await run_cycle(data, storage, worker_id=WORKER_ID)

    assert stats.staged == stats.eligible == 1
    assert stats.skipped == 0
    assert stats.next_after_key == cursor
    assert stats.scan_complete is False


@pytest.mark.asyncio
async def test_dry_run_failure_keeps_last_completed_checkpoint_and_never_mutates():
    page = byte.GcScanPage(items=(), after_key=KEY_A, complete=False)

    class FailsSecondScan(FakeStorage):
        def scan(self, request):
            self.scan_requests.append(request)
            if len(self.scan_requests) == 1:
                return page
            raise byte.MaskGcFailure("storage_unavailable")

    storage = FailsSecondScan()
    data = FakeData()
    stats = await run_cycle(
        data,
        storage,
        worker_id=WORKER_ID,
        scan_pages=2,
        dry_run=True,
    )
    assert stats.scan_pages == 1
    assert stats.scan_errors == 1
    assert stats.next_after_key == KEY_A
    assert stats.scan_complete is False
    assert [name for name, _request in data.calls] == ["inspect_mask_gc_page"]


@pytest.mark.asyncio
async def test_preview_rejects_a_final_empty_page_that_moves_the_cursor_backwards():
    storage = FakeStorage(
        pages=(byte.GcScanPage(items=(), after_key=KEY_A, complete=True),)
    )
    data = FakeData()
    stats = await run_cycle(
        data, storage, worker_id=WORKER_ID, dry_run=True, after_key=KEY_B
    )
    assert stats.scan_errors == 1 and stats.scan_pages == 0
    assert stats.next_after_key == KEY_B and stats.scan_complete is False
    assert data.calls == []


@pytest.mark.asyncio
async def test_run_cycle_validates_typed_bounds_and_cursor_mode_before_io():
    data = FakeData()
    storage = FakeStorage()
    with pytest.raises(c.MaskGcFailure) as error:
        await run_cycle(
            data,
            storage,
            worker_id=WORKER_ID,
            dry_run=1,
        )
    assert error.value.code == "invalid_request"
    with pytest.raises(c.MaskGcFailure) as error:
        await run_cycle(data, storage, worker_id=WORKER_ID, after_key=KEY_A)
    assert error.value.code == "invalid_request"
    assert data.calls == []
    assert storage.scan_requests == []


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
