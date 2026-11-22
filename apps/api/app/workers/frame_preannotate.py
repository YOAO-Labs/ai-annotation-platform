"""Retained-video frame detection fan-out for ``execution_unit=frame``."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import uuid
from dataclasses import asdict

import redis
from fastapi import HTTPException
from redis.exceptions import RedisError
from app.config import settings
from app.application.preannotation_contract import (
    FramePreannotationDispatchReceipt,
    FramePreannotationDispatchRequest,
    PreparedFramePreannotationDispatch,
    VideoFrameTaskPlan,
)
from app.services.labeling_prediction_contract import (
    ApplyPredictionResultError,
    ApplyPredictionResultPort,
    ApplyPredictionResultRequest,
)
from app.services.jobs_lifecycle_data import job_lock_statement
from app.workers.celery_app import celery_app

_WRITE_ATTEMPTS = 5
_RECEIPTS_FIELD = "frame_segment_receipts"
_LEGACY_RECEIPTS_UNAVAILABLE = "frame_segment_receipts_unavailable"
log = logging.getLogger(__name__)


class _FrameWriteDeferred(Exception):
    def __init__(self, resume: dict):
        super().__init__("Frame prediction is waiting for a write lock")
        self.resume = resume


def _segment_id(segment: dict) -> str:
    parts = (
        segment["task_id"],
        segment["input_id"],
        segment["input_job_id"],
        segment["plan_digest"],
        *(str(ordinal) for ordinal in segment["frame_ordinals"]),
    )
    return hashlib.sha256(("\n".join(parts) + "\n").encode("ascii")).hexdigest()


def plan_video_task_segments(
    plans: tuple[VideoFrameTaskPlan, ...], chunk_size: int
) -> tuple[list[dict], int]:
    """Chunk frozen selected ordinals without consulting mutable video metadata."""
    from yoao_assets_video_inputs import values as video_values

    size = min(max(1, int(chunk_size)), video_values.PAGE_SIZE)
    segments = []
    total = 0
    seen_tasks = set()
    for plan in plans:
        if type(plan.selected_count) is not int or plan.selected_count < 1:
            raise ValueError("retained video plan has no selected frames")
        if plan.task_id in seen_tasks:
            raise ValueError("retained video manifest repeated a Task")
        seen_tasks.add(plan.task_id)
        total += plan.selected_count
        for start in range(1, plan.selected_count + 1, size):
            segments.append(
                {
                    "task_id": plan.task_id,
                    "input_id": plan.input_id,
                    "input_job_id": plan.input_job_id,
                    "plan_digest": plan.plan_digest,
                    "frame_ordinals": list(
                        range(start, min(plan.selected_count + 1, start + size))
                    ),
                }
            )
    return segments, total


def _progress_key(job_id: str) -> str:
    return f"frame_preannotate:{job_id}:done"


def _progress_total_key(job_id: str) -> str:
    return f"frame_preannotate:{job_id}:total"


def _segment_results_key(job_id: str) -> str:
    return f"frame_preannotate:{job_id}:segments"


_CACHE_PROGRESS_LUA = """
local done = tonumber(redis.call('GET', KEYS[1]) or '0')
redis.call('SET', KEYS[1], math.max(done, tonumber(ARGV[1])), 'EX', 86400)
redis.call('SET', KEYS[2], ARGV[2], 'EX', 86400)
"""


def _cache_progress(job_id: str, done: int, total: int) -> None:
    try:
        with redis.from_url(settings.redis_url) as progress:
            progress.eval(
                _CACHE_PROGRESS_LUA,
                2,
                _progress_key(job_id),
                _progress_total_key(job_id),
                done,
                total,
            )
    except RedisError as exc:
        log.warning("Frame job %s progress cache write failed: %s", job_id, exc)


def _read_progress(job_id: str) -> tuple[int, int]:
    try:
        with redis.from_url(settings.redis_url) as progress:
            done, total = progress.mget(
                _progress_key(job_id), _progress_total_key(job_id)
            )
        return int(done or 0), int(total or 0)
    except RedisError as exc:
        log.warning("Frame job %s progress cache read failed: %s", job_id, exc)
        return 0, 0


_SEGMENT_RESULT_MAX_FAILURE_IDS = 100
_SEGMENT_RESULT_MAX_GPU_FAILURES = 32


def _bounded_segment_stats(stats: dict) -> dict:
    failed_ids = stats.get("failed_prediction_ids")
    if not isinstance(failed_ids, list):
        failed_ids = []
    bounded = {
        "task_id": str(stats.get("task_id", ""))[:36],
        "frames_done": _safe_segment_count(stats.get("frames_done")),
        "boxes": _safe_segment_count(stats.get("boxes")),
        "failed": _safe_segment_count(stats.get("failed")),
        "skipped": _safe_segment_count(stats.get("skipped")),
        "failed_prediction_ids": [
            str(value)[:36] for value in failed_ids[:_SEGMENT_RESULT_MAX_FAILURE_IDS]
        ],
    }
    failures = stats.get("gpu_arbiter_failures")
    if isinstance(failures, list):
        summary = []
        for failure in failures[:_SEGMENT_RESULT_MAX_GPU_FAILURES]:
            if not isinstance(failure, dict):
                continue
            item = {}
            code = failure.get("error_code")
            if isinstance(code, str):
                item["error_code"] = code[:64]
            for name in ("status_code", "retry_after_s", "count"):
                value = failure.get(name)
                if type(value) is int and 0 <= value <= 2**53 - 1:
                    item[name] = value
            if item:
                summary.append(item)
        if summary:
            bounded["gpu_arbiter_failures"] = summary
    return bounded


def _safe_segment_count(value) -> int:
    if type(value) is int and 0 <= value <= 2**53 - 1:
        return value
    return 0


def _deserialize_segment_stats(encoded) -> dict:
    try:
        if isinstance(encoded, bytes):
            encoded = encoded.decode("utf-8")
        document = json.loads(encoded)
        if (
            not isinstance(document, dict)
            or document.get("version") != 1
            or not isinstance(document.get("stats"), dict)
        ):
            raise ValueError("invalid receipt document")
        return _bounded_segment_stats(document["stats"])
    except (UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError):
        raise RuntimeError("invalid frame segment receipt in Redis") from None


class JobsFramePreannotationDispatcher:
    async def prepare(
        self, request: FramePreannotationDispatchRequest, *, db
    ) -> PreparedFramePreannotationDispatch:
        """Freeze the canvas with the parent's running/preparation transaction."""
        from yoao_jobs.queue_contract import encode_canvas_request

        from app.services import async_job as async_job_svc
        from app.services.jobs_dispatch import jobs_dispatch_data

        if (
            not request.actor_id
            or not request.accepted_video_manifest_id
            or not request.accepted_video_selection_digest
            or not request.accepted_video_sealed_digest
        ):
            raise ValueError("frame dispatch requires a sealed retained-video scope")
        uuid.UUID(request.actor_id)
        job_id = uuid.UUID(request.job_id)
        uuid.UUID(request.accepted_video_manifest_id)
        segments, total_frames = plan_video_task_segments(
            request.video_task_plans, settings.frame_preannotate_chunk_size
        )
        if not segments:
            return PreparedFramePreannotationDispatch(
                lease=None, segment_ids=(), total_frames=0
            )
        callback_id = str(uuid.uuid4())
        tasks = [
            {
                "task_name": "app.workers.frame_preannotate.predict_video_segment",
                "execution_id": str(uuid.uuid4()),
                "args": [segment],
                "kwargs": {
                    "project_id": request.project_id,
                    "ml_backend_id": request.ml_backend_id,
                    "stage0": request.stage0,
                    "actor_id": request.actor_id,
                    "job_id": request.job_id,
                    "accepted_video_manifest_id": request.accepted_video_manifest_id,
                    "accepted_video_selection_digest": (
                        request.accepted_video_selection_digest
                    ),
                    "accepted_video_sealed_digest": request.accepted_video_sealed_digest,
                    "celery_root_task_id": request.root_execution_id,
                },
                "options": {
                    "soft_time_limit": (
                        settings.frame_preannotate_segment_soft_time_limit_seconds
                    )
                },
            }
            for segment in segments
        ]
        callback = {
            "task_name": "app.workers.frame_preannotate.finalize_frame_job",
            "execution_id": callback_id,
            "args": [],
            "kwargs": {"project_id": request.project_id, "job_id": request.job_id},
            "options": {},
        }
        envelope = json.loads(
            encode_canvas_request(callback_id, str(uuid.uuid4()), tasks, callback)
        )
        await db.flush()
        lease = await jobs_dispatch_data(db).prepare(
            job_id, envelope, purpose="frame_canvas"
        )
        await async_job_svc.update_payload(db, job_id, {_RECEIPTS_FIELD: {}})
        return PreparedFramePreannotationDispatch(
            lease=lease,
            segment_ids=tuple(_segment_id(segment) for segment in segments),
            total_frames=total_frames,
        )

    async def __call__(
        self, prepared: PreparedFramePreannotationDispatch, *, db
    ) -> FramePreannotationDispatchReceipt:
        from app.services.jobs_dispatch import publish_prepared_dispatch
        from app.services.jobs_queue import publish_queue_envelope

        lease = prepared.lease
        if lease is None:
            return FramePreannotationDispatchReceipt(
                dispatched=False, segments=0, total_frames=0
            )
        _cache_progress(str(lease.job_id), 0, prepared.total_frames)
        await publish_prepared_dispatch(
            db, lease, publish=lambda: publish_queue_envelope(lease.envelope)
        )
        return FramePreannotationDispatchReceipt(
            dispatched=True,
            segments=len(prepared.segment_ids),
            total_frames=prepared.total_frames,
        )


@celery_app.task(
    bind=True,
    max_retries=2,
    default_retry_delay=30,
    soft_time_limit=settings.frame_preannotate_segment_soft_time_limit_seconds,
)
def extract_frames_task(
    self,
    task_id: str,
    frame_indices: list[int],
    *,
    job_id: str | None = None,
) -> dict:
    """Reject stale pre-retained-source extraction messages fail-closed."""
    if job_id:
        asyncio.run(
            _fail_legacy_frame_job(job_id, "frame_job_requires_retained_video_manifest")
        )
    return {"task_id": task_id, "requested": len(frame_indices), "extracted": 0}


@celery_app.task
def launch_predict_phase(
    extract_results: list[dict],
    *,
    project_id: str,
    ml_backend_id: str,
    stage0: dict,
    job_id: str,
    segments: list[dict],
    celery_root_task_id: str | None = None,
) -> None:
    """Reject stale two-stage chord callbacks that lack retained input evidence."""
    asyncio.run(
        _fail_legacy_frame_job(job_id, "frame_job_requires_retained_video_manifest")
    )


@celery_app.task(
    bind=True,
    max_retries=None,
    default_retry_delay=30,
    soft_time_limit=settings.frame_preannotate_segment_soft_time_limit_seconds,
)
def predict_video_segment(
    self,
    segment: dict,
    *,
    project_id: str,
    ml_backend_id: str,
    stage0: dict,
    actor_id: str,
    job_id: str,
    accepted_video_manifest_id: str,
    accepted_video_selection_digest: str,
    accepted_video_sealed_digest: str,
    celery_root_task_id: str | None = None,
    resume: dict | None = None,
) -> dict:
    """Infer frozen retained-video ordinals and write their frame results.

    The active execution Job and the original retained-input Job stay separate in
    the segment facts. Signed frame URLs are fetched immediately before inference.
    """
    try:
        return asyncio.run(
            _run_segment(
                segment=segment,
                project_id=project_id,
                ml_backend_id=ml_backend_id,
                stage0=stage0,
                actor_id=actor_id,
                job_id=job_id,
                accepted_video_manifest_id=accepted_video_manifest_id,
                accepted_video_selection_digest=accepted_video_selection_digest,
                accepted_video_sealed_digest=accepted_video_sealed_digest,
                celery_root_task_id=celery_root_task_id,
                resume=resume,
            )
        )
    except _FrameWriteDeferred as exc:
        raise self.retry(
            args=(segment,),
            kwargs={
                "project_id": project_id,
                "ml_backend_id": ml_backend_id,
                "stage0": stage0,
                "actor_id": actor_id,
                "job_id": job_id,
                "accepted_video_manifest_id": accepted_video_manifest_id,
                "accepted_video_selection_digest": accepted_video_selection_digest,
                "accepted_video_sealed_digest": accepted_video_sealed_digest,
                "celery_root_task_id": celery_root_task_id,
                "resume": exc.resume,
            },
            countdown=random.uniform(1, min(30, 2 ** min(self.request.retries + 1, 5))),
        ) from exc


class _AuthorityRevoked(Exception):
    pass


def _transient_video_input_error(code: str) -> bool:
    return code in {
        "account_busy",
        "operation_busy",
        "project_busy",
        "membership_busy",
        "batch_busy",
        "task_busy",
        "job_busy",
        "input_busy",
        "source_busy",
        "reference_busy",
        "frame_busy",
        "frame_not_ready",
        "input_not_ready",
        "storage_unavailable",
        "timeout",
    }


def _write_scope_matches_video_source(write_scope, plan) -> bool:
    task = plan.input.task
    return (
        write_scope.task_id == task.task_id
        and write_scope.project_id == task.project_id
        and write_scope.task_source_generation == task.task_source_generation
        and write_scope.source_dataset_item_id == task.dataset_item_id
        and write_scope.source_file_path == task.file_path
        and write_scope.source_file_type == task.file_type
    )


async def _read_video_time_entries(gateway, plan, *, actor_id, ordinals):
    from yoao_assets_video_inputs import values as video_values

    if not ordinals or any(type(value) is not int or value < 1 for value in ordinals):
        raise ValueError("frame segment contains invalid selected ordinals")
    if ordinals != sorted(set(ordinals)) or max(ordinals) > plan.selected_count:
        raise ValueError("frame segment escaped its frozen plan")

    groups: list[list[int]] = []
    for ordinal in ordinals:
        if not groups or ordinal != groups[-1][-1] + 1:
            groups.append([ordinal])
        else:
            groups[-1].append(ordinal)

    entries = {}
    for group in groups:
        page = await gateway.times(
            video_values.FramePageRequest(
                actor_id=actor_id,
                job_id=plan.input.job_id,
                task_id=plan.input.task.task_id,
                input_id=plan.input.input_id,
                plan_digest=plan.plan_digest,
                cursor=group[0] - 1,
                page_size=len(group),
            )
        )
        if page.plan != plan or [entry.ordinal for entry in page.entries] != group:
            raise ValueError("selected video timebase changed its frozen cursor")
        entries.update({entry.ordinal: entry for entry in page.entries})
    if set(entries) != set(ordinals):
        raise ValueError("selected video timebase omitted an ordinal")
    return entries


def _defer_frame_segment(
    frame_ordinals,
    write_scope,
    pending_write,
    pending_evidence,
    pending_failure,
    stats,
    gpu_arbiter_failures,
    segment_id,
):
    return _FrameWriteDeferred(
        {
            "frame_ordinals": list(frame_ordinals),
            "write_scope": asdict(write_scope),
            "pending_write": pending_write,
            "video_evidence": (
                pending_evidence.model_dump(mode="json", by_alias=True)
                if pending_evidence is not None
                else None
            ),
            "pending_failure": pending_failure,
            "stats": {
                **stats,
                "failed_prediction_ids": list(stats["failed_prediction_ids"]),
            },
            "gpu_arbiter_failures": list(gpu_arbiter_failures),
            "segment_id": segment_id,
        }
    )


async def _fail_frame_job_in_session(db, job_id: str, project_id: str, error: str):

    from app.services import async_job as async_job_svc
    from app.services.async_job_notify import notify_job_terminal

    await db.rollback()
    job = await db.scalar(
        job_lock_statement(uuid.UUID(job_id), mode="update", populate_existing=True)
    )
    if job is None or job.status not in {"pending", "running"}:
        return
    receipts = (job.payload or {}).get(_RECEIPTS_FIELD)
    await async_job_svc.mark_failed(db, job.id, error=error)
    await notify_job_terminal(db, job_id=job.id)
    await db.commit()
    if receipts is None:
        done, total = _read_progress(job_id)
    else:
        done = sum(_processed_frames(stats) for stats in receipts.values())
        total = int((job.result or {}).get("frame_total", 0))
        _cache_progress(job_id, done, total)
    _publish_frame_progress(project_id, done, total, status="failed", error=error)


async def _fail_legacy_frame_job(job_id: str, error: str) -> None:
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    from app.services import async_job as async_job_svc

    try:
        parsed_job_id = uuid.UUID(job_id)
    except (TypeError, ValueError):
        return
    engine = create_async_engine(settings.database_url, echo=False)
    SessionLocal = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    try:
        async with SessionLocal() as db:
            job = await async_job_svc.get_job(db, parsed_job_id)
            if job is not None:
                await _fail_frame_job_in_session(db, job_id, str(job.project_id), error)
    finally:
        await engine.dispose()


async def _run_segment(
    *,
    segment: dict,
    project_id: str,
    ml_backend_id: str,
    stage0: dict,
    actor_id: str,
    job_id: str,
    accepted_video_manifest_id: str,
    accepted_video_selection_digest: str,
    accepted_video_sealed_digest: str,
    celery_root_task_id: str | None,
    resume: dict | None = None,
) -> dict:
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    from yoao_inference.execution_context import build_predict_context
    from yoao_inference.gpu.contracts import (
        GPUArbiterErrorCode,
        gpu_arbiter_failure_record,
    )
    from yoao_inference_contract.execution import (
        FrozenModelOperation,
        ModelExecutionError,
        thaw_json,
    )
    from app.services.labeling_core_read_relations import get_core_row, task_row
    from app.services import async_job as async_job_svc
    from app.services.inference_model_execution import model_execution_port
    from app.services.ml_backend import MLBackendService
    from app.services.prediction import PredictionService, to_video_bbox_result
    from app.services import prediction_retry
    from app.services.labeling_write_scope import (
        LabelingWriteScope,
        capture_labeling_write_scope,
    )
    from app.services.labeling_write_guard import lock_labeling_write
    from yoao_assets_video_inputs import evidence as video_evidence
    from yoao_assets_video_inputs import execution as video_execution
    from yoao_assets_video_inputs.execution import VideoInputFailure

    task_id = segment["task_id"]
    input_id = segment["input_id"]
    input_job_id = segment["input_job_id"]
    backend_uuid = uuid.UUID(ml_backend_id)
    plan_digest = segment["plan_digest"]
    frame_ordinals: list[int] = list(
        resume["frame_ordinals"] if resume is not None else segment["frame_ordinals"]
    )
    box_cap = settings.frame_preannotate_max_boxes_per_frame
    segment_id = _segment_id(segment)
    if resume is not None and resume.get("segment_id") not in (None, segment_id):
        raise ValueError("frame retry does not match its frozen segment")

    engine = create_async_engine(settings.database_url, echo=False)
    SessionLocal = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    stats = {
        "task_id": task_id,
        "frames_done": 0,
        "boxes": 0,
        "failed": 0,
        "skipped": 0,
        "failed_prediction_ids": [],
    }
    gpu_arbiter_failures: list[dict] = []
    pending_write = resume.get("pending_write") if resume is not None else None
    pending_failure = resume.get("pending_failure") if resume is not None else None
    pending_evidence = None
    if resume is not None:
        stats.update(resume["stats"])
        gpu_arbiter_failures.extend(resume.get("gpu_arbiter_failures", []))
        if resume.get("video_evidence") is not None:
            pending_evidence = video_evidence.VideoInputEvidence.model_validate(
                resume["video_evidence"]
            )
    try:
        async with SessionLocal() as db:
            async_job = await async_job_svc.get_job(
                db, uuid.UUID(job_id), populate_existing=True
            )
            if (
                async_job is not None
                and str(async_job.project_id) == project_id
                and str(async_job.user_id) == actor_id
            ):
                settled = ((async_job.payload or {}).get(_RECEIPTS_FIELD) or {}).get(
                    segment_id
                )
                if settled is not None:
                    return settled
            if await _job_cancelled(db, job_id):
                stats["skipped"] += len(frame_ordinals)
                return await _finish_segment(
                    stats,
                    gpu_arbiter_failures,
                    project_id=project_id,
                    job_id=job_id,
                    segment_id=segment_id,
                )
            if async_job is None:
                return stats

            try:
                project_uuid = uuid.UUID(project_id)
                actor_uuid = uuid.UUID(actor_id)
                active_job_uuid = uuid.UUID(job_id)
                manifest_uuid = uuid.UUID(accepted_video_manifest_id)
                task_uuid = uuid.UUID(task_id)
                input_uuid = uuid.UUID(input_id)
                input_job_uuid = uuid.UUID(input_job_id)
            except (TypeError, ValueError):
                await _fail_frame_job_in_session(
                    db, job_id, project_id, "frame_scope_invalid"
                )
                return stats

            payload = dict(async_job.payload or {})
            selection = payload.get("accepted_video_selection")
            manifest_result = dict(async_job.result or {}).get("video_manifest")
            if (
                async_job.project_id != project_uuid
                or async_job.user_id != actor_uuid
                or payload.get("accepted_video_inputs_protocol")
                != "aap-assets-video-inputs/v1"
                or payload.get("accepted_video_manifest_id") != str(manifest_uuid)
                or payload.get("accepted_video_selection_digest")
                != accepted_video_selection_digest
                or payload.get("execution_unit") != "frame"
                or not isinstance(selection, dict)
                or not isinstance(manifest_result, dict)
                or manifest_result.get("manifest_id") != accepted_video_manifest_id
                or manifest_result.get("selection_digest")
                != accepted_video_selection_digest
                or manifest_result.get("sealed_digest") != accepted_video_sealed_digest
            ):
                await _fail_frame_job_in_session(
                    db, job_id, project_id, "frame_manifest_scope_mismatch"
                )
                return stats

            task = await get_core_row(db, task_row, task_uuid, populate_existing=True)
            selected_task_ids = selection.get("task_ids")
            selected_batch_id = selection.get("batch_id")
            if (
                task is None
                or task.project_id != project_uuid
                or (
                    selected_task_ids is not None
                    and str(task_uuid) not in selected_task_ids
                )
                or (
                    selected_batch_id is not None
                    and str(task.batch_id) != selected_batch_id
                )
            ):
                await _fail_frame_job_in_session(
                    db, job_id, project_id, "frame_task_scope_unavailable"
                )
                return stats
            execution_scope = payload.get("execution_scope")
            if resume is not None:
                write_scope = LabelingWriteScope(**resume["write_scope"])
            else:
                write_scope = capture_labeling_write_scope(
                    task,
                    actor_id=async_job.user_id,
                    actor_known=True,
                    operation="preannotation",
                    execution_scope=(
                        execution_scope if isinstance(execution_scope, str) else None
                    ),
                    execution_scope_known=isinstance(execution_scope, str),
                    job_id=active_job_uuid,
                    job_id_known=True,
                )

            from app.application.preannotation_execution import (
                _preannotation_actor_revoked,
            )

            if await _preannotation_actor_revoked(
                db, actor_uuid, project_uuid, execution_scope="bulk"
            ):
                await _fail_frame_job_in_session(
                    db, job_id, project_id, "preannotation_actor_revoked"
                )
                return stats

            done_ordinals = await _existing_video_frame_ordinals(
                db,
                task_id=task_uuid,
                execution_job_id=active_job_uuid,
                input_id=input_uuid,
                plan_digest=plan_digest,
            )
            remaining_ordinals = []
            for ordinal in frame_ordinals:
                if ordinal in done_ordinals:
                    stats["skipped"] += 1
                    if pending_write is not None and pending_evidence is not None:
                        if pending_evidence.entry.ordinal == ordinal:
                            pending_write = None
                            pending_evidence = None
                    continue
                remaining_ordinals.append(ordinal)
            frame_ordinals = remaining_ordinals
            if not frame_ordinals:
                return await _finish_segment(
                    stats,
                    gpu_arbiter_failures,
                    project_id=project_id,
                    job_id=job_id,
                    segment_id=segment_id,
                )

            from app.services.accepted_video_inputs import video_input_gateway

            async with video_input_gateway(session_factory=SessionLocal) as gateway:
                read_request = video_execution.ReadVideoInputRequest(
                    input_id=input_uuid,
                    job_id=input_job_uuid,
                    task_id=task_uuid,
                    actor_id=actor_uuid,
                )
                try:
                    input_record = await gateway.input(read_request)
                except VideoInputFailure as exc:
                    if _transient_video_input_error(exc.code):
                        raise _defer_frame_segment(
                            frame_ordinals,
                            write_scope,
                            pending_write,
                            pending_evidence,
                            pending_failure,
                            stats,
                            gpu_arbiter_failures,
                            segment_id,
                        ) from exc
                    await _fail_frame_job_in_session(
                        db, job_id, project_id, f"accepted_video_input_{exc.code}"
                    )
                    return stats
                plan = input_record.plan
                if (
                    input_record.input_id != input_uuid
                    or input_record.job_id != input_job_uuid
                    or input_record.task.task_id != task_uuid
                    or plan is None
                    or plan.plan_digest != plan_digest
                    or plan.selected_count < max(frame_ordinals)
                ):
                    await _fail_frame_job_in_session(
                        db, job_id, project_id, "accepted_video_plan_mismatch"
                    )
                    return stats

                if not _write_scope_matches_video_source(write_scope, plan):
                    await _fail_frame_job_in_session(
                        db, job_id, project_id, "prediction_source_changed"
                    )
                    return stats

                try:
                    entries = await _read_video_time_entries(
                        gateway,
                        plan,
                        actor_id=actor_uuid,
                        ordinals=frame_ordinals,
                    )
                except VideoInputFailure as exc:
                    if _transient_video_input_error(exc.code):
                        raise _defer_frame_segment(
                            frame_ordinals,
                            write_scope,
                            pending_write,
                            pending_evidence,
                            pending_failure,
                            stats,
                            gpu_arbiter_failures,
                            segment_id,
                        ) from exc
                    await _fail_frame_job_in_session(
                        db,
                        job_id,
                        project_id,
                        f"accepted_video_timebase_{exc.code}",
                    )
                    return stats
                source_failure = None
                try:
                    verified = await gateway.verify(
                        video_execution.LockVideoInputRequest(
                            input_id=input_uuid,
                            job_id=input_job_uuid,
                            task_id=task_uuid,
                            actor_id=actor_uuid,
                            plan_digest=plan_digest,
                        )
                    )
                    if verified.plan != plan or verified.status != "ready":
                        source_failure = "input_not_ready"
                except VideoInputFailure as exc:
                    if _transient_video_input_error(exc.code):
                        raise _defer_frame_segment(
                            frame_ordinals,
                            write_scope,
                            pending_write,
                            pending_evidence,
                            pending_failure,
                            stats,
                            gpu_arbiter_failures,
                            segment_id,
                        ) from exc
                    source_failure = exc.code

                pred_svc: ApplyPredictionResultPort = PredictionService(db)
                backend_service = MLBackendService(db)
                backend = await backend_service.get_registry(backend_uuid)
                if backend is None:
                    await _fail_frame_job_in_session(
                        db, job_id, project_id, "ml_backend_unavailable"
                    )
                    return stats
                try:
                    source_pool_id = await backend_service.pool_id_for_registry(
                        backend_uuid
                    )
                except Exception:
                    source_pool_id = None
                context = build_predict_context(
                    prompt=None,
                    output_mode="box",
                    params=stage0.get("params"),
                    model_id=stage0.get("model_id"),
                    task=stage0.get("task"),
                    task_type=stage0.get("task_type"),
                    prompt_type=stage0.get("prompt_type"),
                    model_variants=stage0.get("model_variants"),
                    class_filter=stage0.get("class_filter"),
                    box_threshold=None,
                    text_threshold=None,
                )
                retry_request_context = prediction_retry.retryable_request_context(
                    context
                )
                execution_port = model_execution_port(db)
                current_provenance = None

                gpu_error_codes = frozenset(code.value for code in GPUArbiterErrorCode)

                def model_failure_provenance(error):
                    if isinstance(error, ModelExecutionError):
                        return error.failure.provenance
                    return None

                def model_gpu_failure_record(error):
                    cause = error
                    while cause is not None:
                        record = gpu_arbiter_failure_record(cause)
                        if record is not None:
                            return record
                        cause = cause.__cause__
                    if not isinstance(error, ModelExecutionError):
                        return None
                    failure = error.failure
                    if failure.code not in gpu_error_codes:
                        return None
                    detail = thaw_json(failure.detail)
                    message = (
                        detail.get("message") if isinstance(detail, dict) else None
                    )
                    retry_after_s = None
                    if failure.retry_after is not None:
                        try:
                            retry_after_s = int(failure.retry_after)
                        except (TypeError, ValueError):
                            retry_after_s = None
                    return {
                        "error_code": failure.code,
                        "status_code": failure.status_code,
                        "retry_after_s": retry_after_s,
                        "message": message or failure.code,
                    }

                async def record_failure(
                    ordinal: int,
                    entry,
                    error: Exception,
                    *,
                    frame_record=None,
                    gpu_error=None,
                ) -> None:
                    gpu_error = gpu_error or model_gpu_failure_record(error)
                    model_failure = (
                        error.failure
                        if isinstance(error, ModelExecutionError)
                        else None
                    )
                    selected_provenance = (
                        model_failure_provenance(error) or current_provenance
                    )
                    evidence = video_evidence.failure_evidence(
                        plan,
                        entry,
                        frame_id=(
                            frame_record.frame_id if frame_record is not None else None
                        ),
                        frame_sha256=(
                            frame_record.blob.sha256
                            if frame_record is not None
                            and frame_record.blob is not None
                            else None
                        ),
                    )
                    error_type = (
                        gpu_error["error_code"]
                        if gpu_error is not None
                        else (
                            model_failure.code
                            if model_failure is not None
                            else (
                                error.code
                                if isinstance(error, VideoInputFailure)
                                else type(error).__name__
                            )
                        )
                    )
                    model_failure_detail = (
                        thaw_json(model_failure.detail)
                        if model_failure is not None
                        else None
                    )
                    model_failure_wire = (
                        {
                            "code": model_failure.code,
                            "status_code": model_failure.status_code,
                            "detail": model_failure_detail,
                            "retry_after": model_failure.retry_after,
                        }
                        if model_failure is not None
                        else None
                    )
                    model_failure_message = (
                        (model_failure_detail.get("message") or model_failure.code)
                        if isinstance(model_failure_detail, dict)
                        else (
                            str(model_failure_detail)
                            if model_failure_detail is not None
                            else None
                        )
                    )
                    extra = {
                        **video_evidence.prediction_extra(evidence),
                        "accepted_video_execution_job_id": str(active_job_uuid),
                        "execution_unit": "frame",
                        "execution_scope": write_scope.execution_scope,
                        "task": stage0.get("task"),
                        **(
                            {"request_context": retry_request_context}
                            if retry_request_context is not None
                            else {}
                        ),
                        **({"gpu_arbiter_error": gpu_error} if gpu_error else {}),
                        **(
                            {"model_execution_failure": model_failure_wire}
                            if model_failure_wire is not None
                            else {}
                        ),
                    }
                    failure = {
                        "error_type": str(error_type),
                        "message": (
                            gpu_error["message"]
                            if gpu_error is not None
                            else (model_failure_message or str(error))
                        ),
                        "extra": extra,
                        "ml_backend_id": (
                            selected_provenance.instance_id
                            if selected_provenance is not None
                            else backend_uuid
                        ),
                        "ml_backend_pool_id": (
                            selected_provenance.pool_id
                            if selected_provenance is not None
                            else source_pool_id
                        ),
                        "model_version": None,
                        "evidence": evidence.model_dump(mode="json", by_alias=True),
                        "gpu_arbiter_error": gpu_error,
                    }
                    try:
                        await lock_labeling_write(
                            db,
                            write_scope,
                            operations=("preannotation",),
                            accepted_video_plan=plan,
                        )
                    except HTTPException as exc:
                        await db.rollback()
                        if exc.status_code in {401, 403}:
                            await _fail_frame_job_in_session(
                                db,
                                job_id,
                                project_id,
                                "preannotation_actor_revoked",
                            )
                            raise _AuthorityRevoked from exc
                        detail = exc.detail
                        reason = (
                            detail.get("reason") if isinstance(detail, dict) else None
                        )
                        if reason in {"task_busy", "job_busy", "input_busy"}:
                            raise _defer_frame_segment(
                                frame_ordinals[frame_ordinals.index(ordinal) :],
                                write_scope,
                                None,
                                None,
                                failure,
                                stats,
                                gpu_arbiter_failures,
                                segment_id,
                            ) from exc
                        await _fail_frame_job_in_session(
                            db,
                            job_id,
                            project_id,
                            "preannotation_scope_changed",
                        )
                        raise _AuthorityRevoked from exc
                    failed = await pred_svc.create_failed(
                        task_id=task_uuid,
                        project_id=project_uuid,
                        ml_backend_id=failure["ml_backend_id"],
                        error_type=failure["error_type"],
                        message=failure["message"],
                        model_version=failure["model_version"],
                        extra=failure["extra"],
                        ml_backend_pool_id=failure["ml_backend_pool_id"],
                    )
                    await db.commit()
                    stats["failed"] += 1
                    stats["failed_prediction_ids"].append(str(failed.id))
                    if gpu_error is not None:
                        gpu_arbiter_failures.append(gpu_error)

                if source_failure is not None:
                    for ordinal in frame_ordinals:
                        if await _job_cancelled(db, job_id):
                            stats["skipped"] += len(
                                frame_ordinals
                            ) - frame_ordinals.index(ordinal)
                            break
                        await record_failure(
                            ordinal,
                            entries[ordinal],
                            VideoInputFailure(source_failure),
                        )
                    frame_ordinals = []

                for index, ordinal in enumerate(frame_ordinals):
                    if await _job_cancelled(db, job_id):
                        stats["skipped"] += len(frame_ordinals) - index
                        break
                    current_provenance = None
                    if pending_failure is not None:
                        failure = pending_failure
                        pending_failure = None
                        evidence = (
                            video_evidence.VideoInputFailureEvidence.model_validate(
                                failure["evidence"]
                            )
                        )
                        try:
                            await lock_labeling_write(
                                db,
                                write_scope,
                                operations=("preannotation",),
                                accepted_video_plan=plan,
                            )
                            failed = await pred_svc.create_failed(
                                task_id=task_uuid,
                                project_id=project_uuid,
                                ml_backend_id=(
                                    uuid.UUID(failure["ml_backend_id"])
                                    if failure.get("ml_backend_id")
                                    else None
                                ),
                                ml_backend_pool_id=(
                                    uuid.UUID(failure["ml_backend_pool_id"])
                                    if failure.get("ml_backend_pool_id")
                                    else None
                                ),
                                error_type=failure["error_type"],
                                message=failure["message"],
                                extra={
                                    **video_evidence.prediction_extra(evidence),
                                    **failure["extra"],
                                },
                            )
                            await db.commit()
                            stats["failed"] += 1
                            stats["failed_prediction_ids"].append(str(failed.id))
                            if failure.get("gpu_arbiter_error") is not None:
                                gpu_arbiter_failures.append(
                                    failure["gpu_arbiter_error"]
                                )
                            continue
                        except HTTPException as exc:
                            await db.rollback()
                            if exc.status_code in {401, 403}:
                                await _fail_frame_job_in_session(
                                    db,
                                    job_id,
                                    project_id,
                                    "preannotation_actor_revoked",
                                )
                                raise _AuthorityRevoked from exc
                            detail = exc.detail
                            reason = (
                                detail.get("reason")
                                if isinstance(detail, dict)
                                else None
                            )
                            if reason in {
                                "account_busy",
                                "project_busy",
                                "membership_busy",
                                "batch_busy",
                                "task_busy",
                                "job_busy",
                                "input_busy",
                            }:
                                raise _defer_frame_segment(
                                    frame_ordinals[index:],
                                    write_scope,
                                    None,
                                    None,
                                    failure,
                                    stats,
                                    gpu_arbiter_failures,
                                    segment_id,
                                ) from exc
                            await _fail_frame_job_in_session(
                                db,
                                job_id,
                                project_id,
                                "preannotation_scope_changed",
                            )
                            raise _AuthorityRevoked from exc
                    if pending_write is None:
                        request = video_execution.VideoFrameRequest(
                            actor_id=actor_uuid,
                            job_id=input_job_uuid,
                            task_id=task_uuid,
                            input_id=input_uuid,
                            plan_digest=plan_digest,
                            ordinal=ordinal,
                        )
                        try:
                            reference = await gateway.ensure_reference(request)
                            frame_record = await gateway.frame(request)
                        except VideoInputFailure as exc:
                            if _transient_video_input_error(exc.code):
                                raise _defer_frame_segment(
                                    frame_ordinals[index:],
                                    write_scope,
                                    None,
                                    None,
                                    None,
                                    stats,
                                    gpu_arbiter_failures,
                                    segment_id,
                                ) from exc
                            await db.rollback()
                            frame_record = None
                            gpu_error = model_gpu_failure_record(exc)
                            await record_failure(
                                ordinal,
                                entries[ordinal],
                                exc,
                                frame_record=frame_record,
                                gpu_error=gpu_error,
                            )
                            continue
                        if (
                            reference.plan != plan
                            or reference.entry != entries[ordinal]
                            or frame_record.plan != plan
                            or frame_record.entry != reference.entry
                            or frame_record.status != "ready"
                            or frame_record.blob != reference.blob
                        ):
                            error = VideoInputFailure("invalid_response")
                            await record_failure(ordinal, entries[ordinal], error)
                            continue
                        ready = video_evidence.ready_evidence(plan, frame_record)
                        try:
                            frame_task_id = f"{task_id}:{reference.entry.frame_index}"
                            operation_result = await execution_port.execute(
                                FrozenModelOperation(
                                    request_id=uuid.uuid4(),
                                    operation="batch_predict",
                                    task_inputs=(
                                        {
                                            "id": frame_task_id,
                                            "file_path": reference.url,
                                        },
                                    ),
                                    context=context,
                                    project_id=str(project_uuid),
                                    requested_registry_id=str(backend_uuid),
                                    requested_pool_id=(
                                        str(source_pool_id)
                                        if source_pool_id is not None
                                        else None
                                    ),
                                    endpoint_registry_id=None,
                                    required_instance_id=None,
                                    route_owner=f"frame:{job_id}:{task_id}",
                                    route_operation="frame_predict",
                                    correlation={
                                        "accepted_video_input_job_id": str(
                                            input_job_uuid
                                        ),
                                        "accepted_video_plan_digest": plan_digest,
                                        "frame_ordinal": ordinal,
                                        "frame_index": reference.entry.frame_index,
                                    },
                                    input_provenance=ready.model_dump(
                                        mode="json", by_alias=True
                                    ),
                                )
                            )
                            if (
                                len(operation_result.results) != 1
                                or operation_result.results[0].task_id != frame_task_id
                            ):
                                raise RuntimeError(
                                    "Frame prediction result identity mismatch"
                                )
                            current_provenance = operation_result.provenance
                            prediction = operation_result.results[0]
                            raw = thaw_json(prediction.result)
                            token_meta = thaw_json(prediction.meta)
                            pending_write = {
                                "task_id": write_scope.task_id,
                                "project_id": project_uuid,
                                "ml_backend_id": (
                                    operation_result.provenance.instance_id
                                ),
                                "result": to_video_bbox_result(
                                    raw if isinstance(raw, list) else [],
                                    reference.entry.frame_index,
                                )[:box_cap],
                                "score": prediction.score,
                                "model_version": prediction.model_version,
                                "inference_time_ms": prediction.inference_time_ms,
                                "token_meta": token_meta,
                                "ml_backend_pool_id": (
                                    operation_result.provenance.pool_id
                                ),
                            }
                            pending_evidence = ready
                        except Exception as exc:  # noqa: BLE001
                            await db.rollback()
                            gpu_error = model_gpu_failure_record(exc)
                            await record_failure(
                                ordinal,
                                reference.entry,
                                exc,
                                frame_record=frame_record,
                                gpu_error=gpu_error,
                            )
                            continue

                    if pending_write is None or pending_evidence is None:
                        continue
                    try:
                        current_source = await gateway.verify(
                            video_execution.LockVideoInputRequest(
                                input_id=input_uuid,
                                job_id=input_job_uuid,
                                task_id=task_uuid,
                                actor_id=actor_uuid,
                                plan_digest=plan_digest,
                            )
                        )
                        if (
                            current_source.status != "ready"
                            or current_source.plan != plan
                        ):
                            raise VideoInputFailure("input_corrupt")
                    except VideoInputFailure as exc:
                        await db.rollback()
                        if _transient_video_input_error(exc.code):
                            raise _defer_frame_segment(
                                frame_ordinals[index:],
                                write_scope,
                                pending_write,
                                pending_evidence,
                                None,
                                stats,
                                gpu_arbiter_failures,
                                segment_id,
                            ) from exc
                        await _fail_frame_job_in_session(
                            db, job_id, project_id, f"accepted_video_input_{exc.code}"
                        )
                        return stats
                    for attempt in range(_WRITE_ATTEMPTS):
                        try:
                            await pred_svc.apply_prediction_result(
                                ApplyPredictionResultRequest(
                                    **pending_write,
                                    write_scope=write_scope,
                                    accepted_video_input=pending_evidence,
                                )
                            )
                            await db.commit()
                            break
                        except ApplyPredictionResultError as exc:
                            await db.rollback()
                            if exc.status_code in {401, 403}:
                                await _fail_frame_job_in_session(
                                    db,
                                    job_id,
                                    project_id,
                                    "preannotation_actor_revoked",
                                )
                                raise _AuthorityRevoked from exc
                            if not exc.retryable:
                                failed_evidence = video_evidence.failure_evidence(
                                    plan,
                                    pending_evidence.entry,
                                    frame_id=pending_evidence.frame_id,
                                    frame_sha256=pending_evidence.frame_sha256,
                                )
                                gpu_error = model_gpu_failure_record(exc)
                                failure_extra = {
                                    **video_evidence.prediction_extra(failed_evidence),
                                    "accepted_video_execution_job_id": str(
                                        active_job_uuid
                                    ),
                                    "execution_unit": "frame",
                                    "execution_scope": write_scope.execution_scope,
                                    "task": stage0.get("task"),
                                    **(
                                        {"request_context": retry_request_context}
                                        if retry_request_context is not None
                                        else {}
                                    ),
                                    **(
                                        {"gpu_arbiter_error": gpu_error}
                                        if gpu_error is not None
                                        else {}
                                    ),
                                }
                                pending_failure = {
                                    "evidence": failed_evidence.model_dump(
                                        mode="json", by_alias=True
                                    ),
                                    "error_type": exc.code,
                                    "message": str(exc),
                                    "extra": failure_extra,
                                    "ml_backend_id": str(pending_write["ml_backend_id"])
                                    if pending_write.get("ml_backend_id")
                                    else None,
                                    "ml_backend_pool_id": str(
                                        pending_write["ml_backend_pool_id"]
                                    )
                                    if pending_write.get("ml_backend_pool_id")
                                    else None,
                                    "gpu_arbiter_error": gpu_error,
                                }
                                raise _defer_frame_segment(
                                    frame_ordinals[index:],
                                    write_scope,
                                    None,
                                    None,
                                    pending_failure,
                                    stats,
                                    gpu_arbiter_failures,
                                    segment_id,
                                ) from exc
                            if attempt == _WRITE_ATTEMPTS - 1:
                                raise _defer_frame_segment(
                                    frame_ordinals[index:],
                                    write_scope,
                                    pending_write,
                                    pending_evidence,
                                    None,
                                    stats,
                                    gpu_arbiter_failures,
                                    segment_id,
                                ) from exc
                            await asyncio.sleep(random.uniform(0.05, 0.1) * 2**attempt)
                    stats["frames_done"] += 1
                    stats["boxes"] += len(pending_write["result"])
                    pending_write = None
                    pending_evidence = None

    except _AuthorityRevoked:
        return stats
    except _FrameWriteDeferred:
        raise
    except Exception as exc:
        async with SessionLocal() as failure_db:
            await _fail_frame_job_in_session(
                failure_db,
                job_id,
                project_id,
                f"frame_worker_failed:{type(exc).__name__}",
            )
        raise
    finally:
        await engine.dispose()

    return await _finish_segment(
        stats,
        gpu_arbiter_failures,
        project_id=project_id,
        job_id=job_id,
        segment_id=segment_id,
    )


async def _finish_segment(
    stats: dict,
    gpu_arbiter_failures: list[dict],
    *,
    project_id: str,
    job_id: str,
    segment_id: str,
) -> dict:
    from yoao_inference.gpu.contracts import summarize_gpu_arbiter_failures

    if gpu_arbiter_failures:
        stats["gpu_arbiter_failures"] = summarize_gpu_arbiter_failures(
            gpu_arbiter_failures
        )

    canonical = await _write_job_progress(
        job_id,
        segment_id,
        project_id,
        segment_stats=stats,
    )
    return canonical if isinstance(canonical, dict) else stats


async def _write_job_progress(
    job_id: str,
    segment_id: str | None,
    project_id: str,
    *,
    segment_stats: dict | None,
) -> dict:
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    engine = create_async_engine(settings.database_url, echo=False)
    SessionLocal = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    try:
        async with SessionLocal() as db:
            job = await _lock_frame_job(db, job_id, project_id)
            canonical = (
                _bounded_segment_stats(segment_stats)
                if segment_stats is not None
                else {}
            )
            if job is None:
                return canonical
            if job.status not in {"pending", "running"}:
                return ((job.payload or {}).get(_RECEIPTS_FIELD) or {}).get(
                    segment_id, canonical
                )

            segment_ids, total = _frozen_frame_publication(job)
            if segment_id is not None and segment_id not in segment_ids:
                raise ValueError("segment does not belong to the frozen frame dispatch")
            receipts = _load_frame_receipts(job, segment_ids)
            if receipts is None:
                await _mark_unknown_frame_job(db, job)
                await db.commit()
                _publish_frame_progress(
                    project_id,
                    0,
                    total,
                    status="failed",
                    error=_LEGACY_RECEIPTS_UNAVAILABLE,
                )
                return canonical

            if segment_id is not None:
                canonical = receipts.setdefault(segment_id, canonical)
            done = sum(_processed_frames(stats) for stats in receipts.values())
            status = await _settle_frame_receipts(
                db, job, segment_ids, receipts, done, total
            )
            if status == "running":
                await db.flush()
                _cache_progress(job_id, done, total)
                if segment_id is not None:
                    _publish_frame_progress(project_id, done, total, status=status)
                await db.commit()
            else:
                await db.commit()
                _cache_progress(job_id, done, total)
                _publish_frame_progress(project_id, done, total, status=status)
            return canonical
    finally:
        await engine.dispose()


async def _job_cancelled(db, job_id: str) -> bool:
    """Stop queued segments when their job is missing, terminal or cancelling."""
    from app.db.models.async_job import AsyncJobStatus
    from app.services import async_job as async_job_svc

    job = await async_job_svc.get_job(db, uuid.UUID(job_id))
    if job is None:
        return True
    await db.refresh(job)
    if job.status not in {AsyncJobStatus.PENDING.value, AsyncJobStatus.RUNNING.value}:
        return True
    return bool((job.payload or {}).get("cancel_requested"))


async def _existing_video_frame_ordinals(
    db,
    *,
    task_id: uuid.UUID,
    execution_job_id: uuid.UUID,
    input_id: uuid.UUID,
    plan_digest: str,
) -> set[int]:
    """Find completed frames by actual Job and retained plan, including empty results."""
    from sqlalchemy import select

    from app.services.labeling_core_read_relations import (
        prediction_meta_row,
        prediction_row,
    )

    accepted_video = prediction_meta_row.extra["accepted_video_input"]
    ordinal = accepted_video["entry"]["ordinal"].as_string()
    values = (
        await db.execute(
            select(ordinal)
            .join(
                prediction_row,
                (prediction_row.id == prediction_meta_row.prediction_id)
                & (
                    prediction_row.created_at
                    == prediction_meta_row.prediction_created_at
                ),
            )
            .where(
                prediction_row.task_id == task_id,
                prediction_meta_row.extra["accepted_video_execution_job_id"].as_string()
                == str(execution_job_id),
                accepted_video["input_id"].as_string() == str(input_id),
                accepted_video["plan_digest"].as_string() == plan_digest,
            )
        )
    ).scalars()
    result = set()
    for value in values:
        try:
            result.add(int(value))
        except (TypeError, ValueError):
            continue
    return result


def _publish_frame_progress(
    project_id: str, current: int, total: int, *, status: str, error: str | None = None
) -> None:
    """逐帧 job 进度 → 项目 SSE 通道 (复用 prediction_progress.publish_progress 的 payload 形态)。"""
    from app.services.prediction_progress import publish_progress

    try:
        publish_progress(project_id, current, total, status=status, error=error)
    except RedisError as exc:
        log.warning("Frame progress publication failed project=%s: %s", project_id, exc)


@celery_app.task
def finalize_frame_job(
    segment_stats: list[dict], *, project_id: str, job_id: str
) -> None:
    asyncio.run(_finalize(project_id=project_id, job_id=job_id))


async def _lock_frame_job(db, job_id: str, project_id: str):
    job = await db.scalar(
        job_lock_statement(uuid.UUID(job_id), mode="update", populate_existing=True)
    )
    return job if job is not None and str(job.project_id) == project_id else None


def _frozen_frame_publication(job) -> tuple[list[str], int]:
    dispatch = (job.result or {}).get("frame_dispatch")
    if not isinstance(dispatch, dict):
        raise RuntimeError("frame job has no frozen segment publication")
    segment_ids = dispatch.get("segment_ids")
    total = dispatch.get("total_frames")
    if (
        not isinstance(segment_ids, list)
        or not segment_ids
        or any(not isinstance(value, str) for value in segment_ids)
        or len(set(segment_ids)) != len(segment_ids)
        or type(total) is not int
        or total <= 0
    ):
        raise RuntimeError("frame job has no frozen segment publication")
    return segment_ids, total


def _processed_frames(stats: dict) -> int:
    return stats["frames_done"] + stats["skipped"] + stats["failed"]


def _load_frame_receipts(job, segment_ids: list[str]) -> dict[str, dict] | None:
    stored = (job.payload or {}).get(_RECEIPTS_FIELD)
    if stored is not None:
        return dict(stored)

    job_id = str(job.id)
    with redis.from_url(settings.redis_url) as progress, progress.pipeline() as pipe:
        encoded, observed_done = (
            pipe.hmget(_segment_results_key(job_id), segment_ids)
            .get(_progress_key(job_id))
            .execute()
        )
    imported = {
        segment_id: _deserialize_segment_stats(value)
        for segment_id, value in zip(segment_ids, encoded, strict=True)
        if value is not None
    }
    if len(imported) == len(segment_ids):
        return imported
    processed = sum(_processed_frames(stats) for stats in imported.values())
    if observed_done is None or int(observed_done) != processed:
        return None
    return imported


async def _mark_unknown_frame_job(db, job) -> None:
    from app.services import async_job as async_job_svc
    from app.services.async_job_notify import notify_job_terminal

    log.warning("Frame job %s has expired legacy segment receipts", job.id)
    await async_job_svc.mark_failed(db, job.id, error=_LEGACY_RECEIPTS_UNAVAILABLE)
    await notify_job_terminal(db, job_id=job.id)


async def _finalize(*, project_id: str, job_id: str) -> None:
    await _write_job_progress(job_id, None, project_id, segment_stats=None)


async def _settle_frame_receipts(
    db, job, segment_ids: list[str], receipts: dict[str, dict], done: int, total: int
) -> str:
    from app.services import async_job as async_job_svc
    from yoao_inference.gpu.contracts import summarize_gpu_arbiter_failures

    if len(receipts) < len(segment_ids):
        pct = min(99, max(int(job.progress_pct or 0), done * 100 // total))
        await async_job_svc.update_progress(
            db, job.id, pct, extra_payload={_RECEIPTS_FIELD: receipts}
        )
        return "running"
    if receipts.keys() != set(segment_ids) or done != total:
        raise RuntimeError("frame segment receipts differ from the frozen publication")

    canonical = [receipts[segment_id] for segment_id in segment_ids]
    frames_done = sum(item["frames_done"] for item in canonical)
    failed = sum(item["failed"] for item in canonical)
    skipped = sum(item["skipped"] for item in canonical)
    merged = {
        **(job.result or {}),
        "frames_done": frames_done,
        "boxes": sum(item["boxes"] for item in canonical),
        "failed": failed,
        "frame_failed_count": failed,
        "failed_prediction_ids": [
            failure_id
            for item in canonical
            for failure_id in item.get("failed_prediction_ids", [])
        ],
        "skipped": skipped,
        "execution_unit": "frame",
    }
    gpu_arbiter_failures = summarize_gpu_arbiter_failures(
        failure
        for item in canonical
        for failure in item.get("gpu_arbiter_failures", [])
    )
    if gpu_arbiter_failures:
        merged["gpu_arbiter_failures"] = gpu_arbiter_failures
    await async_job_svc.update_progress(
        db, job.id, 99, extra_payload={_RECEIPTS_FIELD: receipts}
    )
    if (job.payload or {}).get("cancel_requested"):
        status = "cancelled"
        await async_job_svc.mark_cancelled(db, job.id, result=merged)
    elif failed > 0 and frames_done == 0 and skipped == 0:
        status = "failed"
        await async_job_svc.mark_failed(
            db, job.id, error="all_video_frame_predictions_failed", result=merged
        )
    else:
        status = "completed"
        await async_job_svc.mark_complete(db, job.id, result=merged)
    if status in {"cancelled", "failed"}:
        from app.services.async_job_notify import notify_job_terminal

        await notify_job_terminal(db, job_id=job.id)
    return status
