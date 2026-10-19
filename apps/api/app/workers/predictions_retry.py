"""v0.8.6 F6 · 失败预测重试 Celery task

链路：
1. 读 failed_predictions 行 → 取 task_id + ml_backend_id
2. ws 推 `failed_prediction.retry.started`
3. 调 MLBackendClient.predict 重跑
4. 成功 → 写 predictions + 删 failed_predictions + async_jobs `job.completed`
5. 失败 → retry_count += 1 + last_retry_at + async_jobs `job.failed`

软上限由重试用例判断（HTTP 409）；本 task 保留既有执行与计数语义。
"""

from __future__ import annotations

import asyncio
import logging
import re
import tempfile
import uuid
from pathlib import Path

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings
from app.services.labeling_prediction_contract import (
    ApplyPredictionResultPort,
    ApplyPredictionResultRequest,
)
from app.workers.celery_app import celery_app

log = logging.getLogger(__name__)
_WRITE_ATTEMPTS = 3


async def _verify_retry_source(gateway, request):
    from aap_assets_inputs.contract import InputFailure

    for attempt in range(_WRITE_ATTEMPTS):
        try:
            return await gateway.verify_source(request)
        except InputFailure as exc:
            if (
                exc.code
                not in {
                    "input_busy",
                    "source_busy",
                    "reference_busy",
                    "job_busy",
                    "storage_unavailable",
                }
                or attempt == _WRITE_ATTEMPTS - 1
            ):
                raise
            await asyncio.sleep(0.05 * 2**attempt)


async def _apply_retry_prediction_with_retries(writer, db, request):
    from app.services.labeling_prediction_contract import ApplyPredictionResultError

    for attempt in range(_WRITE_ATTEMPTS):
        try:
            return await writer.apply_prediction_result(request)
        except ApplyPredictionResultError as exc:
            if not exc.retryable or attempt == _WRITE_ATTEMPTS - 1:
                raise
            await db.rollback()
            await asyncio.sleep(0.05 * 2**attempt)
    raise AssertionError("unreachable prediction retry write state")


def _accepted_input_metadata(value, *, task_id, project_id) -> dict:
    """Validate server-owned retry provenance without borrowing the current URL."""
    if not isinstance(value, dict):
        raise ValueError("accepted input metadata is malformed")
    try:
        input_id = uuid.UUID(value["input_id"])
        job_id = uuid.UUID(value["job_id"])
        metadata_task_id = uuid.UUID(value["task_id"])
        sha256 = value["sha256"]
        byte_size = value["byte_size"]
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ValueError("accepted input metadata is malformed") from exc
    if (
        metadata_task_id != task_id
        or not isinstance(sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", sha256) is None
        or type(byte_size) is not int
        or byte_size <= 0
    ):
        raise ValueError("accepted input metadata is malformed")
    return {
        "input_id": input_id,
        "job_id": job_id,
        "task_id": metadata_task_id,
        "project_id": project_id,
        "sha256": sha256,
        "byte_size": byte_size,
    }


class _PredictionRetryTerminalFailure(Exception):
    def __init__(self, result: dict):
        self.result = result
        super().__init__(str(result.get("reason") or result.get("status") or "failed"))


@celery_app.task(
    bind=True, name="app.workers.predictions_retry.retry_failed_prediction"
)
def retry_failed_prediction(self, failed_id: str, user_id: str) -> dict:
    return asyncio.run(
        _run_retry(failed_id, user_id, celery_task_id=getattr(self.request, "id", None))
    )


async def _run_retry(
    failed_id: str, user_id: str, celery_task_id: str | None = None
) -> dict:
    engine = create_async_engine(settings.database_url, echo=False)
    SessionLocal = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    try:
        return await _do_retry_with_factory(
            SessionLocal, failed_id, user_id, celery_task_id=celery_task_id
        )
    finally:
        await engine.dispose()


async def _do_retry_with_factory(
    session_factory,
    failed_id: str,
    user_id: str,
    celery_task_id: str | None = None,
) -> dict:
    """实际 retry 逻辑；session_factory 暴露便于测试 mock。"""
    from app.services import async_job as async_job_svc
    from app.services.async_job_notify import notify_job_terminal
    from app.services.prediction import PredictionService

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
                result = await _run_retry_attempt(
                    session_factory,
                    failed_id,
                    user_id,
                    job_id=job.id,
                )
                await async_job_svc.replace_result(
                    db, job.id, result=_retry_result_payload(result)
                )
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
                await PredictionService(retry_db).bump_failed_prediction_retry_counter(
                    fid
                )
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


async def _run_retry_attempt(
    session_factory, failed_id: str, user_id: str, *, job_id: uuid.UUID
) -> dict:
    """重试业务逻辑。终态通知由外层 async_jobs 统一发。"""
    from app.db.models.ml_backend_registry import MLBackendRegistry as MLBackend
    from app.db.models.prediction import FailedPrediction
    from app.db.models.task import Task
    from app.services.gpu_arbitration.dispatch import (
        build_gpu_dispatch_context_factory,
    )
    from app.services.gpu_arbitration.contracts import gpu_arbiter_failure_record
    from app.services.ml_routing.client import RoutedMLBackendClient
    from app.services.notification import NotificationService
    from app.services.prediction import PredictionService
    from app.services.labeling_write_scope import capture_labeling_write_scope
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
        failure_extra = fp.extra if isinstance(fp.extra, dict) else {}
        stored_context = failure_extra.get("request_context")
        request_context = stored_context if isinstance(stored_context, dict) else None
        raw_accepted_input = failure_extra.get("accepted_input")
        accepted_input = None
        if "accepted_input" in failure_extra:
            if task is None:
                raise ValueError("accepted input retry task is unavailable")
            accepted_input = _accepted_input_metadata(
                raw_accepted_input, task_id=task.id, project_id=fp.project_id
            )
        elif (
            failure_extra.get("execution_scope") == "workbench"
            and failure_extra.get("execution_unit") not in {"frame", "video", "scene"}
            and task is not None
            and task.file_type == "image"
            and failure_extra.get("task") not in {"text", "tracker"}
        ):
            raise ValueError("legacy Workbench image retry has no accepted input")
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

    if not task or not backend:
        async with session_factory() as db:
            await PredictionService(db).bump_failed_prediction_retry_counter(fid)
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

            async def predict_with_input(input_url: str):
                task_input = {"id": str(task.id), "file_path": input_url}
                if request_context is None:
                    return await client.predict([task_input])
                return await client.predict([task_input], context=request_context)

            if accepted_input is None:
                results = await predict_with_input(resolve_task_url(task))
            else:
                from aap_assets_inputs import contract as accepted_inputs_contract
                from aap_assets_inputs.contract import InputFailure
                from app.services.accepted_inputs import input_gateway

                input_request = accepted_inputs_contract.InputRequest(
                    input_id=accepted_input["input_id"],
                    job_id=accepted_input["job_id"],
                    task_id=accepted_input["task_id"],
                )
                async with input_gateway(session_factory=session_factory) as gateway:
                    reference = await gateway.reference(input_request)
                    record = reference.input
                    if (
                        record.project_id != accepted_input["project_id"]
                        or record.sha256 != accepted_input["sha256"]
                        or record.byte_size != accepted_input["byte_size"]
                    ):
                        raise InputFailure("input_corrupt")
                    with tempfile.TemporaryDirectory(
                        prefix="aap-accepted-retry-"
                    ) as directory:
                        await gateway.download(reference, Path(directory) / "source")
                        results = await predict_with_input(reference.url)
                        await _verify_retry_source(gateway, input_request)
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
            await PredictionService(db).bump_failed_prediction_retry_counter(
                fid, gpu_arbiter_error=gpu_arbiter_error
            )
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
        prediction_writer: ApplyPredictionResultPort = pred_svc
        write_request = ApplyPredictionResultRequest(
            task_id=task.id,
            project_id=task.project_id,
            ml_backend_id=executed_backend_id,
            result=first.result,
            score=first.score,
            model_version=first.model_version,
            inference_time_ms=first.inference_time_ms,
            ml_backend_pool_id=retry_pool_id,
            token_meta=first.meta,
            write_scope=capture_labeling_write_scope(
                task,
                actor_id=uid,
                actor_known=True,
                operation="prediction_retry",
                execution_scope=None,
                execution_scope_known=True,
                job_id=job_id,
                job_id_known=True,
                accepted_input_id=(
                    accepted_input["input_id"] if accepted_input else None
                ),
            ),
            pipeline_extra=(
                {
                    "accepted_input": {
                        "input_id": str(accepted_input["input_id"]),
                        "job_id": str(accepted_input["job_id"]),
                        "task_id": str(accepted_input["task_id"]),
                        "sha256": accepted_input["sha256"],
                        "byte_size": accepted_input["byte_size"],
                    }
                }
                if accepted_input
                else None
            ),
            **(
                {"model_provenance": request_context["frozen_model_ref"]}
                if request_context and request_context.get("frozen_model_ref")
                else {}
            ),
        )
        pred = await _apply_retry_prediction_with_retries(
            prediction_writer, db, write_request
        )
        # 删除 failed_prediction 行
        await pred_svc.delete_failed_prediction_after_retry(fid)
        await db.commit()

    return {
        "status": "succeeded",
        "failed_id": failed_id,
        "prediction_id": str(pred.id),
    }
