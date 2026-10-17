"""v0.21.7 · 单帧分支批量逐帧预标注 fan-out 引擎。

``execution_unit=frame`` 的视频 task: 图像 backend (det) 在整段视频**逐帧**跑检测, 每帧落
``VideoBboxGeometry``。区别于整段 tracker (video_track_bbox, 跨帧关联) 与单题工作台单帧 AI。

**两阶段 Celery fan-out (v0.21.8)**::

    chord(
        group(extract_frames_task.s(task, 全帧) for task in videos),  # 阶段 A: 每视频抽一次
        launch_predict_phase.s(segments, ...),                         # 抽帧全绿 → 派阶段 B
    )
    # launch_predict_phase 内:
    chord(
        group(predict_video_segment.s(seg) for seg in segments),       # 阶段 B: 纯预测 (只读缓存)
        finalize_frame_job.s(job_id, ...),                             # 段全绿后聚合收尾
    )

- **阶段 A** ``extract_frames_task``: 每视频**一次**下载源视频 + 抽全部计划帧 → ``VideoFrameCache``
  (跨视频并行, 走 media 队列)。消除 v0.21.7「每段各下一次整段视频」的重复下载。
- **阶段 B 段任务** ``predict_video_segment``: **只读**缓存帧 URL (不再下载) → 逐帧 stage-0 predict →
  ``to_video_bbox_result`` → 落库; 段级幂等 (跳已落库帧, 断点续跑) + cancel 检查 + 每帧框数上限;
  Redis ``INCR`` 已完成帧计数 → SSE 进度 + ``async_job.progress_pct`` (跑中 DB 列表实时进度)。
- **finalize** ``finalize_frame_job``: 汇总各段 stats → async_job 收尾 + SSE completed。

**首刀 scope**: 源**单阶段**检测 (frame × pipeline-depth 多阶段是组合爆炸, 与 plan 的
``frame × track 二维展开`` 同属排除项, 不做)。抽帧全量 (采样步长可选降级), 每 task 帧数上限
``settings.frame_preannotate_max_frames`` 截断。
"""

from __future__ import annotations

import asyncio
import random
import uuid
from dataclasses import asdict
from typing import cast

import redis
from fastapi import HTTPException

from app.config import settings
from app.services.labeling_prediction_contract import (
    ApplyPredictionResultError,
    ApplyPredictionResultPort,
    ApplyPredictionResultRequest,
)
from app.workers.celery_app import celery_app

# 逐帧抽帧的目标宽度 / 格式 (ML backend 拉取用)。jpeg 通用、体积小; 宽度够检测即可。
_FRAME_WIDTH = 1280
_FRAME_FORMAT = "jpeg"
_WRITE_ATTEMPTS = 5


class _FrameWriteDeferred(Exception):
    def __init__(self, resume: dict):
        super().__init__("Frame prediction is waiting for a write lock")
        self.resume = resume


# ── 纯函数: 段规划 (可单测, 不碰 IO) ────────────────────────────────────────


def plan_frame_indices(frame_count: int, max_frames: int, step: int = 1) -> list[int]:
    """一个视频 task 要逐帧跑的帧号列表。

    - 全量: ``step=1`` → [0, 1, .., min(frame_count, max_frames)-1]。
    - 采样降级: ``step>1`` → [0, step, 2*step, ...], 仍受 ``max_frames`` 截断 (截的是**已选帧数**)。
    """
    if frame_count <= 0:
        return []
    step = max(1, int(step))
    indices = list(range(0, int(frame_count), step))
    return indices[: max(0, int(max_frames))]


def chunk_frames(frame_indices: list[int], chunk_size: int) -> list[list[int]]:
    """把一个 task 的帧号列表切成若干段 (每段一个 Celery 子任务)。同 task 内切, 不跨 task
    (每段一次视频下载)。"""
    size = max(1, int(chunk_size))
    return [frame_indices[i : i + size] for i in range(0, len(frame_indices), size)]


def plan_segments(
    task_frame_counts: list[tuple[str, int]],
    *,
    max_frames: int,
    chunk_size: int,
    step: int = 1,
) -> tuple[list[dict], int]:
    """给一批 (task_id, frame_count) 规划所有段 + 统计总帧数。

    返回 ``(segments, total_frames)``:
    - ``segments``: ``[{"task_id", "frame_indices"}]`` —— 每段属单一 task。
    - ``total_frames``: 所有段帧数之和 (进度分母)。
    """
    segments: list[dict] = []
    total = 0
    for task_id, frame_count in task_frame_counts:
        indices = plan_frame_indices(frame_count, max_frames, step)
        total += len(indices)
        for chunk in chunk_frames(indices, chunk_size):
            segments.append({"task_id": task_id, "frame_indices": chunk})
    return segments, total


# ── Redis 进度 (分布式段任务共享计数) ────────────────────────────────────────


def _progress_key(job_id: str) -> str:
    return f"frame_preannotate:{job_id}:done"


def _progress_total_key(job_id: str) -> str:
    return f"frame_preannotate:{job_id}:total"


def _init_progress(job_id: str, total: int) -> None:
    r = redis.from_url(settings.redis_url)
    try:
        r.set(_progress_total_key(job_id), total, ex=86400)
        r.set(_progress_key(job_id), 0, ex=86400)
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


# ── 编排入口: 建段 + 派 chord (由 _run_batch 的 frame 分支调用) ────────────────


async def dispatch_frame_preannotate(
    db,
    *,
    project_id: str,
    ml_backend_id: str,
    tasks: list,
    stage0: dict,
    job_id: str,
    celery_root_task_id: str | None,
) -> dict:
    """逐帧执行单位编排: 为每个视频 task 规划段 → 建 job 级 chord 派发。

    返回 ``{"dispatched": bool, "segments": int, "total_frames": int}``。
    ``total_frames==0`` (无可跑帧) 时不派 chord, 由调用方按无操作收尾。
    """
    from celery import chord, group

    from app.services.video_frame_service import build_context_from_task

    max_frames = settings.frame_preannotate_max_frames
    chunk_size = settings.frame_preannotate_chunk_size

    task_frame_counts: list[tuple[str, int]] = []
    for task in tasks:
        try:
            ctx = await build_context_from_task(db, task)
        except Exception:
            continue  # 非视频 / 元数据缺失 → 跳过 (该 task 不逐帧)
        frame_count = int(ctx.metadata.frame_count or 0)
        if frame_count > 0:
            task_frame_counts.append((str(task.id), frame_count))

    segments, total_frames = plan_segments(
        task_frame_counts, max_frames=max_frames, chunk_size=chunk_size
    )
    if total_frames <= 0 or not segments:
        return {"dispatched": False, "segments": 0, "total_frames": 0}

    # 每视频全帧计划 (阶段 A 抽帧用; 与 plan_segments 同口径, 全量 step=1)。
    video_plans: list[tuple[str, list[int]]] = []
    for task_id, frame_count in task_frame_counts:
        idx = plan_frame_indices(frame_count, max_frames)
        if idx:
            video_plans.append((task_id, idx))

    _init_progress(job_id, total_frames)

    # 两阶段 fan-out (v0.21.8): 阶段 A 每视频抽帧一次 (跨视频并行, 走 media 队列), 全绿后回调
    # 派阶段 B 预测 chord。消除 v0.21.7「每段各下一次整段视频」(N 段 = N 次下载) 的重复下载——
    # 段任务 (阶段 B) 此时只读 VideoFrameCache, 零下载。
    extract_sigs = [
        extract_frames_task.s(
            task_id,
            frame_indices,
            job_id=job_id,
        )
        for task_id, frame_indices in video_plans
    ]
    chord(group(extract_sigs))(
        launch_predict_phase.s(
            project_id=project_id,
            ml_backend_id=ml_backend_id,
            stage0=stage0,
            job_id=job_id,
            segments=segments,
            celery_root_task_id=celery_root_task_id,
        )
    )
    return {
        "dispatched": True,
        "segments": len(segments),
        "total_frames": total_frames,
    }


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
    """阶段 A: 一个视频抽全部计划帧 → VideoFrameCache (下载源视频一次)。

    v0.21.8: 从段任务里拆出、每视频一次, 消除段级重复下载。阶段 B 段任务只读缓存。
    返回 ``{task_id, requested, extracted}``。
    """
    return asyncio.run(_run_extract(task_id, list(frame_indices), job_id=job_id))


async def _run_extract(
    task_id: str, frame_indices: list[int], *, job_id: str | None
) -> dict:
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    from app.db.models.task import Task
    from app.services.video_frame_service import build_context_from_task

    stats = {"task_id": task_id, "requested": len(frame_indices), "extracted": 0}
    engine = create_async_engine(settings.database_url, echo=False)
    SessionLocal = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    try:
        async with SessionLocal() as db:
            task = await db.get(Task, uuid.UUID(task_id))
            if task is None:
                return stats
            # job 已取消 → 不抽 (阶段 B 段任务也会各自 no-op)。
            if job_id and await _job_cancelled(db, job_id):
                return stats
            ctx = await build_context_from_task(db, task)
            urls = await _extract_frames_for_video(db, ctx, frame_indices)
            stats["extracted"] = len(urls)
    finally:
        await engine.dispose()
    return stats


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
    """阶段 A (抽帧) chord 回调: 全绿后派阶段 B 预测 chord。

    段任务此时只读 VideoFrameCache (帧已抽好), 不再下载。``extract_results`` 仅作 chord 同步屏障,
    个别视频抽帧失败不阻断——其段任务读缓存未命中会各自记 failed。
    """
    from celery import chord, group

    seg_sigs = [
        predict_video_segment.s(
            seg,
            project_id=project_id,
            ml_backend_id=ml_backend_id,
            stage0=stage0,
            job_id=job_id,
            celery_root_task_id=celery_root_task_id,
        )
        for seg in segments
    ]
    if not seg_sigs:
        return
    chord(group(seg_sigs))(finalize_frame_job.s(project_id=project_id, job_id=job_id))


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
    job_id: str,
    celery_root_task_id: str | None = None,
    resume: dict | None = None,
) -> dict:
    """处理一个段 (单 task 的一段帧号): 抽帧 → 逐帧 stage-0 predict → video_bbox 落库。

    返回段 stats ``{task_id, frames_done, boxes, failed, skipped}`` 供 chord 回调聚合。
    """
    try:
        return asyncio.run(
            _run_segment(
                segment=segment,
                project_id=project_id,
                ml_backend_id=ml_backend_id,
                stage0=stage0,
                job_id=job_id,
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
        raise self.retry(
            args=(segment,),
            kwargs={
                "project_id": project_id,
                "ml_backend_id": ml_backend_id,
                "stage0": stage0,
                "job_id": job_id,
                "celery_root_task_id": celery_root_task_id,
                "resume": exc.resume,
            },
            countdown=random.uniform(1, min(30, 2 ** min(self.request.retries + 1, 5))),
        ) from exc


async def _run_segment(
    *,
    segment: dict,
    project_id: str,
    ml_backend_id: str,
    stage0: dict,
    job_id: str,
    celery_root_task_id: str | None,
    resume: dict | None = None,
) -> dict:
    from sqlalchemy import inspect
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    from app.db.models.ml_backend_registry import MLBackendRegistry as MLBackend
    from app.db.models.task import Task
    from app.db.models.async_job import AsyncJob
    from app.services.gpu_arbitration.dispatch import (
        build_gpu_dispatch_context_factory,
    )
    from app.services.gpu_arbitration.contracts import (
        gpu_arbiter_failure_record,
    )
    from app.services.ml_routing.client import RoutedMLBackendClient
    from app.services.prediction import PredictionService, to_video_bbox_result
    from app.services.video_frame_service import build_context_from_task
    from app.services.prediction_context import build_predict_context
    from app.services.labeling_write_scope import (
        LabelingWriteScope,
        capture_labeling_write_scope,
    )

    task_id = segment["task_id"]
    frame_indices: list[int] = list(
        resume["frame_indices"] if resume is not None else segment["frame_indices"]
    )
    box_cap = settings.frame_preannotate_max_boxes_per_frame

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
    }
    gpu_arbiter_failures: list[dict] = []
    pending_write = None
    if resume is not None:
        stats.update(resume["stats"])
        gpu_arbiter_failures.extend(resume["gpu_arbiter_failures"])
        pending_write = resume["pending_write"]
    try:
        async with SessionLocal() as db:
            async_job = await db.get(AsyncJob, uuid.UUID(job_id))
            # cancel: 段边界粒度 (段小 ~chunk_size 帧, 已排队的段各自入口 no-op)。job 被取消
            # → 整段跳过, 不再抽帧/predict。
            if await _job_cancelled(db, job_id):
                stats["skipped"] += len(frame_indices)
                if async_job is None or async_job.status not in {"pending", "running"}:
                    return stats
                return await _finish_segment(
                    stats, gpu_arbiter_failures, project_id=project_id, job_id=job_id
                )

            if resume is not None:
                write_scope = LabelingWriteScope(**resume["write_scope"])
            else:
                task = await db.get(Task, uuid.UUID(task_id))
                if task is None:
                    stats["failed"] += len(frame_indices)
                    return await _finish_segment(
                        stats,
                        gpu_arbiter_failures,
                        project_id=project_id,
                        job_id=job_id,
                    )
                payload = dict(async_job.payload or {})
                execution_scope = payload.get("execution_scope")
                execution_scope_known = isinstance(execution_scope, str)
                write_scope = capture_labeling_write_scope(
                    task,
                    actor_id=async_job.user_id,
                    actor_known=True,
                    operation="preannotation",
                    execution_scope=execution_scope if execution_scope_known else None,
                    execution_scope_known=execution_scope_known,
                    job_id=uuid.UUID(job_id),
                    job_id_known=True,
                )
            pred_svc: ApplyPredictionResultPort = PredictionService(db)
            client = None

            # 段级幂等 (断点续跑): 该 task 已落库的 video_bbox 帧号, 跳过。
            done_frames = await _existing_frame_indices(db, write_scope.task_id)

            for index, fi in enumerate(frame_indices):
                if fi in done_frames:
                    stats["skipped"] += 1
                    pending_write = None
                    continue
                try:
                    if pending_write is None:
                        if client is None:
                            from app.services.ml_backend import MLBackendService

                            task = await db.get(Task, uuid.UUID(task_id))
                            backend = await db.get(MLBackend, uuid.UUID(ml_backend_id))
                            if task is None or backend is None:
                                raise HTTPException(
                                    status_code=404, detail="Task or backend not found"
                                )
                            try:
                                source_pool_id = await MLBackendService(
                                    db
                                ).pool_id_for_registry(backend.id)
                            except Exception:
                                source_pool_id = None
                            ctx = await build_context_from_task(db, task)
                            frame_urls = await _read_frame_urls(db, ctx, frame_indices)
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
                            client = RoutedMLBackendClient(
                                db,
                                backend,
                                project_id=uuid.UUID(project_id),
                                owner=f"frame:{job_id}:{task_id}",
                                operation="frame_predict",
                                shadow_session_factory=SessionLocal,
                                dispatch_context_factory=dispatch_context_factory,
                            )
                        url = frame_urls.get(fi)
                        if not url:
                            stats["failed"] += 1
                            continue
                        if inspect(backend).expired:
                            await db.refresh(backend)
                        results = await client.predict(
                            [{"id": f"{task_id}:{fi}", "file_path": url}],
                            context=context,
                        )
                        raw = results[0].result if results else []
                        pending_write = {
                            "task_id": write_scope.task_id,
                            "project_id": uuid.UUID(project_id),
                            "ml_backend_id": client.last_instance_id or backend.id,
                            "result": to_video_bbox_result(
                                raw if isinstance(raw, list) else [], fi
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
                    for attempt in range(_WRITE_ATTEMPTS):
                        try:
                            await pred_svc.apply_prediction_result(
                                ApplyPredictionResultRequest(
                                    **pending_write, write_scope=write_scope
                                )
                            )
                            await db.commit()
                            break
                        except ApplyPredictionResultError as exc:
                            if not exc.retryable:
                                raise
                            await db.rollback()
                            if attempt == _WRITE_ATTEMPTS - 1:
                                raise _FrameWriteDeferred(
                                    {
                                        "frame_indices": frame_indices[index:],
                                        "write_scope": asdict(write_scope),
                                        "pending_write": pending_write,
                                        "stats": stats,
                                        "gpu_arbiter_failures": gpu_arbiter_failures,
                                    }
                                ) from exc
                            await asyncio.sleep(random.uniform(0.05, 0.1) * 2**attempt)
                    stats["frames_done"] += 1
                    stats["boxes"] += len(pending_write["result"])
                except _FrameWriteDeferred:
                    raise
                except Exception as exc:
                    await db.rollback()
                    stats["failed"] += 1
                    failure = gpu_arbiter_failure_record(exc)
                    if failure is not None:
                        gpu_arbiter_failures.append(failure)
                pending_write = None
    finally:
        await engine.dispose()

    return await _finish_segment(
        stats, gpu_arbiter_failures, project_id=project_id, job_id=job_id
    )


async def _finish_segment(
    stats: dict, gpu_arbiter_failures: list[dict], *, project_id: str, job_id: str
) -> dict:
    from app.services.gpu_arbitration.contracts import summarize_gpu_arbiter_failures

    if gpu_arbiter_failures:
        stats["gpu_arbiter_failures"] = summarize_gpu_arbiter_failures(
            gpu_arbiter_failures
        )

    # 进度推进 (已处理帧 = 成功 + 跳过 + 失败): 让分母对齐规划总帧数。
    processed = stats["frames_done"] + stats["skipped"] + stats["failed"]
    await _write_job_progress(job_id, processed, project_id)
    return stats


async def _write_job_progress(job_id: str, done_delta: int, project_id: str) -> None:
    """Publish completed segment progress while fencing terminal transitions."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    from app.db.models.async_job import AsyncJob
    from app.services import async_job as async_job_svc

    engine = create_async_engine(settings.database_url, echo=False)
    SessionLocal = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    try:
        async with SessionLocal() as db:
            job = await db.scalar(
                select(AsyncJob)
                .where(AsyncJob.id == uuid.UUID(job_id))
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if job is None or job.status not in {"pending", "running"}:
                return
            done, total = _bump_progress(job_id, done_delta)
            if total > 0:
                pct = min(99, max(int(job.progress_pct or 0), int(done * 100 / total)))
                await async_job_svc.update_progress(db, job.id, pct)
                await db.flush()
            _publish_frame_progress(project_id, done, total, status="running")
            await db.commit()
    finally:
        await engine.dispose()


async def _job_cancelled(db, job_id: str) -> bool:
    """Stop queued segments when their job is missing, terminal or cancelling."""
    from app.db.models.async_job import AsyncJob, AsyncJobStatus

    job = await db.get(AsyncJob, uuid.UUID(job_id))
    if job is None:
        return True
    await db.refresh(job)
    if job.status not in {AsyncJobStatus.PENDING.value, AsyncJobStatus.RUNNING.value}:
        return True
    return bool((job.payload or {}).get("cancel_requested"))


async def _existing_frame_indices(db, task_uuid: uuid.UUID) -> set[int]:
    """该 task 已落库的 video_bbox 帧号集合 (段级幂等 / 断点续跑用)。

    prediction.result 里每条 item 的 ``geometry.frame_index``; 只认 video_bbox。
    """
    from sqlalchemy import select

    from app.db.models.prediction import Prediction

    rows = await db.execute(
        select(Prediction.result).where(Prediction.task_id == task_uuid)
    )
    done: set[int] = set()
    for (result,) in rows.all():
        if not isinstance(result, list):
            continue
        for item in result:
            geom = item.get("geometry") if isinstance(item, dict) else None
            if isinstance(geom, dict) and geom.get("type") == "video_bbox":
                fi = geom.get("frame_index")
                if isinstance(fi, int):
                    done.add(fi)
    return done


async def _extract_frames_for_video(
    db, ctx, frame_indices: list[int]
) -> dict[int, str]:
    """阶段 A: 抽这些帧 → VideoFrameCache (下载源视频一次), 返回 {frame_index: url}。

    先 ``_ensure_frame_row`` 建行, 再 ``_extract_video_frames`` (下载一次 + 逐帧落存储),
    最后回读 URL。已 ready 帧命中缓存不重抽 (断点续跑一次下载即可)。
    """
    from app.services.video_frame_service import _ensure_frame_row
    from app.workers.media import _extract_video_frames

    for fi in frame_indices:
        await _ensure_frame_row(db, ctx, fi, _FRAME_WIDTH, _FRAME_FORMAT)
    await db.commit()

    requests = [
        {"frame_index": fi, "width": _FRAME_WIDTH, "format": _FRAME_FORMAT}
        for fi in frame_indices
    ]
    await _extract_video_frames(str(ctx.item.id), requests)
    return await _read_frame_urls(db, ctx, frame_indices)


async def _read_frame_urls(db, ctx, frame_indices: list[int]) -> dict[int, str]:
    """只读 VideoFrameCache 取已抽好帧的 URL (阶段 B 段任务用, 不下载/不抽)。

    帧未 ready (阶段 A 漏抽/失败) → 不入返回 map, 调用方记 failed, **不回退下载**。
    """
    from sqlalchemy import select

    from app.db.models.dataset import VideoFrameCache
    from app.services.video_frame_service import _asset_url

    urls: dict[int, str] = {}
    rows = await db.execute(
        select(VideoFrameCache).where(
            VideoFrameCache.dataset_item_id == ctx.item.id,
            VideoFrameCache.frame_index.in_(frame_indices),
            VideoFrameCache.width == _FRAME_WIDTH,
            VideoFrameCache.format == _FRAME_FORMAT,
        )
    )
    for row in rows.scalars().all():
        if row.status == "ready" and row.storage_key:
            urls[row.frame_index] = _asset_url(row.storage_key)
    return urls


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


async def _finalize(segment_stats: list[dict], *, project_id: str, job_id: str) -> None:
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    from app.db.models.async_job import AsyncJob
    from app.services import async_job as async_job_svc
    from app.services.gpu_arbitration.contracts import summarize_gpu_arbiter_failures

    frames_done = sum(int(s.get("frames_done", 0)) for s in segment_stats if s)
    boxes = sum(int(s.get("boxes", 0)) for s in segment_stats if s)
    failed = sum(int(s.get("failed", 0)) for s in segment_stats if s)
    skipped = sum(int(s.get("skipped", 0)) for s in segment_stats if s)
    gpu_arbiter_failures = summarize_gpu_arbiter_failures(
        failure
        for stats in segment_stats
        if stats
        for failure in stats.get("gpu_arbiter_failures", [])
    )

    engine = create_async_engine(settings.database_url, echo=False)
    SessionLocal = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    try:
        async with SessionLocal() as db:
            job = await db.scalar(
                select(AsyncJob)
                .where(AsyncJob.id == uuid.UUID(job_id))
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if job is None or job.status not in {"pending", "running"}:
                return
            merged = dict(job.result or {})
            merged.update(
                {
                    "frames_done": frames_done,
                    "boxes": boxes,
                    "failed": failed,
                    "skipped": skipped,
                    "execution_unit": "frame",
                }
            )
            if gpu_arbiter_failures:
                merged["gpu_arbiter_failures"] = gpu_arbiter_failures
            status = (
                "cancelled"
                if (job.payload or {}).get("cancel_requested")
                else "completed"
            )
            if status == "cancelled":
                await async_job_svc.mark_cancelled(db, job.id, result=merged)
            else:
                await async_job_svc.mark_complete(db, job.id, result=merged)
            await db.commit()
    finally:
        await engine.dispose()

    _, total = _bump_progress(job_id, 0)
    _publish_frame_progress(
        project_id, total or (frames_done + skipped + failed), total, status=status
    )
