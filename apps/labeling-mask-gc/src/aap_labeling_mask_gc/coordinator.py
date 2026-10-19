"""Bounded Labeling orchestration across native data and byte-owner ports."""

from __future__ import annotations

import asyncio
import inspect
from typing import Protocol
from uuid import UUID

from . import contract as c
from aap_assets_mask_content import gc_contract as byte


class NativeMaskGcData(Protocol):
    async def call(self, operation: str, request: c.Value) -> c.Value: ...


class MaskGcStorage(Protocol):
    def scan(self, request: byte.GcScanRequest) -> byte.GcScanPage: ...

    def execute(
        self, request: byte.DeleteOperationRequest
    ) -> byte.DeleteOperationStatus: ...

    def status(
        self, request: byte.DeleteStatusRequest
    ) -> byte.DeleteOperationStatus: ...


async def _data_call(data: NativeMaskGcData, operation: str, request: c.Value):
    result = data.call(operation, request)
    if inspect.isawaitable(result):
        task = asyncio.ensure_future(result)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await task
            except Exception:
                pass
            raise
    return result


async def _storage_call(storage: MaskGcStorage, operation: str, request):
    method = getattr(storage, operation)
    work = (
        method(request)
        if inspect.iscoroutinefunction(method)
        else asyncio.to_thread(method, request)
    )
    task = asyncio.ensure_future(work)
    try:
        result = await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except Exception:
            pass
        raise
    if inspect.isawaitable(result):
        followup = asyncio.ensure_future(result)
        try:
            return await asyncio.shield(followup)
        except asyncio.CancelledError:
            try:
                await followup
            except Exception:
                pass
            raise
    return result


def _expected_delete(claim: c.DeleteClaim) -> byte.DeleteOperationRequest:
    return byte.DeleteOperationRequest(
        operation_id=claim.operation_id,
        object_key=claim.object_key,
        etag=claim.etag,
        last_modified=claim.last_modified,
    )


def _validate_status(
    value: object,
    *,
    operation_id: UUID,
    expected_request: byte.DeleteOperationRequest,
    allow_not_found: bool,
) -> byte.DeleteOperationStatus:
    try:
        status = byte.DeleteOperationStatus.model_validate(value)
        if status.operation_id != operation_id:
            raise ValueError("status operation does not match claim")
        if status.state == "not_found":
            if not allow_not_found:
                raise ValueError("execute did not establish the operation")
            return status
        if status.request != expected_request:
            raise ValueError("status request does not match claim identity")
        return status
    except (TypeError, ValueError):
        raise byte.MaskGcFailure("invalid_response") from None


def _controlled_error(exc: BaseException) -> c.DeleteErrorCode:
    if isinstance(exc, byte.MaskGcFailure) and exc.code == "invalid_response":
        return "invalid_response"
    return "peer_unavailable"


async def _resolve_delete(
    storage: MaskGcStorage, claim: c.DeleteClaim
) -> tuple[byte.DeleteOperationStatus | None, c.DeleteErrorCode | None]:
    try:
        expected = _expected_delete(claim)
    except (TypeError, ValueError):
        return None, "invalid_response"
    try:
        status = await _storage_call(storage, "execute", expected)
        return (
            _validate_status(
                status,
                operation_id=claim.operation_id,
                expected_request=expected,
                allow_not_found=False,
            ),
            None,
        )
    except Exception as execute_error:
        error_code = _controlled_error(execute_error)

    status_request = byte.DeleteStatusRequest(operation_id=claim.operation_id)
    try:
        status = await _storage_call(storage, "status", status_request)
        status = _validate_status(
            status,
            operation_id=claim.operation_id,
            expected_request=expected,
            allow_not_found=True,
        )
    except Exception as status_error:
        if _controlled_error(status_error) == "invalid_response":
            error_code = "invalid_response"
        return None, error_code

    if status.state != "not_found":
        return status, None

    # A not_found status means the journal claim never became visible. Retrying
    # with the same stable ID is safe; the byte owner conditionally claims once.
    try:
        retried = await _storage_call(storage, "execute", expected)
        retried = _validate_status(
            retried,
            operation_id=claim.operation_id,
            expected_request=expected,
            allow_not_found=False,
        )
        return retried, None
    except Exception as retry_error:
        if _controlled_error(retry_error) == "invalid_response":
            error_code = "invalid_response"
    return None, error_code


def _add(stats: dict[str, int], name: str, amount: int) -> None:
    if type(amount) is not int or amount < 0:
        raise c.MaskGcFailure("invalid_response")
    stats[name] += amount


def _validate_scan_page(request: byte.GcScanRequest, page: byte.GcScanPage) -> None:
    if len(page.items) > request.limit:
        raise byte.MaskGcFailure("invalid_response")
    if request.after_key is None:
        return
    previous = request.after_key.encode("utf-8")
    if (
        not page.complete
        and page.after_key is not None
        and page.after_key.encode("utf-8") <= previous
    ):
        raise byte.MaskGcFailure("invalid_response")
    if any(item.object_key.encode("utf-8") <= previous for item in page.items):
        raise byte.MaskGcFailure("invalid_response")


async def _inspect_page(
    data: NativeMaskGcData,
    after_key: str | None,
    page: byte.GcScanPage,
    *,
    include_reference_count: bool,
) -> c.InspectMaskGcPageResult:
    request = c.InspectMaskGcPageRequest(
        after_key=after_key,
        page=page,
        include_reference_count=include_reference_count,
    )
    result = await _data_call(data, "inspect_mask_gc_page", request)
    if not isinstance(result, c.InspectMaskGcPageResult):
        result = c.InspectMaskGcPageResult.model_validate(result)
    c.validate_scope("inspect_mask_gc_page", request, result)
    return result


async def _run_preview(
    data: NativeMaskGcData,
    storage: MaskGcStorage,
    *,
    scan_pages: int,
    after_key: str | None,
) -> c.CycleStats:
    stats = c.CycleStats(dry_run=True).model_dump()
    stats["next_after_key"] = after_key
    cursor = after_key
    reference_count_pending = True

    for _ in range(scan_pages):
        try:
            scan_request = byte.GcScanRequest(
                after_key=cursor,
                limit=byte.MAX_GC_PAGE,
            )
            page = await _storage_call(storage, "scan", scan_request)
            page = byte.GcScanPage.model_validate(page)
            _validate_scan_page(scan_request, page)
        except Exception:
            stats["scan_errors"] += 1
            break

        try:
            inspection = await _inspect_page(
                data,
                cursor,
                page,
                include_reference_count=reference_count_pending,
            )
        except Exception:
            stats["data_errors"] += 1
            break

        if reference_count_pending:
            stats["referenced"] = inspection.referenced
            reference_count_pending = False
        _add(stats, "scan_pages", 1)
        _add(stats, "scanned", inspection.scanned)
        _add(stats, "eligible", inspection.eligible)
        # Dry-run has no separate retained counter; skipped carries the preview's
        # retained count while staged remains zero.
        _add(stats, "skipped", inspection.retained)
        cursor = page.after_key
        stats["next_after_key"] = cursor
        stats["scan_complete"] = page.complete
        if page.complete:
            break

    return c.CycleStats(**stats)


async def run_cycle(
    data: NativeMaskGcData,
    storage: MaskGcStorage,
    *,
    worker_id: UUID,
    scan_pages: int = 1,
    delete_limit: int = 100,
    dry_run: bool = False,
    after_key: str | None = None,
) -> c.CycleStats:
    """Preview inventory or stage a bounded page and reconcile delete claims.

    Byte-owner failures never cause a terminal native result to be fabricated.
    Unresolved operation IDs are passed to the native finisher with either the
    validated status or a controlled uncertainty code, and later claims continue.
    """
    if not isinstance(worker_id, UUID):
        raise c.MaskGcFailure("invalid_request")
    try:
        request = c.RunRequest(
            scan_pages=scan_pages,
            delete_limit=delete_limit,
            dry_run=dry_run,
            after_key=after_key,
        )
    except (TypeError, ValueError):
        raise c.MaskGcFailure("invalid_request")
    if request.dry_run:
        return await _run_preview(
            data,
            storage,
            scan_pages=request.scan_pages,
            after_key=request.after_key,
        )

    stats = c.CycleStats().model_dump()

    # A failed inventory pass leaves the durable cursor untouched. Continue to
    # reconcile already-staged operations in this same cycle.
    sweep: c.SweepClaim | None = None
    try:
        claimed = await _data_call(
            data,
            "claim_mask_gc_sweep",
            c.ClaimMaskGcSweepRequest(worker_id=worker_id, lease_seconds=30),
        )
        if not isinstance(claimed, c.ClaimMaskGcSweepResult):
            claimed = c.ClaimMaskGcSweepResult.model_validate(claimed)
        sweep = claimed.sweep
        if sweep is not None:
            stats["next_after_key"] = sweep.after_key
            stats["scan_complete"] = sweep.complete
    except Exception:
        stats["data_errors"] += 1

    reference_count_pending = True
    for _ in range(request.scan_pages):
        if sweep is None or sweep.complete:
            break
        try:
            scan_request = byte.GcScanRequest(
                after_key=sweep.after_key,
                limit=byte.MAX_GC_PAGE,
            )
            page = await _storage_call(storage, "scan", scan_request)
            page = byte.GcScanPage.model_validate(page)
            _validate_scan_page(scan_request, page)
            if reference_count_pending:
                try:
                    inspection = await _inspect_page(
                        data,
                        sweep.after_key,
                        page,
                        include_reference_count=True,
                    )
                    stats["referenced"] = inspection.referenced
                    reference_count_pending = False
                except Exception:
                    # The preview count is observational. Its failure must not
                    # prevent the durable staging and replay path from running.
                    stats["data_errors"] += 1
            stage_request = c.StageMaskGcPageRequest(
                sweep_id=sweep.sweep_id,
                generation=sweep.generation,
                lease_token=sweep.lease_token,
                after_key=sweep.after_key,
                page=page,
            )
            staged = await _data_call(
                data,
                "stage_mask_gc_page",
                stage_request,
            )
            if not isinstance(staged, c.StageMaskGcPageResult):
                staged = c.StageMaskGcPageResult.model_validate(staged)
            c.validate_scope("stage_mask_gc_page", stage_request, staged)
            _add(stats, "scan_pages", 1)
            _add(stats, "scanned", staged.scanned)
            _add(stats, "eligible", staged.staged)
            _add(stats, "staged", staged.staged)
            _add(stats, "skipped", staged.skipped)
            sweep = staged.sweep
            stats["next_after_key"] = sweep.after_key
            stats["scan_complete"] = sweep.complete
            if page.complete:
                break
        except Exception:
            stats["scan_errors"] += 1
            break

    try:
        delete_claims = await _data_call(
            data,
            "claim_mask_gc_deletes",
            c.ClaimMaskGcDeletesRequest(
                worker_id=worker_id,
                limit=request.delete_limit,
                lease_seconds=30,
            ),
        )
        if not isinstance(delete_claims, c.ClaimMaskGcDeletesResult):
            delete_claims = c.ClaimMaskGcDeletesResult.model_validate(delete_claims)
        if len(delete_claims.items) > request.delete_limit:
            raise c.MaskGcFailure("invalid_response")
    except Exception:
        stats["data_errors"] += 1
        return c.CycleStats(**stats)

    for claim in delete_claims.items:
        stats["delete_claims"] += 1
        receipt, error_code = await _resolve_delete(storage, claim)
        try:
            finished = await _data_call(
                data,
                "finish_mask_gc_delete",
                c.FinishMaskGcDeleteRequest(
                    operation_id=claim.operation_id,
                    generation=claim.generation,
                    lease_token=claim.lease_token,
                    receipt=receipt,
                    error_code=error_code,
                ),
            )
            if not isinstance(finished, c.FinishMaskGcDeleteResult):
                finished = c.FinishMaskGcDeleteResult.model_validate(finished)
            if finished.operation_id != claim.operation_id:
                raise c.MaskGcFailure("invalid_response")
            expected_completed = receipt is not None and receipt.state == "completed"
            if finished.completed != expected_completed:
                raise c.MaskGcFailure("invalid_response")
            if finished.completed:
                stats["completed"] += 1
                if receipt is not None:
                    stats[receipt.outcome] += 1
            elif finished.state in {"pending", "indeterminate"}:
                stats[finished.state] += 1
                stats["durable_uncertainties"] += 1
        except Exception:
            # A lost finish acknowledgement is itself ambiguous. Leave the claim
            # fenced; the next worker replays its stable byte operation ID.
            stats["finish_errors"] += 1

    return c.CycleStats(**stats)
