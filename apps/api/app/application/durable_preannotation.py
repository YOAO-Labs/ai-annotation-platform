"""Consume frozen batch members without repeating accepted inference results."""

from __future__ import annotations

import asyncio
import tempfile
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError

from app.application.preannotation_results import (
    PreannotationResultDeferred,
    restore_write_scope,
    scope_json,
)
from app.services.labeling_prediction_contract import ApplyPredictionResultError
from app.services.labeling_write_scope import capture_labeling_write_scope
from app.services.raster_mask_contract import RasterMaskContractError
from app.services.labeling_core_read_relations import get_core_row, task_row


@dataclass(frozen=True, slots=True)
class RetainedBatchInput:
    reference: object
    local_path: Path
    metadata: dict


@asynccontextmanager
async def _retained_input(execution, member):
    """Open the original frozen source only when model work is still necessary."""
    from aap_assets_inputs import contract as c
    from app.services.accepted_inputs import input_gateway

    frozen = member.manifest_member
    if frozen is None:
        yield None
        return
    record = frozen.get("input")
    if not isinstance(record, dict):
        raise ValueError(str(frozen.get("error") or "input_not_ready"))
    if "state" in frozen and frozen["state"] != "ready":
        raise ValueError(str(frozen.get("error") or "input_not_ready"))
    request = c.InputRequest(
        input_id=uuid.UUID(record["input_id"]),
        job_id=execution.scope["job_id"],
        task_id=member.task_id,
    )
    try:
        async with input_gateway(session_factory=execution.session_factory) as gateway:
            reference = await gateway.ensure_reference(request)
            actual = reference.input
            if any(
                str(getattr(actual, key)) != str(record[key])
                for key in (
                    "input_id",
                    "job_id",
                    "task_id",
                    "project_id",
                    "actor_id",
                    "sha256",
                    "byte_size",
                )
            ):
                raise ValueError("retained input differs from the frozen member")
            current = await gateway.verify_source(request)
            if current.sha256 != actual.sha256 or current.byte_size != actual.byte_size:
                raise ValueError("retained source changed")
            with tempfile.TemporaryDirectory(
                prefix="aap-batch-result-input-"
            ) as directory:
                path = Path(directory) / "source"
                await gateway.download(reference, path)
                yield RetainedBatchInput(
                    reference=reference,
                    local_path=path,
                    metadata={
                        key: record[key]
                        for key in (
                            "input_id",
                            "job_id",
                            "task_id",
                            "sha256",
                            "byte_size",
                        )
                    },
                )
    except c.InputFailure as exc:
        if exc.code.endswith("_busy") or exc.code in {
            "storage_unavailable",
            "input_not_ready",
        }:
            raise PreannotationResultDeferred(exc.code) from None
        raise
    except OSError:
        raise PreannotationResultDeferred("storage_unavailable") from None


async def freeze_batch_members(
    execution, db, entries, *, request, accepted_input, manifest_summary=None
):
    """Seal the entire selection before overwrite, inference, or member outcomes."""
    if execution.progress.selection_complete:
        return
    cursor = execution.progress.selection_cursor
    atomic_selection = request.accepted_input_manifest_protocol is None
    page = []
    last = 0
    async for index, task, manifest_member, _batch in entries:
        ordinal = index + 1
        last = ordinal
        if ordinal <= cursor:
            # Manifest membership is already sealed. A non-manifest selection
            # is staged in one transaction, so it never has a partial cursor.
            continue
        frozen = (
            manifest_member.model_dump(mode="json")
            if manifest_member is not None
            else {
                "kind": "workbench",
                "ordinal": ordinal,
                "task_id": str(task.id),
                "project_id": str(task.project_id),
                "state": "ready",
                "input": accepted_input.reference.input.model_dump(mode="json"),
            }
            if accepted_input is not None
            else None
        )
        if manifest_member is not None:
            frozen.update(
                {
                    "manifest_id": manifest_summary["manifest_id"],
                    "selection_digest": manifest_summary["selection_digest"],
                    "sealed_digest": manifest_summary["sealed_digest"],
                }
            )
        input_record = frozen.get("input") if frozen is not None else None
        scope = (
            capture_labeling_write_scope(
                task,
                actor_id=execution.scope["actor_id"],
                actor_known=True,
                operation="preannotation",
                execution_scope=request.execution_scope,
                execution_scope_known=True,
                job_id=execution.scope["job_id"],
                job_id_known=True,
                accepted_input_id=(
                    uuid.UUID(input_record["input_id"])
                    if isinstance(input_record, dict)
                    else None
                ),
            )
            if task is not None
            else None
        )
        page.append(
            {
                "ordinal": ordinal,
                "task_id": task.id if task is not None else manifest_member.task_id,
                "write_scope": scope_json(scope) if scope is not None else None,
                "manifest_member": frozen,
            }
        )
        if len(page) == 100:
            await execution.call(
                "stage_batch_result_members",
                after_ordinal=cursor,
                members=page,
                complete=False,
                db=db if atomic_selection else None,
            )
            cursor = ordinal
            page = []
    if last < cursor:
        raise ValueError("selection lost already staged members")
    await execution.call(
        "stage_batch_result_members",
        after_ordinal=cursor,
        members=page,
        complete=True,
        db=db if atomic_selection else None,
    )
    if atomic_selection:
        try:
            await db.commit()
        except SQLAlchemyError:
            await db.rollback()
            raise PreannotationResultDeferred("storage_unavailable") from None


async def _cancel_requested(db, job_id):
    from app.services import async_job as async_job_svc

    job = await async_job_svc.get_job(db, job_id, populate_existing=True)
    return (
        job is None
        or job.status == "cancelled"
        or bool((job.payload or {}).get("cancel_requested"))
    )


async def _actor_available(db, execution):
    from app.services.project_access import get_workspace_project
    from app.services.identity_account_lock_data import IdentityAccountLockData
    from app.services.project_access import is_privileged_for_project

    actor_id = uuid.UUID(str(execution.scope["actor_id"]))
    actor = await IdentityAccountLockData(db).read_actor_status(actor_id)
    project = await get_workspace_project(
        db, project_id=execution.scope["project_id"], populate_existing=True
    )
    return (
        actor is not None
        and actor.is_active
        and project is not None
        and is_privileged_for_project(actor, project)
    )


async def _apply_ready_result(
    execution, db, writer, member, result, requests, *, manifest_backed
):
    from copy import deepcopy
    from app.services.labeling_write_guard import lock_labeling_write

    values = dict(
        ordinal=member.ordinal,
        result_id=result.result_id,
        prepared_digest=result.prepared_digest,
    )
    for attempt in range(3):
        try:
            if requests:
                await writer.apply_prediction_result_bundle(deepcopy(requests))
            else:
                await lock_labeling_write(
                    db,
                    restore_write_scope(member.write_scope),
                    operations=("preannotation",),
                    accepted_input_job_id=execution.scope["job_id"]
                    if manifest_backed
                    else None,
                )
            # The existing Labeling Task guard runs first. This lease lock and
            # completion proof join Prediction/Meta in that same transaction.
            await execution.call("lock_batch_result_write", db=db, **values)
            await execution.call("finish_batch_result_write", db=db, **values)
            try:
                await db.commit()
            except SQLAlchemyError:
                await db.rollback()
                receipt = await execution.read_result(member.ordinal)
                if receipt.result is not None and receipt.result.state == "applied":
                    await execution.call(
                        "read_batch_result_execution",
                        leased=False,
                        after_ordinal=member.ordinal,
                        limit=1,
                    )
                    return
                raise PreannotationResultDeferred("storage_unavailable") from None
            return
        except ApplyPredictionResultError as exc:
            await db.rollback()
            if not exc.retryable:
                raise
            if attempt == 2:
                raise PreannotationResultDeferred(exc.code) from None
            await asyncio.sleep(0.05 * 2**attempt)
        except SQLAlchemyError:
            await db.rollback()
            raise PreannotationResultDeferred("storage_unavailable") from None


async def _overwrite(execution, db, request, *, member=None):
    from app.services.batch import BatchService
    from app.services.labeling_write_guard import lock_labeling_write

    if request.predict_mode != "overwrite":
        return
    if member is None and execution.progress.bulk_overwrite_done:
        return
    if member is not None and member.overwrite_done:
        return
    selected = (
        [member] if member is not None else [item async for item in execution.members()]
    )
    task_ids = []
    for item in sorted(selected, key=lambda item: str(item.task_id)):
        if item.write_scope is None:
            continue
        scope = restore_write_scope(item.write_scope)
        if (
            await get_core_row(db, task_row, item.task_id, populate_existing=True)
            is None
        ):
            continue
        await lock_labeling_write(
            db,
            scope,
            operations=("preannotation",),
            accepted_input_job_id=(
                execution.scope["job_id"]
                if request.accepted_input_manifest_protocol is not None
                else None
            ),
        )
        task_ids.append(item.task_id)
    if task_ids:
        await BatchService(db).clean_task_predictions(
            task_ids, actor_id=execution.scope["actor_id"]
        )
    await execution.call(
        "mark_batch_result_overwrite",
        db=db,
        ordinal=member.ordinal if member is not None else None,
    )
    try:
        await db.commit()
    except SQLAlchemyError:
        await db.rollback()
        raise PreannotationResultDeferred("storage_unavailable") from None


async def run_durable_batch(
    execution,
    db,
    request,
    *,
    entries,
    accepted_input,
    infer,
    writer,
    job_meta,
    result_extra,
    finish_batch=None,
    failure_context=None,
):
    """Resume captured raw/prepared bodies; count only atomic native outcomes."""
    from aap_preannotation_results.contract import BatchResultFailure
    from aap_assets_mask_content.contract import MaskContentFailure
    from app.services import prediction_progress
    from app.services.async_job_notify import notify_job_terminal
    from app.services.batch_result_masks import normalize_batch_prediction_masks

    await freeze_batch_members(
        execution,
        db,
        entries,
        request=request,
        accepted_input=accepted_input,
        manifest_summary=result_extra.get("manifest"),
    )
    await execution.call("start_batch_result_execution")
    manifest_backed = request.accepted_input_manifest_protocol is not None
    member_overwrite = manifest_backed or request.accepted_inputs_protocol is not None
    if not member_overwrite and not await _cancel_requested(
        db, execution.scope["job_id"]
    ):
        await _overwrite(execution, db, request)
    cancelled = False
    forced_error = None
    async for member in execution.members(
        after_ordinal=execution.progress.next_ordinal - 1
    ):
        execution.ensure_owned()
        if await _cancel_requested(db, execution.scope["job_id"]):
            saved = await execution.read_result(member.ordinal)
            if saved.result is None or not saved.result.raw_complete:
                cancelled = True
                break
        if not await _actor_available(db, execution):
            forced_error = "preannotation_actor_revoked"
            break
        context = {
            "task_id": str(member.task_id),
            "total_cost": "0",
            "pipeline_stages": {},
            "gpu_arbiter_failures": [],
            **(failure_context(member) if failure_context is not None else {}),
        }
        result = None
        try:
            if member.write_scope is None:
                await execution.call(
                    "finish_batch_result_failure",
                    db=db,
                    ordinal=member.ordinal,
                    failed_prediction_id=None,
                    error_code="task_missing",
                    context=context,
                )
                await db.commit()
                continue
            await execution.call("start_batch_result_member", ordinal=member.ordinal)
            current = await execution.read_result(member.ordinal)
            result = current.result
            if result is None or not result.raw_complete:
                task = await get_core_row(
                    db, task_row, member.task_id, populate_existing=True
                )
                if task is None:
                    await execution.call(
                        "finish_batch_result_failure",
                        db=db,
                        ordinal=member.ordinal,
                        failed_prediction_id=None,
                        error_code="task_missing",
                        context=context,
                    )
                    await db.commit()
                    continue
                if (
                    result is None
                    and member.state == "planned"
                    and manifest_backed
                    and request.predict_mode == "skip_predicted"
                    and task.total_predictions
                ):
                    await execution.call(
                        "skip_batch_result_member",
                        ordinal=member.ordinal,
                        reason="already_predicted",
                    )
                    continue
                from app.services.labeling_write_guard import lock_labeling_write

                await lock_labeling_write(
                    db,
                    restore_write_scope(member.write_scope),
                    operations=("preannotation",),
                    accepted_input_job_id=(
                        execution.scope["job_id"] if manifest_backed else None
                    ),
                )
                await db.commit()
                async with _retained_input(execution, member) as retained:
                    if member_overwrite:
                        await _overwrite(execution, db, request, member=member)
                    requests, context = await infer(
                        task,
                        restore_write_scope(member.write_scope),
                        retained,
                        member.manifest_member,
                        member.ordinal,
                    )
                # This precedes byte production and final Task/Job write locks.
                await execution.capture(
                    member.ordinal, requests, stage="raw", context=context
                )
                result = (await execution.read_result(member.ordinal)).result
            elif result.state == "capturing":
                await execution.seal(member.ordinal, stage="raw")
            context = result.context
            if not result.prepared_complete:
                _raw_result, requests = await execution.load(
                    member.ordinal, stage="raw"
                )
                requests = await normalize_batch_prediction_masks(db, requests)
                # Only producer epoch/grace facts are pending here. Prediction
                # rows do not exist until the ready snapshot is durably sealed.
                await db.commit()
                await execution.capture(
                    member.ordinal, requests, stage="prepared", context=context
                )
            elif result.state != "ready":
                await execution.seal(member.ordinal, stage="prepared")
            result, requests = await execution.load(member.ordinal, stage="prepared")
            await _apply_ready_result(
                execution,
                db,
                writer,
                member,
                result,
                requests,
                manifest_backed=manifest_backed,
            )
        except PreannotationResultDeferred:
            await db.rollback()
            raise
        except (BatchResultFailure, SQLAlchemyError):
            await db.rollback()
            # A protocol/provider/commit error cannot prove that business writes
            # failed, or that a captured result is safe to discard.
            raise PreannotationResultDeferred("storage_unavailable") from None
        except MaskContentFailure as exc:
            await db.rollback()
            raise PreannotationResultDeferred(exc.code) from None
        except Exception as exc:
            await db.rollback()
            if isinstance(exc, RasterMaskContractError) and (
                exc.detail.get("retryable") or exc.status_code >= 500
            ):
                raise PreannotationResultDeferred(exc.detail["reason"]) from None
            if isinstance(exc, HTTPException) and exc.status_code >= 500:
                raise PreannotationResultDeferred("storage_unavailable") from None
            if isinstance(exc, HTTPException):
                reason = (
                    exc.detail.get("reason") if isinstance(exc.detail, dict) else None
                )
                if isinstance(reason, str) and reason.endswith("_busy"):
                    raise PreannotationResultDeferred(reason) from None
            if isinstance(
                exc, (HTTPException, ApplyPredictionResultError)
            ) and exc.status_code in {401, 403}:
                forced_error = "preannotation_actor_revoked"
                break
            error_code = getattr(exc, "code", None) or type(exc).__name__
            current_failure = (
                failure_context(member) if failure_context is not None else {}
            )
            # An inference exception can happen after route selection. Captured
            # results instead retain the original executed instance on replay.
            if result is None or not result.raw_complete:
                context = {**context, **current_failure}
            from app.services.gpu_arbitration.contracts import (
                gpu_arbiter_failure_record,
            )

            gpu_error = gpu_arbiter_failure_record(exc)
            if gpu_error is not None:
                context = {**context, "gpu_arbiter_failures": [gpu_error]}
                error_code = gpu_error["error_code"]
            current_task = await get_core_row(
                db, task_row, member.task_id, populate_existing=True
            )
            if current_task is None:
                error_code = "task_missing"
            elif current_task.project_id != execution.scope["project_id"]:
                error_code = "scope_abandoned"
                current_task = None
            failure = (
                await writer.create_failed(
                    task_id=member.task_id,
                    project_id=execution.scope["project_id"],
                    ml_backend_id=uuid.UUID(
                        context.get("ml_backend_id") or request.ml_backend_id
                    ),
                    ml_backend_pool_id=(
                        uuid.UUID(context["ml_backend_pool_id"])
                        if context.get("ml_backend_pool_id") is not None
                        else None
                    ),
                    error_type=error_code,
                    message=str(exc),
                    failure_id=member.failure_id,
                    extra={
                        "execution_scope": request.execution_scope,
                        "execution_unit": request.execution_unit,
                        **current_failure.get("failure_extra", {}),
                        **context.get("failure_extra", {}),
                        **(
                            {"gpu_arbiter_error": gpu_error}
                            if gpu_error is not None
                            else {}
                        ),
                    },
                )
                if current_task is not None
                else None
            )
            await execution.call(
                "finish_batch_result_failure",
                db=db,
                ordinal=member.ordinal,
                failed_prediction_id=failure.id if failure is not None else None,
                error_code=error_code,
                context=context,
            )
            try:
                await db.commit()
            except SQLAlchemyError:
                await db.rollback()
                raise PreannotationResultDeferred("storage_unavailable") from None
        progress = execution.progress
        completed = (
            progress.success_count + progress.failed_count + progress.skipped_count
        )
        stage_step = bool(progress.total) and (
            (completed * 100 // progress.total) % 5 == 0 or completed == progress.total
        )
        prediction_progress.publish_progress(
            request.project_id,
            completed,
            progress.total or 0,
            status="running",
            job_meta=job_meta,
            pipeline_stages=(
                [
                    {"stage": int(key), **value}
                    for key, value in sorted(
                        progress.pipeline_stages.items(), key=lambda item: int(item[0])
                    )
                ]
                if progress.pipeline_stages and stage_step
                else None
            ),
        )
    if await _cancel_requested(db, execution.scope["job_id"]):
        cancelled = True
    await execution.call(
        "read_batch_result_execution", leased=False, after_ordinal=0, limit=1
    )
    progress = execution.progress
    extra = {
        **result_extra,
        "total_cost": f"{Decimal(progress.total_cost):.4f}",
        "pipeline_stages": [
            {"stage": int(key), **value}
            for key, value in sorted(
                progress.pipeline_stages.items(), key=lambda item: int(item[0])
            )
        ],
    }
    status = (
        "cancelled"
        if cancelled
        else "failed"
        if forced_error is not None
        or (progress.success_count == 0 and progress.failed_count > 0)
        else "completed"
    )
    if status == "completed" and progress.success_count and finish_batch is not None:
        await finish_batch()
    await execution.call(
        "finish_batch_result_execution",
        db=db,
        status=status,
        error_code=(forced_error or "all_predictions_failed")
        if status == "failed"
        else None,
        result_extra=extra,
    )
    from app.services import async_job as async_job_svc

    await async_job_svc.get_job(db, execution.scope["job_id"], populate_existing=True)
    await notify_job_terminal(db, job_id=execution.scope["job_id"])
    try:
        await db.commit()
    except SQLAlchemyError:
        await db.rollback()
        raise PreannotationResultDeferred("storage_unavailable") from None
    prediction_progress.publish_progress(
        request.project_id,
        progress.success_count + progress.failed_count + progress.skipped_count,
        progress.total or 0,
        status=status,
        job_meta=job_meta,
    )
