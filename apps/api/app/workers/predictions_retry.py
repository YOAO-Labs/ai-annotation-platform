"""v0.8.6 F6 · 失败预测重试 Celery task

链路：
1. 读 failed_predictions 行 → 取 task_id + ml_backend_id
2. ws 推 `failed_prediction.retry.started`
3. 调 MLBackendClient.predict 重跑
4. 成功 → 写 predictions + 删 failed_predictions + async_jobs `job.completed`
5. 失败 → retry_count += 1 + last_retry_at + async_jobs `job.failed`

软上限 max=3 由路由层判断（HTTP 409）；本 task 信任传入。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings
from app.workers.celery_app import celery_app

log = logging.getLogger(__name__)


class _PredictionRetryTerminalFailure(Exception):
    def __init__(self, result: dict):
        self.result = result
        super().__init__(str(result.get("reason") or result.get("status") or "failed"))


def _require_qualified_image_retry(task, project, retry_job_id):
    """A late historical image delivery must not invent today's original source."""
    if retry_job_id is None and (
        (task is not None and task.file_type == "image")
        or (project is not None and project.data_type == "image")
    ):
        from app.services.content_snapshot import ContentError

        raise ContentError("prediction_original_requires_new_request", 409)


@celery_app.task(
    bind=True, name="app.workers.predictions_retry.retry_failed_prediction"
)
def retry_failed_prediction(
    self, failed_id: str, user_id: str, async_job_id: str | None = None
) -> dict:
    return asyncio.run(
        _run_retry(
            failed_id,
            user_id,
            celery_task_id=getattr(self.request, "id", None),
            async_job_id=async_job_id,
            worker_node_id=f"worker:{self.request.hostname}",
        )
    )


async def _run_retry(
    failed_id: str,
    user_id: str,
    celery_task_id: str | None = None,
    async_job_id: str | None = None,
    worker_node_id: str | None = None,
) -> dict:
    engine = create_async_engine(settings.database_url, echo=False)
    SessionLocal = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    try:
        return await _do_retry_with_factory(
            SessionLocal,
            failed_id,
            user_id,
            celery_task_id=celery_task_id,
            async_job_id=async_job_id,
            worker_node_id=worker_node_id,
        )
    finally:
        await engine.dispose()


async def _do_retry_with_factory(
    session_factory,
    failed_id: str,
    user_id: str,
    celery_task_id: str | None = None,
    async_job_id: str | None = None,
    worker_node_id: str | None = None,
) -> dict:
    """实际 retry 逻辑；session_factory 暴露便于测试 mock。"""
    from app.services import async_job as async_job_svc
    from app.services.async_job_notify import notify_job_terminal

    if async_job_id is not None:
        return await _run_qualified_retry(
            session_factory,
            failed_id,
            user_id,
            async_job_id,
            celery_task_id,
            worker_node_id,
        )

    fid = uuid.UUID(failed_id)
    uid = uuid.UUID(user_id)

    async with session_factory() as db:
        project_id, payload = await _retry_job_metadata(db, fid)
        job = None
        try:
            async with async_job_svc.track_job(
                db,
                kind="prediction_retry",
                user_id=uid,
                project_id=project_id,
                payload=payload,
                celery_task_id=celery_task_id,
            ) as job:
                await db.commit()
                result = await _run_retry_attempt(session_factory, failed_id, user_id)
                job.result = _retry_result_payload(result)
                if result.get("status") != "succeeded":
                    raise _PredictionRetryTerminalFailure(result)
        except _PredictionRetryTerminalFailure as exc:
            if job is not None:
                await notify_job_terminal(db, job_id=job.id)
                await db.commit()
            return exc.result
        except Exception:
            if job is not None:
                await notify_job_terminal(db, job_id=job.id)
                await db.commit()
            async with session_factory() as retry_db:
                await _bump_retry_counter(retry_db, fid)
                await retry_db.commit()
            raise

        if job is not None:
            await notify_job_terminal(db, job_id=job.id)
            await db.commit()
        return result


async def _retry_job_metadata(
    db: AsyncSession, fid: uuid.UUID
) -> tuple[uuid.UUID | None, dict]:
    from app.db.models.ml_backend_registry import MLBackendRegistry as MLBackend
    from app.db.models.prediction import FailedPrediction
    from app.db.models.project import Project
    from app.db.models.task import Task

    payload: dict = {"failed_prediction_id": str(fid)}
    fp = await db.get(FailedPrediction, fid)
    if not fp:
        return None, payload

    payload.update(
        {
            "task_id": str(fp.task_id) if fp.task_id else None,
            "project_id": str(fp.project_id),
            "ml_backend_id": str(fp.ml_backend_id) if fp.ml_backend_id else None,
            "error_type": fp.error_type,
            "message": (fp.message or "")[:200],
        }
    )
    task = await db.get(Task, fp.task_id) if fp.task_id else None
    if task is not None:
        payload["task_display_id"] = task.display_id
    project = await db.get(Project, fp.project_id)
    if project is not None:
        payload["project_display_id"] = project.display_id
        payload["project_name"] = project.name
    backend = await db.get(MLBackend, fp.ml_backend_id) if fp.ml_backend_id else None
    if backend is not None:
        payload["ml_backend_name"] = backend.name
    return fp.project_id, payload


def _retry_result_payload(result: dict) -> dict:
    status = str(result.get("status") or "")
    out = dict(result)
    if status == "succeeded":
        out.setdefault("success_count", 1)
        out.setdefault("failed_count", 0)
        return out
    out.setdefault("success_count", 0)
    out.setdefault("failed_count", 1)
    out.setdefault("reason", status or "failed")
    return out


async def _run_retry_attempt(session_factory, failed_id: str, user_id: str) -> dict:
    """重试业务逻辑。终态通知由外层 async_jobs 统一发。"""
    from app.db.models.ml_backend_registry import MLBackendRegistry as MLBackend
    from app.db.models.prediction import FailedPrediction
    from app.db.models.project import Project
    from app.db.models.task import Task
    from app.services.gpu_arbitration.dispatch import (
        build_gpu_dispatch_context_factory,
    )
    from app.services.gpu_arbitration.contracts import gpu_arbiter_failure_record
    from app.services.ml_routing.client import RoutedMLBackendClient
    from app.services.notification import NotificationService
    from app.services.prediction import PredictionService
    from app.services.storage import resolve_task_url

    fid = uuid.UUID(failed_id)
    uid = uuid.UUID(user_id)

    # 第一阶段：读 failed + 推 started
    async with session_factory() as db:
        fp = await db.get(FailedPrediction, fid)
        if not fp:
            log.warning("retry_failed_prediction: not found id=%s", failed_id)
            return {"status": "not_found", "failed_id": failed_id}
        task = await db.get(Task, fp.task_id) if fp.task_id else None
        backend = (
            await db.get(MLBackend, fp.ml_backend_id) if fp.ml_backend_id else None
        )
        snapshot_id = fp.execution_snapshot_id
        stored_context = (fp.extra or {}).get("request_context")
        request_context = stored_context if isinstance(stored_context, dict) else None
        project = await db.get(Project, fp.project_id)
        _require_qualified_image_retry(task, project, None)
        ns = NotificationService(db)
        pending = await ns.notify(
            user_id=uid,
            type="failed_prediction.retry.started",
            target_type="failed_prediction",
            target_id=fid,
            payload={"project_id": str(fp.project_id)},
        )
        await db.commit()
        if pending is not None:
            await ns.publish_committed([pending])

    if snapshot_id is not None:
        return await _run_snapshot_retry(session_factory, fid, uid)

    if not task or not backend:
        async with session_factory() as db:
            await _bump_retry_counter(db, fid)
            await db.commit()
        return {
            "status": "failed",
            "failed_id": failed_id,
            "reason": "missing_task_or_backend",
        }

    # 第二阶段：按 failed_prediction 记录的 registry 定位逻辑池，再由 router
    # 选择本次重试实际调用的物理实例。
    executed_backend_id = backend.id
    retry_pool_id = None
    try:
        async with session_factory() as route_db:
            client = RoutedMLBackendClient(
                route_db,
                backend,
                project_id=task.project_id,
                owner=f"retry:{failed_id}",
                operation="prediction_retry",
                shadow_session_factory=session_factory,
                dispatch_context_factory=build_gpu_dispatch_context_factory(
                    session_factory
                ),
            )
            task_input = {
                "id": str(task.id),
                "file_path": resolve_task_url(task),
            }
            if request_context is None:
                results = await client.predict([task_input])
            else:
                results = await client.predict([task_input], context=request_context)
            executed_backend_id = client.last_instance_id or backend.id
            retry_pool_id = client.pool_id
        if not results:
            raise RuntimeError("backend returned empty results")
        first = results[0]
    except Exception as exc:
        log.warning(
            "retry_failed_prediction: backend call failed id=%s err=%s", failed_id, exc
        )
        gpu_arbiter_error = gpu_arbiter_failure_record(exc)
        async with session_factory() as db:
            await _bump_retry_counter(db, fid, gpu_arbiter_error=gpu_arbiter_error)
            await db.commit()
        result = {
            "status": "failed",
            "failed_id": failed_id,
            "reason": (
                gpu_arbiter_error["message"]
                if gpu_arbiter_error is not None
                else str(exc)
            )[:200],
        }
        if gpu_arbiter_error is not None:
            result["gpu_arbiter_error"] = gpu_arbiter_error
        return result

    # 第三阶段：写 predictions + 删 failed + 推 succeeded
    async with session_factory() as db:
        pred_svc = PredictionService(db)
        pred = await pred_svc.create_from_ml_result(
            task_id=task.id,
            project_id=task.project_id,
            ml_backend_id=executed_backend_id,
            result=first.result,
            score=first.score,
            model_version=first.model_version,
            inference_time_ms=first.inference_time_ms,
            ml_backend_pool_id=retry_pool_id,
            token_meta=first.meta,
            **(
                {"model_provenance": request_context["frozen_model_ref"]}
                if request_context and request_context.get("frozen_model_ref")
                else {}
            ),
        )
        # 删除 failed_prediction 行
        fp_again = await db.get(FailedPrediction, fid)
        if fp_again:
            await db.delete(fp_again)
        await db.commit()

    return {
        "status": "succeeded",
        "failed_id": failed_id,
        "prediction_id": str(pred.id),
    }


async def _bump_retry_counter(
    db: AsyncSession,
    fid: uuid.UUID,
    *,
    gpu_arbiter_error: dict | None = None,
    pipeline_errors: list | None = None,
) -> None:
    from app.db.models.prediction import FailedPrediction
    from sqlalchemy import select

    fp = await db.scalar(
        select(FailedPrediction)
        .where(FailedPrediction.id == fid)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if fp and not (fp.extra or {}).get("retry_job_id"):
        fp.retry_count = (fp.retry_count or 0) + 1
        fp.last_retry_at = datetime.now(timezone.utc)
        extra = dict(fp.extra or {})
        extra.pop("retry_pending", None)
        if gpu_arbiter_error is not None:
            extra["last_gpu_arbiter_error"] = gpu_arbiter_error
        if pipeline_errors is not None:
            extra["last_pipeline_errors"] = pipeline_errors
        fp.extra = extra
        await db.flush()


async def _run_qualified_retry(
    factory, failed_id, actor_id, job_id, celery_task_id, node_id
):
    from app.services import async_job as jobs
    from app.services.content_snapshot import ContentError

    fid, uid, jid = uuid.UUID(failed_id), uuid.UUID(actor_id), uuid.UUID(job_id)
    async with factory() as db:
        job = await jobs.lock_job(db, jid)
        if (
            job is None
            or job.kind != "prediction_retry"
            or job.user_id != uid
            or (job.payload or {}).get("failed_prediction_id") != str(fid)
            or job.celery_task_id != celery_task_id
        ):
            raise ContentError("prediction_retry_attempt_invalid")
        if job.status != "pending":
            return job.result or {"status": "already_running", "failed_id": str(fid)}
        from app.services.notification import NotificationService

        notifications = NotificationService(db)
        pending = await notifications.notify(
            user_id=uid,
            type="failed_prediction.retry.started",
            target_type="failed_prediction",
            target_id=fid,
            payload={"project_id": str(job.project_id)},
        )
        await jobs.mark_running(db, jid)
        await db.commit()
        if pending is not None:
            await notifications.publish_committed([pending])
    return await _run_snapshot_retry(
        factory, fid, uid, retry_job_id=jid, node_id=node_id
    )


async def _finish_qualified_failure(
    factory, failed_id, job_id, reason, pipeline_errors, *, gpu_arbiter_error=None
):
    from app.services.prediction_retries import finish_retry_attempt

    async with factory() as db:
        result = await finish_retry_attempt(
            db,
            job_id,
            reason=reason,
            pipeline_errors=pipeline_errors,
            gpu_arbiter_error=gpu_arbiter_error,
        )
        await db.commit()
        return result or {
            "status": "failed",
            "failed_id": str(failed_id),
            "reason": reason,
        }


async def _run_snapshot_retry(
    session_factory, failed_id, actor_id, *, retry_job_id=None, node_id=None
):
    """Replay one failed task's original pipeline without resolving a new release."""
    from sqlalchemy import select
    from app.db.models.prediction import FailedPrediction
    from app.db.models.task import Task
    from app.db.models.project import Project
    from app.db.models.user import User
    from app.db.models.ml_backend_registry import MLBackendRegistry
    from app.services.prediction_execution import (
        load_snapshot,
        validate_task_scope,
        build_stage_context,
        execution_summary,
    )
    from app.services.prediction import PredictionService
    from app.services import trained_models
    from app.services.storage import resolve_task_url
    from app.services.ml_routing.client import RoutedMLBackendClient
    from app.services.gpu_arbitration.dispatch import build_gpu_dispatch_context_factory
    from app.workers.tasks import (
        _run_task_pipeline,
        _resolve_input_mode,
        _hold_preannotation_authority,
        _preannotation_actor_revoked,
        _require_image_job_proof,
        _require_image_inputs,
        _input_receipt_kwargs,
    )

    pipeline_errors = None
    reference_owner = None
    task_uuid = None
    try:
        async with session_factory() as db:
            # Serialize duplicate deliveries and retain the snapshot reference during cleanup.
            query = select(FailedPrediction).where(FailedPrediction.id == failed_id)
            if retry_job_id is None:
                query = query.with_for_update()
            fp = await db.scalar(query.execution_options(populate_existing=True))
            if not fp:
                if retry_job_id is not None:
                    raise ValueError("original_failure_unavailable")
                return {"status": "not_found", "failed_id": str(failed_id)}
            if fp.dismissed_at is not None:
                raise ValueError("失败记录已放弃，请先恢复")
            task = await db.get(Task, fp.task_id) if fp.task_id else None
            actor = await db.get(User, actor_id)
            project = await db.get(Project, fp.project_id)
            if not task or not actor or not project:
                raise ValueError("原始任务或执行者已不可用")
            _require_qualified_image_retry(task, project, retry_job_id)
            payload, digest = await load_snapshot(
                db, fp.execution_snapshot_id, project.id
            )
            scope = payload["execution_scope"]
            if await _preannotation_actor_revoked(db, actor, project.id, scope):
                raise ValueError("重试操作权限已失效")
            await validate_task_scope(db, project, task, actor, scope)
            if retry_job_id is not None:
                from copy import deepcopy
                from app.db.models.async_job import AsyncJob
                from app.services.prediction_references import ReferencePredictionJob
                from app.services.content_snapshot import ContentError

                job = await db.get(AsyncJob, retry_job_id, populate_existing=True)
                proof = (
                    deepcopy((job.payload or {}).get("prediction_authority"))
                    if job
                    else None
                )
                _require_image_job_proof(project, proof)
                if (
                    not proof
                    or (fp.extra or {}).get("retry_job_id") != str(retry_job_id)
                    or fp.execution_snapshot_id != job.execution_snapshot_id
                ):
                    raise ContentError("prediction_retry_attempt_changed")
                reference_owner = ReferencePredictionJob(
                    db,
                    retry_job_id,
                    proof,
                    project_id=project.id,
                    actor_id=actor_id,
                    task_ids=[task.id],
                    batch_id=task.batch_id,
                    scope=scope,
                    predict_mode="append",
                    snapshot_id=fp.execution_snapshot_id,
                    snapshot_digest=digest,
                    node_id=node_id,
                    execution_unit=payload.get("execution_unit"),
                    job_kind="prediction_retry",
                )
                task_uuid = task.id
                task_inputs = await reference_owner.open_inputs(task, session_factory)
            else:
                task_inputs = None
            _require_image_inputs(task, reference_owner, task_inputs)
            stages = sorted(payload["stages"], key=lambda stage: stage["stage"])
            clients, contexts, modes = [], [], []
            for stage in stages:
                ref = stage.get("frozen_model_ref")
                if trained_models.is_controlled(stage.get("model_id")):
                    if (
                        not ref
                        or str(
                            await trained_models.validate_frozen_reference(
                                db, project.id, ref
                            )
                        )
                        != stage["ml_backend_id"]
                    ):
                        raise ValueError("原始阶段的模型部署身份已失效")
                backend = await db.get(
                    MLBackendRegistry, uuid.UUID(stage["ml_backend_id"])
                )
                if not backend:
                    raise ValueError("原始管线推理实例已删除")
                clients.append(
                    RoutedMLBackendClient(
                        db,
                        backend,
                        project_id=project.id,
                        owner=f"retry:{failed_id}:stage:{stage['stage']}",
                        operation="prediction_retry",
                        shadow_session_factory=session_factory,
                        dispatch_context_factory=build_gpu_dispatch_context_factory(
                            session_factory
                        ),
                    )
                )
                contexts.append(build_stage_context(stage, payload))
                modes.append(_resolve_input_mode(stage))
            results, extra, _ = await _run_task_pipeline(
                task,
                stages,
                clients,
                contexts,
                resolve_url=reference_owner.resolve_url
                if task_inputs
                else resolve_task_url,
                upload_crop=task_inputs.upload_crop if task_inputs else None,
                load_image=task_inputs.load_image if task_inputs else None,
                before_dispatch=(lambda: reference_owner.before_dispatch(task))
                if reference_owner
                else None,
                stage_modes=modes,
                execution_snapshot_id=fp.execution_snapshot_id,
                input_session_factory=None if task_inputs else session_factory,
                input_owner_id=retry_job_id or failed_id,
                input_owner_kind="prediction_retry",
            )
            if (extra or {}).get("pipeline", {}).get("partial"):
                pipeline_errors = extra["pipeline"].get("stage_errors", [])
                raise ValueError("管线下游阶段仍有失败，已保留原失败记录和原候选结果")
            if not results:
                raise ValueError("推理实例未返回结果")
            if reference_owner is not None:
                from app.services import async_job as jobs

                await jobs.lock_job(db, retry_job_id)
            if not await _hold_preannotation_authority(db, actor, project.id, scope):
                raise ValueError("重试期间操作权限已撤销")
            reference_receipt = None
            if reference_owner is not None:
                fp = await db.scalar(
                    select(FailedPrediction)
                    .where(FailedPrediction.id == failed_id)
                    .with_for_update(nowait=True)
                    .execution_options(populate_existing=True)
                )
                if (
                    fp is None
                    or fp.dismissed_at is not None
                    or (fp.extra or {}).get("retry_job_id") != str(retry_job_id)
                    or fp.execution_snapshot_id != job.execution_snapshot_id
                ):
                    raise ContentError("prediction_retry_attempt_changed")
                await reference_owner.lock_key()
            await validate_task_scope(db, project, task, actor, scope, lock=True)
            if reference_owner is not None:
                reference_receipt = await reference_owner.final_receipt(task)
            receipt_kwargs = _input_receipt_kwargs(
                reference_owner, task_uuid, reference_receipt
            )

            extra = {
                **(extra or {}),
                "execution_snapshot": {
                    "id": str(fp.execution_snapshot_id),
                    "digest": digest,
                    "stages": execution_summary(stages),
                },
            }
            writes = [
                dict(
                    task_id=task.id,
                    project_id=project.id,
                    ml_backend_id=clients[0].last_instance_id
                    or uuid.UUID(stages[0]["ml_backend_id"]),
                    ml_backend_pool_id=clients[0].pool_id,
                    result=result.result,
                    score=result.score,
                    model_version=result.model_version,
                    inference_time_ms=result.inference_time_ms,
                    token_meta=result.meta,
                    pipeline_extra=extra,
                    **(
                        {"model_provenance": stages[0]["frozen_model_ref"]}
                        if stages[0].get("frozen_model_ref")
                        else {}
                    ),
                )
                for result in results
            ]
            native_receipts = (
                await reference_owner.qualify_native_results(
                    task, reference_receipt, writes
                )
                if reference_owner
                else [None] * len(writes)
            )
            service = PredictionService(db)
            first = None
            for write, native_receipt in zip(writes, native_receipts, strict=True):
                pred = await service.create_from_ml_result(
                    **write,
                    **(
                        {"background_native_reference_receipt": native_receipt}
                        if native_receipt is not None
                        else receipt_kwargs
                    ),
                )
                first = first or pred
            from app.db.models.prediction import Prediction, PredictionMeta

            for previous_id in (fp.extra or {}).get("partial_prediction_ids", []):
                previous = await db.scalar(
                    select(Prediction).where(
                        Prediction.id == uuid.UUID(previous_id),
                        Prediction.task_id == task.id,
                    )
                )
                if previous is None:
                    continue
                metadata = await db.scalar(
                    select(PredictionMeta).where(
                        PredictionMeta.prediction_id == previous.id
                    )
                )
                if metadata and (metadata.extra or {}).get(
                    "execution_snapshot", {}
                ).get("id") == str(fp.execution_snapshot_id):
                    # Retain accepted annotation provenance; hide superseded AI candidates only.
                    previous.rejected_shape_indexes = list(
                        range(len(previous.result or []))
                    )
                    metadata.extra = {
                        **(metadata.extra or {}),
                        "superseded_by_prediction_id": str(first.id),
                    }
            await db.delete(fp)
            result = {
                "status": "succeeded",
                "failed_id": str(failed_id),
                "prediction_id": str(first.id),
            }
            if reference_owner is not None:
                from app.services.async_job_notify import notify_job_terminal

                await jobs.mark_complete(
                    db, retry_job_id, result=_retry_result_payload(result)
                )
                await notify_job_terminal(db, job_id=retry_job_id)
            await db.commit()
            return result
    except Exception as exc:
        if retry_job_id is not None:
            from app.services.content_snapshot import ContentError
            from app.services.gpu_arbitration.contracts import (
                gpu_arbiter_failure_record,
            )
            from fastapi import HTTPException

            gpu_arbiter_error = gpu_arbiter_failure_record(exc)
            reason = (
                gpu_arbiter_error["message"]
                if gpu_arbiter_error is not None
                else (
                    exc.code
                    if isinstance(exc, ContentError)
                    else (
                        exc.detail.get("code", "prediction_retry_authority_changed")
                        if isinstance(exc, HTTPException)
                        and isinstance(exc.detail, dict)
                        else (str(exc) or "prediction_retry_failed")[:200]
                    )
                )
            )
            return await _finish_qualified_failure(
                session_factory,
                failed_id,
                retry_job_id,
                reason,
                pipeline_errors,
                gpu_arbiter_error=gpu_arbiter_error,
            )

        async with session_factory() as db:
            from app.services.gpu_arbitration.contracts import (
                gpu_arbiter_failure_record,
            )

            await _bump_retry_counter(
                db,
                failed_id,
                gpu_arbiter_error=gpu_arbiter_failure_record(exc),
                pipeline_errors=pipeline_errors,
            )
            await db.commit()
        return {
            "status": "failed",
            "failed_id": str(failed_id),
            "reason": str(exc)[:200],
        }

    finally:
        if reference_owner is not None and task_uuid is not None:
            await reference_owner.close_inputs(task_uuid)
