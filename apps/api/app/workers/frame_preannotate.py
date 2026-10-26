"""Retained-video frame detection fan-out for ``execution_unit=frame``."""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import uuid
from dataclasses import asdict
from typing import cast

import redis
from fastapi import HTTPException
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


class _FrameWriteDeferred(Exception):
    def __init__(self, resume: dict):
        super().__init__("Frame prediction is waiting for a write lock")
        self.resume = resume


# ── 纯函数: 段规划 (可单测, 不碰 IO) ────────────────────────────────────────


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
    from aap_assets_video_inputs import values as video_values

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


# ── Redis 进度 (分布式段任务共享计数) ────────────────────────────────────────


def _progress_key(job_id: str) -> str:
    return f"frame_preannotate:{job_id}:done"


def _progress_total_key(job_id: str) -> str:
    return f"frame_preannotate:{job_id}:total"


def _segment_results_key(job_id: str) -> str:
    return f"frame_preannotate:{job_id}:segments"


_INIT_PROGRESS_LUA = """
local expected = tonumber(ARGV[1])
local previous = redis.call('GET', KEYS[1])
if previous and tonumber(previous) ~= expected then
  return 0
end
redis.call('SET', KEYS[1], expected, 'EX', 86400, 'NX')
if redis.call('EXISTS', KEYS[2]) == 0 then
  local completed = 0
  for _, value in ipairs(redis.call('HVALS', KEYS[3])) do
    local stats = cjson.decode(value).stats
    completed = completed + (tonumber(stats.frames_done) or 0)
      + (tonumber(stats.skipped) or 0) + (tonumber(stats.failed) or 0)
  end
  redis.call('SET', KEYS[2], completed, 'EX', 86400, 'NX')
end
redis.call('EXPIRE', KEYS[1], 86400)
redis.call('EXPIRE', KEYS[2], 86400)
redis.call('EXPIRE', KEYS[3], 86400)
return 1
"""


def _init_progress(job_id: str, total: int) -> None:
    r = redis.from_url(settings.redis_url)
    try:
        initialized = r.eval(
            _INIT_PROGRESS_LUA,
            3,
            _progress_total_key(job_id),
            _progress_key(job_id),
            _segment_results_key(job_id),
            total,
        )
        if initialized != 1:
            raise RuntimeError("frame progress differs from its frozen dispatch")
    finally:
        r.close()


def _bump_progress(job_id: str, done_delta: int) -> tuple[int, int]:
    """已完成帧 +delta, 返回 (done, total)。"""
    r = redis.from_url(settings.redis_url)
    try:
        done = int(cast(int, r.incrby(_progress_key(job_id), max(0, done_delta))) or 0)
        total = int(cast(bytes | None, r.get(_progress_total_key(job_id))) or 0)
        return done, total
    finally:
        r.close()


_SEGMENT_PROGRESS_LUA = """
local segments = KEYS[1]
local done = KEYS[2]
local total = KEYS[3]
local segment_id = ARGV[1]
local delta = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])
local result = ARGV[4]
local inserted = redis.call('HSETNX', segments, segment_id, result)
if inserted == 1 then
  redis.call('INCRBY', done, delta)
else
  result = redis.call('HGET', segments, segment_id)
end
redis.call('EXPIRE', segments, ttl)
redis.call('EXPIRE', done, ttl)
redis.call('EXPIRE', total, ttl)
return {tonumber(redis.call('GET', done) or '0'), tonumber(redis.call('GET', total) or '0'), result}
"""

_SEGMENT_RESULT_MAX_FAILURE_IDS = 100
_SEGMENT_RESULT_MAX_GPU_FAILURES = 32


def _bounded_segment_stats(stats: dict) -> dict:
    """Keep chord receipts small and limited to aggregate frame outcomes."""
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


def _serialize_segment_stats(stats: dict) -> str:
    return json.dumps(
        {"version": 1, "stats": _bounded_segment_stats(stats)},
        separators=(",", ":"),
        sort_keys=True,
    )


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


def _bump_segment_progress(
    job_id: str, segment_id: str, done_delta: int, stats: dict
) -> tuple[int, int, dict]:
    """Cache one logical chord result and count its progress atomically."""
    r = redis.from_url(settings.redis_url)
    try:
        done, total, result = r.eval(
            _SEGMENT_PROGRESS_LUA,
            3,
            _segment_results_key(job_id),
            _progress_key(job_id),
            _progress_total_key(job_id),
            segment_id,
            max(0, done_delta),
            86400,
            _serialize_segment_stats(stats),
        )
        return (
            int(done or 0),
            int(total or 0),
            _deserialize_segment_stats(result),
        )
    finally:
        r.close()


# ── 编排入口: 建段 + 派 chord (由应用层 batch command 注入调用) ────────────────


class JobsFramePreannotationDispatcher:
    async def prepare(
        self, request: FramePreannotationDispatchRequest, *, db
    ) -> PreparedFramePreannotationDispatch:
        """Freeze the canvas with the parent's running/preparation transaction."""
        from aap_jobs.queue_contract import encode_canvas_request

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
        _init_progress(str(lease.job_id), prepared.total_frames)
        await publish_prepared_dispatch(
            db, lease, publish=lambda: publish_queue_envelope(lease.envelope)
        )
        return FramePreannotationDispatchReceipt(
            dispatched=True,
            segments=len(prepared.segment_ids),
            total_frames=prepared.total_frames,
        )


# ── 阶段 A: 每视频抽全帧一次 (走 media 队列, 跨视频并行) ──────────────────────────


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


# ── 阶段 A→B 衔接: 抽帧 chord 回调派预测 chord ───────────────────────────────────


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


# ── 段任务: 逐帧 predict + 落库 ─────────────────────────────────────────────


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
        # RETRY does not refresh Redis chord keys; retain completed sibling results.
        backend = self.app.backend
        if self.request.group and backend.expires:
            for suffix in (".j", ".s", ".t"):
                backend.expire(
                    backend.get_key_for_group(self.request.group, suffix),
                    backend.expires,
                )
        with redis.from_url(settings.redis_url) as progress:
            progress.expire(_progress_key(job_id), 86400)
            progress.expire(_progress_total_key(job_id), 86400)
            progress.expire(_segment_results_key(job_id), 86400)
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
    from aap_assets_video_inputs import values as video_values

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
    if job is not None and job.status in {"pending", "running"}:
        await async_job_svc.mark_failed(db, job.id, error=error)
        await notify_job_terminal(db, job_id=job.id)
        await db.commit()
    done, total = _bump_progress(job_id, 0)
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

    from app.db.models.ml_backend_registry import MLBackendRegistry as MLBackend
    from app.services.labeling_core_read_relations import get_core_row, task_row
    from app.services import async_job as async_job_svc
    from app.services.gpu_arbitration.dispatch import (
        build_gpu_dispatch_context_factory,
    )
    from app.services.gpu_arbitration.contracts import (
        gpu_arbiter_failure_record,
    )
    from app.services.ml_routing.client import RoutedMLBackendClient
    from app.services.prediction import PredictionService, to_video_bbox_result
    from app.services.prediction_context import build_predict_context
    from app.services import prediction_retry
    from app.services.labeling_write_scope import (
        LabelingWriteScope,
        capture_labeling_write_scope,
    )
    from app.services.labeling_write_guard import lock_labeling_write
    from aap_assets_video_inputs import evidence as video_evidence
    from aap_assets_video_inputs import execution as video_execution
    from aap_assets_video_inputs.execution import VideoInputFailure

    task_id = segment["task_id"]
    input_id = segment["input_id"]
    input_job_id = segment["input_job_id"]
    backend_uuid = uuid.UUID(ml_backend_id)
    plan_digest = segment["plan_digest"]
    frame_ordinals: list[int] = list(
        resume["frame_ordinals"] if resume is not None else segment["frame_ordinals"]
    )
    box_cap = settings.frame_preannotate_max_boxes_per_frame
    segment_id = resume.get("segment_id") if resume is not None else None
    segment_id = segment_id or _segment_id(segment)

    engine = create_async_engine(settings.database_url, echo=False)
    SessionLocal = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    dispatch_context_factory = build_gpu_dispatch_context_factory(SessionLocal)

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
            if await _job_cancelled(db, job_id):
                stats["skipped"] += len(frame_ordinals)
                if async_job is None or async_job.status not in {"pending", "running"}:
                    return await _finish_segment(
                        stats,
                        gpu_arbiter_failures,
                        project_id=project_id,
                        job_id=job_id,
                        segment_id=segment_id,
                    )
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

            if await _preannotation_actor_revoked(db, actor_uuid, project_uuid):
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
            # A broker redelivery after the original commit is a skip, even when
            # the result list is empty. The Labeling writer repeats this check
            # atomically under its Task lock to close the inference/write race.
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

                from app.services.ml_backend import MLBackendService
                from app.services.prediction import PredictionService
                from app.services.prediction import to_video_bbox_result
                from app.services.prediction_context import build_predict_context

                pred_svc: ApplyPredictionResultPort = PredictionService(db)
                backend = await db.get(MLBackend, backend_uuid)
                if backend is None:
                    await _fail_frame_job_in_session(
                        db, job_id, project_id, "ml_backend_unavailable"
                    )
                    return stats
                try:
                    source_pool_id = await MLBackendService(db).pool_id_for_registry(
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
                dispatch_context_factory = build_gpu_dispatch_context_factory(
                    SessionLocal
                )
                client = RoutedMLBackendClient(
                    db,
                    backend,
                    project_id=project_uuid,
                    owner=f"frame:{job_id}:{task_id}",
                    operation="frame_predict",
                    shadow_session_factory=SessionLocal,
                    dispatch_context_factory=dispatch_context_factory,
                )

                async def record_failure(
                    ordinal: int,
                    entry,
                    error: Exception,
                    *,
                    frame_record=None,
                    gpu_error=None,
                ) -> None:
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
                            error.code
                            if isinstance(error, VideoInputFailure)
                            else type(error).__name__
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
                    }
                    failure = {
                        "error_type": str(error_type),
                        "message": (
                            gpu_error["message"]
                            if gpu_error is not None
                            else str(error)
                        ),
                        "extra": extra,
                        "ml_backend_id": (
                            client.last_instance_id or backend_uuid
                            if client is not None
                            else backend_uuid
                        ),
                        "ml_backend_pool_id": (
                            client.pool_id or source_pool_id
                            if client is not None
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
                            gpu_error = gpu_arbiter_failure_record(exc)
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
                            from sqlalchemy import inspect

                            if inspect(backend).expired:
                                await db.refresh(backend)
                            results = await client.predict(
                                [
                                    {
                                        "id": f"{task_id}:{reference.entry.frame_index}",
                                        "file_path": reference.url,
                                    }
                                ],
                                context=context,
                            )
                            raw = results[0].result if results else []
                            pending_write = {
                                "task_id": write_scope.task_id,
                                "project_id": project_uuid,
                                "ml_backend_id": client.last_instance_id
                                or backend_uuid,
                                "result": to_video_bbox_result(
                                    raw if isinstance(raw, list) else [],
                                    reference.entry.frame_index,
                                )[:box_cap],
                                "score": results[0].score if results else None,
                                "model_version": results[0].model_version
                                if results
                                else None,
                                "inference_time_ms": (
                                    results[0].inference_time_ms if results else None
                                ),
                                "token_meta": results[0].meta if results else None,
                                "ml_backend_pool_id": client.pool_id or source_pool_id,
                            }
                            pending_evidence = ready
                        except Exception as exc:  # noqa: BLE001
                            await db.rollback()
                            gpu_error = gpu_arbiter_failure_record(exc)
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
                                gpu_error = gpu_arbiter_failure_record(exc)
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
    from app.services.gpu_arbitration.contracts import summarize_gpu_arbiter_failures

    if gpu_arbiter_failures:
        stats["gpu_arbiter_failures"] = summarize_gpu_arbiter_failures(
            gpu_arbiter_failures
        )

    # 进度推进 (已处理帧 = 成功 + 跳过 + 失败): 让分母对齐规划总帧数。
    processed = stats["frames_done"] + stats["skipped"] + stats["failed"]
    canonical = await _write_job_progress(
        job_id,
        processed,
        project_id,
        segment_id=segment_id,
        segment_stats=stats,
    )
    return canonical if isinstance(canonical, dict) else stats


async def _write_job_progress(
    job_id: str,
    done_delta: int,
    project_id: str,
    *,
    segment_id: str,
    segment_stats: dict,
) -> dict:
    """Publish completed segment progress while fencing terminal transitions."""
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    from app.services import async_job as async_job_svc

    engine = create_async_engine(settings.database_url, echo=False)
    SessionLocal = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    try:
        async with SessionLocal() as db:
            job = await db.scalar(
                job_lock_statement(
                    uuid.UUID(job_id), mode="update", populate_existing=True
                )
            )
            active = job is not None and job.status in {"pending", "running"}
            if active:
                _init_progress(job_id, int((job.result or {}).get("frame_total", 0)))
            done, total, canonical = _bump_segment_progress(
                job_id,
                segment_id,
                done_delta if active else 0,
                segment_stats,
            )
            if not active:
                return canonical
            if total > 0:
                pct = min(99, max(int(job.progress_pct or 0), int(done * 100 / total)))
                await async_job_svc.update_progress(db, job.id, pct)
                await db.flush()
            _publish_frame_progress(project_id, done, total, status="running")
            await db.commit()
            if total > 0 and done >= total:
                await _finalize([], project_id=project_id, job_id=job_id)
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

    publish_progress(project_id, current, total, status=status, error=error)


# ── chord 回调: job 收尾 ────────────────────────────────────────────────────


@celery_app.task
def finalize_frame_job(
    segment_stats: list[dict], *, project_id: str, job_id: str
) -> None:
    """段全绿后聚合各段 stats → async_job 收尾 + SSE completed。"""
    asyncio.run(_finalize(segment_stats or [], project_id=project_id, job_id=job_id))


def _canonical_frame_segment_stats(job) -> list[dict] | None:
    dispatch = (job.result or {}).get("frame_dispatch")
    segment_ids = dispatch.get("segment_ids") if isinstance(dispatch, dict) else None
    if (
        not isinstance(segment_ids, list)
        or not segment_ids
        or any(not isinstance(value, str) for value in segment_ids)
        or len(set(segment_ids)) != len(segment_ids)
    ):
        raise RuntimeError("frame job has no frozen segment publication")
    with redis.from_url(settings.redis_url) as progress:
        encoded = progress.hmget(_segment_results_key(str(job.id)), segment_ids)
    if any(value is None for value in encoded):
        return None
    stats = [_deserialize_segment_stats(value) for value in encoded]
    processed = sum(
        item["frames_done"] + item["skipped"] + item["failed"] for item in stats
    )
    if processed != dispatch["total_frames"]:
        raise RuntimeError("frame segment receipts differ from the frozen publication")
    return stats


async def _finalize(segment_stats: list[dict], *, project_id: str, job_id: str) -> None:
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    from app.services import async_job as async_job_svc
    from app.services.gpu_arbitration.contracts import summarize_gpu_arbiter_failures

    engine = create_async_engine(settings.database_url, echo=False)
    SessionLocal = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    try:
        async with SessionLocal() as db:
            job = await db.scalar(
                job_lock_statement(
                    uuid.UUID(job_id), mode="update", populate_existing=True
                )
            )
            if job is None or job.status not in {"pending", "running"}:
                return
            # A recovered canvas may deliver duplicate header results. Only the
            # complete frozen logical-segment receipts can authorize completion.
            canonical = _canonical_frame_segment_stats(job)
            if canonical is None:
                return
            frames_done = sum(item["frames_done"] for item in canonical)
            boxes = sum(item["boxes"] for item in canonical)
            failed = sum(item["failed"] for item in canonical)
            skipped = sum(item["skipped"] for item in canonical)
            failed_prediction_ids = [
                failure_id
                for item in canonical
                for failure_id in item.get("failed_prediction_ids", [])
            ]
            gpu_arbiter_failures = summarize_gpu_arbiter_failures(
                failure
                for item in canonical
                for failure in item.get("gpu_arbiter_failures", [])
            )
            merged = dict(job.result or {})
            merged.update(
                {
                    "frames_done": frames_done,
                    "boxes": boxes,
                    "failed": failed,
                    "frame_failed_count": failed,
                    "failed_prediction_ids": failed_prediction_ids,
                    "skipped": skipped,
                    "execution_unit": "frame",
                }
            )
            if gpu_arbiter_failures:
                merged["gpu_arbiter_failures"] = gpu_arbiter_failures
            if (job.payload or {}).get("cancel_requested"):
                status, error = "cancelled", None
            elif failed > 0 and frames_done == 0 and skipped == 0:
                status, error = "failed", "all_video_frame_predictions_failed"
            else:
                status, error = "completed", None
            if status == "cancelled":
                await async_job_svc.mark_cancelled(db, job.id, result=merged)
            elif status == "failed":
                await async_job_svc.mark_failed(db, job.id, error=error, result=merged)
            else:
                await async_job_svc.mark_complete(db, job.id, result=merged)
            if status in {"cancelled", "failed"}:
                from app.services.async_job_notify import notify_job_terminal

                await notify_job_terminal(db, job_id=job.id)
            await db.commit()
    finally:
        await engine.dispose()

    done, total = _bump_progress(job_id, 0)
    if total <= 0:
        total = int((job.result or {}).get("frame_total", 0))
    _publish_frame_progress(
        project_id,
        min(done, total) if total else frames_done + skipped + failed,
        total,
        status=status,
    )
