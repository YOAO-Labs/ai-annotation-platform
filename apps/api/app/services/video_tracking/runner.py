from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime, timezone

import redis.asyncio as aioredis
from aap_protocol_v2 import CorrectionFramePrompt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models.annotation import Annotation
from app.db.models.dataset import DatasetItem, VideoSegment
from app.db.models.mask_qc import MaskQCIssue
from app.db.models.mask_review_scope import MaskReviewScope
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.video_tracker_job import (
    VideoTrackerJob,
    VideoTrackerJobKind,
    VideoTrackerJobStatus,
)
from app.services.annotation_evidence import record_annotation_actors_for_tasks
from app.services.annotation_propagation import _new_track_id
from app.services.gpu_arbitration.contracts import (
    GPUDispatchContextFactory,
    GPUShadowSessionFactory,
    gpu_arbiter_failure_record,
)
from app.services.ml_backend import MLBackendService
from app.services.ml_routing.contracts import (
    RejectionReason,
    RouteOutcome,
    RouterMode,
    RoutingError,
)
from app.services.raster_mask_storage import (
    load_coco_rle,
    store_coco_rle,
    validate_mask_geometry_for_task,
    lock_raster_mask_references,
)
from app.services.mask_qc.topology import rle_and, rle_replace_region, rle_xor
from app.services.storage import resolve_task_url
from app.services.video_frame_service import derive_step
from app.services.video_collaboration import segment_work_bounds_for_task
from app.services.video_tracking.adapters import (
    TrackerContext,
    TrackerFrameResult,
    TrackerSession,
    get_tracker_adapter,
)
from app.services.video_tracks import is_polygon_track
from app.services.video_tracks import (
    is_mask_track,
    remove_frame_from_outside_ranges,
    resolve_mask_track_state_at_frame,
    resolve_track_at_frame,
)
from app.utils.raster_mask_rle import coco_rle_area, coco_rle_bbox_norm

log = logging.getLogger(__name__)

TrackerEventPublisher = Callable[[str, dict], Awaitable[None]]
MAX_TRACKER_STAGED_BYTES = 64 * 1024 * 1024

# v0.22.2 · B-combo · 发现趟窗口帧数。multiplex 需多帧传播才在种子帧填充 obj_id (单帧窗
# 不出检测), 故发现趟跑这么多帧但只取种子帧的框铸种; 取小值以压低 multiplex 显存与耗时。
COMBO_DISCOVERY_WINDOW_FRAMES = 5


class TrackerJobStateConflict(ValueError):
    """Requested review action is incompatible with the current job state."""

    def __init__(
        self,
        message: str,
        *,
        reason: str = "tracker_job_conflict",
        conflicts: list[dict] | None = None,
    ) -> None:
        super().__init__(message)
        self.detail = {"reason": reason, "message": message}
        if conflicts:
            self.detail["conflicts"] = conflicts


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def publish_tracker_event(channel: str, payload: dict) -> None:
    redis = aioredis.from_url(settings.redis_url)
    try:
        await redis.publish(channel, json.dumps(payload))
    except Exception as exc:
        log.warning(
            "video tracker event publish failed channel=%s err=%s", channel, exc
        )
    finally:
        try:
            await redis.close()
        except Exception:
            pass


def _event(job: VideoTrackerJob, type_: str, **extra: object) -> dict:
    return {
        "type": type_,
        "job_id": str(job.id),
        "task_id": str(job.task_id),
        "dataset_item_id": str(job.dataset_item_id),
        "annotation_id": str(job.annotation_id),
        "at": _now().isoformat(),
        **extra,
    }


def _normalize_bbox(geometry: dict) -> dict:
    return {
        "x": float(geometry.get("x", 0)),
        "y": float(geometry.get("y", 0)),
        "w": float(geometry.get("w", geometry.get("width", 0))),
        "h": float(geometry.get("h", geometry.get("height", 0))),
    }


def _normalize_points(geometry: dict) -> list[list[float]]:
    """result geometry ({type:"polygon", points:[[x,y],...]}) → 归一化顶点列表。"""
    points = geometry.get("points") or []
    return [[float(p[0]), float(p[1])] for p in points if len(p) >= 2]


async def _materialize_tracker_mask_result(
    result: TrackerFrameResult,
) -> TrackerFrameResult:
    geometry = result.geometry or {}
    if geometry.get("type") != "mask":
        return result
    rle = geometry.get("rle")
    if not isinstance(rle, dict):
        raise ValueError("tracker mask result requires geometry.rle")
    reference = await store_coco_rle(rle)
    bbox = coco_rle_bbox_norm(rle)
    return replace(
        result,
        geometry={"type": "mask", "mask": reference, "bbox": bbox},
        outside=result.outside or not bbox,
    )


def _tracker_result_json_bytes(result: TrackerFrameResult) -> int:
    return len(
        json.dumps(_serialize_results([result])[0], separators=(",", ":")).encode()
    )


async def _mask_track_seed_geometry(geometry: dict, frame_index: int) -> dict:
    resolved = resolve_track_at_frame(geometry, frame_index)
    if resolved is None:
        return geometry
    rle = await load_coco_rle(resolved.get("mask") or {})
    bbox = coco_rle_bbox_norm(rle)
    return {"type": "bbox", **bbox} if bbox else geometry


def _tracker_windows(job: VideoTrackerJob) -> list[tuple[int, int]]:
    size = max(1, int(settings.video_tracker_window_size_frames))
    # sam3 两档视频模型都用更小分窗(不动 sam2 的窗口, 避免回归其长程记忆):
    # - sam3_video(multiplex): 视频前向显存随窗口线性增长, 大窗 OOM@24GB。
    # - sam3_video_interactive(PVS): backend wrapper 上限 SAM3_PVS_MAX_WINDOW_FRAMES(默认 16),
    #   超限会被 backend 拒; PVS 是 SAM2 式 memory 传播、显存轻于 multiplex, 但先与之齐。
    # v0.22.2 · B-combo · sam3_video_combo 追踪趟走 PVS, 用与 PVS 齐的小分窗。
    if job.model_key in ("sam3_video", "sam3_video_interactive", "sam3_video_combo"):
        size = min(size, max(1, int(settings.video_tracker_sam3_window_size_frames)))
    windows = []
    start = job.from_frame
    while start <= job.to_frame:
        end = min(job.to_frame, start + size - 1)
        windows.append((start, end))
        start = end + 1
    if job.direction == "backward":
        windows.reverse()
    return windows


def _bounded_tracker_windows(
    from_frame: int, to_frame: int, direction: str, size: int
) -> list[tuple[int, int]]:
    windows: list[tuple[int, int]] = []
    start = from_frame
    while start <= to_frame:
        end = min(to_frame, start + max(1, size) - 1)
        windows.append((start, end))
        start = end + 1
    if direction == "backward":
        windows.reverse()
    return windows


def _correction_execution_windows(
    job: VideoTrackerJob,
) -> list[tuple[int, int, str, bool]]:
    correction_frame = int(
        job.correction_frame if job.correction_frame is not None else job.from_frame
    )
    correction = (job.prompt or {}).get("correction") or {}
    routing = correction.get("routing") or {}
    frozen_limit = routing.get("max_window_frames")
    size = max(1, int(settings.video_tracker_window_size_frames))
    if job.model_key.startswith("sam3"):
        size = min(size, max(1, int(settings.video_tracker_sam3_window_size_frames)))
    if type(frozen_limit) is int and frozen_limit > 0:
        size = min(size, frozen_limit)
    windows: list[tuple[int, int, str, bool]] = []
    if job.direction in {"backward", "bidirectional"}:
        backward = _bounded_tracker_windows(
            job.from_frame, correction_frame, "backward", size
        )
        windows.extend(
            (start, end, "backward", index == 0)
            for index, (start, end) in enumerate(backward)
        )
    if job.direction in {"forward", "bidirectional"}:
        forward = _bounded_tracker_windows(
            correction_frame, job.to_frame, "forward", size
        )
        windows.extend(
            (start, end, "forward", index == 0)
            for index, (start, end) in enumerate(forward)
        )
    return windows


def _tracker_session_capability(
    backend: object | None, model_key: str
) -> tuple[str, int]:
    capabilities = (getattr(backend, "health_meta", None) or {}).get(
        "capabilities"
    ) or {}
    for model in capabilities.get("models") or []:
        if model_key not in set(model.get("supported_trackers") or []):
            continue
        mode = model.get("tracker_context_mode")
        limit = model.get("max_context_frames")
        if mode == "session" and type(limit) is int and limit > 0:
            return mode, limit
    return "none", 0


def _context_spans(
    windows: list[tuple[int, int, str, bool]], max_frames: int
) -> dict[int, tuple[int, int]]:
    spans: dict[int, tuple[int, int]] = {}
    start_index = 0
    while start_index < len(windows):
        direction = windows[start_index][2]
        indexes = [start_index]
        lo, hi = windows[start_index][0], windows[start_index][1]
        for index in range(start_index + 1, len(windows)):
            if windows[index][2] != direction:
                break
            next_lo = min(lo, windows[index][0])
            next_hi = max(hi, windows[index][1])
            if next_hi - next_lo + 1 > max_frames:
                break
            indexes.append(index)
            lo, hi = next_lo, next_hi
        for index in indexes:
            spans[index] = (lo, hi)
        start_index = indexes[-1] + 1
    return spans


async def _correction_seed(
    db: AsyncSession, job: VideoTrackerJob, annotation: Annotation
) -> list[dict]:
    correction = (job.prompt or {}).get("correction") or {}
    if int(annotation.version or 1) != int(correction.get("source_version", 0)):
        raise ValueError("source_version_conflict")
    geometry = annotation.geometry or {}
    if geometry.get("type") != "video_track_mask":
        raise ValueError("video_mask_track_required")
    track_id = str(annotation.track_id or geometry.get("track_id") or "")
    if track_id != str(correction.get("track_id") or ""):
        raise ValueError("track_identity_changed")
    correction_frame = int(
        job.correction_frame if job.correction_frame is not None else -1
    )
    keyframe = next(
        (
            item
            for item in geometry.get("keyframes") or []
            if int(item.get("frame_index", -1)) == correction_frame
        ),
        None,
    )
    if not isinstance(keyframe, dict) or keyframe.get("source") != "manual":
        raise ValueError("manual_correction_frame_missing")
    reference = keyframe.get("mask") or {}
    digest = str(reference.get("sha256") or "")
    if digest != str(correction.get("corrected_digest") or ""):
        raise ValueError("corrected_mask_digest_mismatch")
    segment_snapshot = correction.get("segment") or {}
    segment = await db.get(VideoSegment, job.segment_id) if job.segment_id else None
    task = await db.get(Task, job.task_id)
    work_start, work_end = (
        await segment_work_bounds_for_task(db, task=task, segment=segment)
        if task is not None and segment is not None
        else (-1, -1)
    )
    lease_enforced = segment_snapshot.get("lease_enforced") is not False
    if (
        segment is None
        or str(segment.id) != str(segment_snapshot.get("id") or "")
        or job.from_frame < work_start
        or job.to_frame > work_end
        or (
            lease_enforced
            and (
                (str(segment.locked_by) if segment.locked_by else None)
                != segment_snapshot.get("locked_by")
                or (segment.locked_at.isoformat() if segment.locked_at else None)
                != segment_snapshot.get("locked_at")
                or segment.lock_expires_at is None
                or segment.lock_expires_at <= _now()
            )
        )
    ):
        raise ValueError("segment_lease_changed")

    if correction.get("seed_mode") == "native_mask":
        rle = await load_coco_rle(reference)
        prompt = CorrectionFramePrompt.model_validate(
            {
                "frame_index": correction_frame,
                "direction": job.direction,
                "mask_prompt": {
                    "rle": rle,
                    "source_annotation_id": str(annotation.id),
                    "source_version": int(annotation.version or 1),
                    "source_digest": digest,
                },
                "output_geometry": "mask",
            }
        ).model_dump(mode="json")
    else:
        if (
            correction.get("fallback_reason") != "mask_prompt_unsupported"
            or correction.get("fallback_confirmed") is not True
        ):
            raise ValueError("bbox_fallback_not_confirmed")
        return [{"obj_id": 1, "bbox": dict(correction.get("seed_bbox") or {})}]
    return [{"obj_id": 1, "prompts": [prompt]}]


def _source_keyframe(annotation: Annotation, job: VideoTrackerJob) -> dict:
    geometry = annotation.geometry or {}
    frame_index = int(geometry.get("frame_index", job.from_frame))
    return {
        "frame_index": frame_index,
        "bbox": _normalize_bbox(geometry),
        "source": "manual",
        "occluded": False,
    }


def _coerce_video_track_geometry(
    annotation: Annotation, job: VideoTrackerJob, target_kind: str
) -> dict:
    geometry = annotation.geometry or {}
    track_id = str(annotation.track_id or geometry.get("track_id") or _new_track_id())
    target_type = {
        "polygon": "video_track_polygon",
        "mask": "video_track_mask",
    }.get(target_kind, "video_track_bbox")
    if geometry.get("type") == target_type:
        return {
            "type": target_type,
            "track_id": track_id,
            "keyframes": [dict(item) for item in geometry.get("keyframes") or []],
            "outside": [dict(item) for item in geometry.get("outside") or []],
        }
    if target_kind in {"polygon", "mask"}:
        return {
            "type": target_type,
            "track_id": track_id,
            "keyframes": [],
            "outside": [],
        }
    return {
        "type": "video_track_bbox",
        "track_id": track_id,
        "keyframes": [_source_keyframe(annotation, job)],
        "outside": [],
    }


def _merge_outside_ranges(existing: list[dict], frames: list[int]) -> list[dict]:
    ranges = [dict(item) for item in existing]
    if not frames:
        return ranges

    start = previous = frames[0]
    for frame_index in frames[1:]:
        if frame_index == previous + 1:
            previous = frame_index
            continue
        ranges.append({"from": start, "to": previous, "source": "prediction"})
        start = previous = frame_index
    ranges.append({"from": start, "to": previous, "source": "prediction"})
    return ranges


def apply_tracker_results(
    annotation: Annotation,
    job: VideoTrackerJob,
    results: list[TrackerFrameResult],
    grid_step: int = 1,
    output_geometry: str | None = None,
    override_manual_frames: set[int] | None = None,
) -> None:
    result_kind = next(
        (
            result.geometry.get("type")
            for result in results
            if not result.outside and isinstance(result.geometry, dict)
        ),
        None,
    )
    if output_geometry in {"bbox", "polygon", "mask"}:
        target_kind = output_geometry
    else:
        target_kind = (
            "mask"
            if result_kind == "mask"
            else "polygon"
            if result_kind == "polygon"
            else "bbox"
        )
    geometry = _coerce_video_track_geometry(annotation, job, target_kind)
    is_polygon = target_kind == "polygon"
    is_mask = target_kind == "mask"
    keyframes = geometry["keyframes"]
    overridden = override_manual_frames or set()
    manual_frames = {
        int(item.get("frame_index", 0))
        for item in keyframes
        if item.get("source", "manual") == "manual"
        and int(item.get("frame_index", 0)) not in overridden
    }
    prediction_by_frame = {
        int(item.get("frame_index", 0)): item
        for item in keyframes
        if item.get("source") != "manual"
    }
    outside_frames: list[int] = []

    for result in results:
        # 采样开启时只回填落在网格上的帧（D2：tracker 仍逐源帧算并用于跨窗续追，
        # 但只持久化网格帧，与导航/导出网格一致，避免编辑器里堆满够不到的关键帧）。
        if grid_step > 1 and result.frame_index % grid_step != 0:
            continue
        if result.outside:
            outside_frames.append(result.frame_index)
            prediction_by_frame.pop(result.frame_index, None)
            continue
        if result.frame_index in manual_frames:
            continue
        if is_mask:
            reference = result.geometry.get("mask")
            bbox = result.geometry.get("bbox") or {}
            if not isinstance(reference, dict) or float(bbox.get("w", 0)) <= 0:
                outside_frames.append(result.frame_index)
                prediction_by_frame.pop(result.frame_index, None)
                continue
            shape_field = {"mask": dict(reference)}
        elif is_polygon:
            points = _normalize_points(result.geometry)
            if len(points) < 3:
                # 退化多边形(顶点<3)当 outside 处理, 避免写坏 schema。
                outside_frames.append(result.frame_index)
                prediction_by_frame.pop(result.frame_index, None)
                continue
            shape_field = {"points": points}
        else:
            shape_field = {"bbox": _normalize_bbox(result.geometry)}
        prediction_by_frame[result.frame_index] = {
            "frame_index": result.frame_index,
            **shape_field,
            "source": "prediction",
            "occluded": False,
        }

    outside_frame_set = set(outside_frames)
    merged = [
        item
        for item in keyframes
        if (
            item.get("source", "manual") == "manual"
            and int(item.get("frame_index", 0)) not in overridden
        )
        or (
            int(item.get("frame_index", 0)) not in prediction_by_frame
            and int(item.get("frame_index", 0)) not in outside_frame_set
        )
    ]
    merged.extend(prediction_by_frame.values())
    geometry["keyframes"] = sorted(
        merged, key=lambda item: int(item.get("frame_index", 0))
    )
    visible_result_frames = {
        result.frame_index for result in results if not result.outside
    }
    existing_outside = geometry.get("outside") or []
    for frame_index in sorted(visible_result_frames):
        existing_outside = remove_frame_from_outside_ranges(
            existing_outside, frame_index
        )
    geometry["outside"] = _merge_outside_ranges(
        existing_outside, sorted(set(outside_frames))
    )

    annotation.geometry = geometry
    annotation.track_id = str(geometry["track_id"])
    annotation.annotation_type = geometry["type"]
    if is_mask:
        annotation.tool_unit_id = "region"
    annotation.version = int(annotation.version or 1) + 1


def _partition_results_by_instance(
    results: list[TrackerFrameResult],
) -> tuple[list[TrackerFrameResult], dict[str, list[TrackerFrameResult]]]:
    """把逐帧结果拆成 (主实例结果, {instance_id: 非主实例结果}) —— 阶段 0 多目标落库。

    模式 a「自动发现」下 backend 一帧可返回多实例。主实例 = 与用户种子对应的那个,
    回填源 annotation; 其余每个 instance_id 各成一条新 track。
    - 无任何 instance_id (单实例老 backend): 全部归主, 与既有单 track 行为等价 (零回归)。
    - 有 instance_id: primary 标记所属的 instance 归主; 若无一标 primary, 取最小的
      instance_id 归主 (数字 id 按数值比较, 否则字典序; 确定性兜底)。同一 instance_id 的
      所有帧整体归属同一去向。
    """
    if all(r.instance_id is None for r in results):
        return list(results), {}

    primary_id = next(
        (r.instance_id for r in results if r.primary and r.instance_id is not None),
        None,
    )
    if primary_id is None:
        # instance_id 契约上是 str(obj_id) (见 _instance_seed_obj_id), 但可能是非数字兜底。
        # 纯数字按数值取 min ("2" < "10"), 否则字典序, 避免 "10" < "2" 挑错主实例。
        primary_id = min(
            (r.instance_id for r in results if r.instance_id is not None),
            key=lambda s: (0, int(s)) if s.isdigit() else (1, s),
        )

    primary: list[TrackerFrameResult] = []
    extras: dict[str, list[TrackerFrameResult]] = {}
    for result in results:
        if result.instance_id is None or result.instance_id == primary_id:
            primary.append(result)
        else:
            extras.setdefault(result.instance_id, []).append(result)
    return primary, extras


# v0.22.2 · M · 多选批量: 单源延展时从结果推断主实例 id, 构造单源 source_map 的键。
_SOLE_SOURCE_KEY = "__sole__"


def _primary_instance_id(results: list[TrackerFrameResult]) -> str | None:
    """单源延展时推断与用户种子对应的主实例 id (primary 标记 → 最小 id → None 全无 id)。
    复用 _partition_results_by_instance 的主实例判定, 供构造单源 source_map。"""
    primary, _ = _partition_results_by_instance(results)
    return primary[0].instance_id if primary else None


def _instance_seed_obj_id(instance_id: str, fallback: int) -> int:
    """把 instance_id 还原成 backend 播种用的 obj_id。PVS 的 instance_id 就是 str(obj_id),
    直接 int; 非数字 (老 backend 兜底) 按序补 fallback。"""
    return int(instance_id) if instance_id.isdigit() else fallback


def _continuation_seeds(last_geom_by_instance: dict[str, dict]) -> list[dict]:
    """多目标跨窗续追种子: 每个实例用其上一窗末帧几何 + 同一 obj_id 重播种下一窗。

    v0.21.27 · U-pvs-1 · seed-驱动 tracker (PVS) 每窗是独立会话, 若只续主实例, 非主实例
    过一窗即丢。这里对**每个**实例各下发一条 seed (obj_id 与其 instance_id 一致 → 跨窗身份
    稳定); backend seeds 支持 geometry (自动取外接框)。text-驱动 multiplex 同样把这些框作为
    下一窗正提示，并继续按 text 发现目标。
    """
    return [
        {"obj_id": _instance_seed_obj_id(iid, idx + 1), "geometry": geom}
        for idx, (iid, geom) in enumerate(sorted(last_geom_by_instance.items()))
    ]


# ── v0.22.2 · B-combo · multiplex 发现 → PVS 种子铸造 ──────────────────────
# combo (sam3_video_combo) 两趟编排的桥: 发现趟 (multiplex 按 text 在种子帧检测) 的
# per-obj 结果 → 追踪趟 (PVS) 的逐对象种子。发现对象**无源** → 种子不带
# source_annotation_id, 落库走成熟的无源新建 (source_map 为空 → 全部 _new_discovered_track)。


def _combo_seeds_from_discovery(
    discovery_results: list["TrackerFrameResult"],
    *,
    seed_frame: int,
) -> list[dict]:
    """发现趟结果 → PVS 种子 (逐对象一条, obj_id=1..N, geometry=发现框)。

    multiplex 需多帧传播才会在**种子帧**填充检测 (单帧窗不出 obj_id), 故发现趟跑一小窗但
    只取 seed_frame 这一帧的 per-obj 框铸种 (传播帧仅为让模型锁定对象)。按 instance_id 稳定
    排序后逐个铸成 PVS 种子 (obj_id 连续从 1 起, 与 _instance_seed_obj_id 契约一致), geometry
    直接用发现框 (PVS backend seeds 支持 geometry, 自动取外接框)。outside / 空框跳过。种子
    不带 source_annotation_id → 落库全部新建。
    """
    seeds: list[dict] = []
    obj = 1
    for result in sorted(
        discovery_results, key=lambda r: (r.instance_id or "", r.frame_index)
    ):
        if result.frame_index != seed_frame or result.outside or not result.geometry:
            continue
        seeds.append({"obj_id": obj, "geometry": result.geometry})
        obj += 1
    return seeds


# ── v0.21.28 · B-mx · text-multiplex 跨窗 IoU 关联 ─────────────────────────
# multiplex (sam3_video) 按 text 每窗独立会话重检测, 窗内 obj_id 稳定但**跨窗局部**
# (窗 N 的 obj "1" 与窗 N-1 的 obj "1" 未必同物)。平台在窗边界帧按 IoU 关联, 把窗内
# obj_id remap 成跨窗稳定的全局 instance_id (未匹配 → 新 track), 供阶段 0 分组落库。


def _bbox_of_geometry(geom: dict) -> tuple[float, float, float, float]:
    """任意 geometry → 归一化 (x, y, w, h) 外接框; 取不到 → 零框。"""
    if not isinstance(geom, dict):
        return (0.0, 0.0, 0.0, 0.0)
    if geom.get("type") == "polygon":
        pts = geom.get("points") or []
        xs = [float(p[0]) for p in pts if len(p) >= 2]
        ys = [float(p[1]) for p in pts if len(p) >= 2]
        if not xs or not ys:
            return (0.0, 0.0, 0.0, 0.0)
        return (min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))
    if geom.get("type") == "mask":
        bbox = geom.get("bbox") or {}
        return (
            float(bbox.get("x", 0.0)),
            float(bbox.get("y", 0.0)),
            float(bbox.get("w", 0.0)),
            float(bbox.get("h", 0.0)),
        )
    return (
        float(geom.get("x", 0.0)),
        float(geom.get("y", 0.0)),
        float(geom.get("w", geom.get("width", 0.0))),
        float(geom.get("h", geom.get("height", 0.0))),
    )


def _geom_iou(g1: dict, g2: dict) -> float:
    """两 geometry 外接框的 IoU (归一化坐标)。任一零框 → 0。"""
    x1, y1, w1, h1 = _bbox_of_geometry(g1)
    x2, y2, w2, h2 = _bbox_of_geometry(g2)
    if w1 <= 0 or h1 <= 0 or w2 <= 0 or h2 <= 0:
        return 0.0
    ix1, iy1 = max(x1, x2), max(y1, y2)
    ix2, iy2 = min(x1 + w1, x2 + w2), min(y1 + h1, y2 + h2)
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    union = w1 * h1 + w2 * h2 - inter
    return inter / union if union > 0 else 0.0


def _associate_multiplex_window(
    window_results: list[TrackerFrameResult],
    prev_boundary: dict[str, dict],
    next_global: list[int],
    iou_threshold: float = 0.3,
) -> list[TrackerFrameResult]:
    """把一窗 text-multiplex 结果的窗内 instance_id remap 成跨窗稳定的全局 id。

    每个窗内实例的**边界帧**(组内首个非 outside 结果 = 传播起点, 与上一窗相邻)几何与
    ``prev_boundary``(上一窗每全局实例的末帧几何)按 IoU 贪心匹配; ≥阈值则复用该全局 id,
    否则分配新全局 id。``prev_boundary`` 就地更新为本窗每全局实例的**末帧**几何(供下一窗
    匹配)。primary 标记随结果保留(replace 只改 instance_id)。
    """
    by_local: dict[str, list[TrackerFrameResult]] = defaultdict(list)
    for r in window_results:
        by_local[r.instance_id or "0"].append(r)
    remap: dict[str, str] = {}
    used: set[str] = set()
    for local_id, group in by_local.items():
        head = next((r for r in group if not r.outside and r.geometry), None)
        gid: str | None = None
        if head is not None and prev_boundary:
            best = iou_threshold
            for cand_gid, geom in prev_boundary.items():
                if cand_gid in used:
                    continue
                iou = _geom_iou(head.geometry, geom)
                if iou >= best:
                    best, gid = iou, cand_gid
        if gid is None:
            gid = str(next_global[0])
            next_global[0] += 1
        used.add(gid)
        remap[local_id] = gid
    remapped = [
        replace(r, instance_id=remap[r.instance_id or "0"]) for r in window_results
    ]
    new_boundary: dict[str, dict] = {}
    for r in remapped:  # yield 序: 末帧最后写入 → 与下一窗相邻的边界
        if not r.outside and r.geometry and r.instance_id:
            new_boundary[r.instance_id] = r.geometry
    # 本窗全 outside / 空产出 (短暂遮挡 / backend 空窗) 时保留上一窗边界: 否则遮挡帧会把
    # 跨窗身份抹掉, 下一窗所有实例被当作新发现 → 同一物体在遮挡后被拆成两条轨迹。
    if new_boundary:
        prev_boundary.clear()
        prev_boundary.update(new_boundary)
    return remapped


@dataclass(frozen=True)
class _TrackTarget:
    """新建轨迹的归属模板: 有源延展取自源轨迹, 无源检测取自 job 的显式目标类别。"""

    task_id: uuid.UUID
    project_id: uuid.UUID
    video_segment_id: uuid.UUID | None
    user_id: uuid.UUID | None
    class_name: str
    tool_unit_id: str


def _target_from_source(source: Annotation) -> _TrackTarget:
    return _TrackTarget(
        task_id=source.task_id,
        project_id=source.project_id,
        video_segment_id=source.video_segment_id,
        user_id=source.user_id,
        class_name=source.class_name,
        tool_unit_id=source.tool_unit_id,
    )


def _target_from_job(job: VideoTrackerJob, task: Task) -> _TrackTarget:
    # v0.22.1 · B · 无源检测: 类别由 job.target_* 显式指定 (缺省兜底空类 / bbox 单位)。
    return _TrackTarget(
        task_id=job.task_id,
        project_id=task.project_id,
        video_segment_id=job.segment_id,
        user_id=job.created_by,
        class_name=job.target_class_name or "",
        tool_unit_id=job.target_tool_unit_id or "bbox",
    )


def _seed_source_map(job: VideoTrackerJob) -> dict[str, uuid.UUID]:
    """从 job.prompt.seeds 读 {instance_id: 源 annotation id} —— v0.22.2 · M · 多选批量。

    每个 seed 可带 source_annotation_id (该 obj 延展哪条已有轨迹); 无该字段的 seed
    (无源检测 / B-combo 发现) 不入映射。instance_id 契约 = str(obj_id)。
    """
    seeds = ((job.prompt or {}).get("seeds")) or []
    mapping: dict[str, uuid.UUID] = {}
    for seed in seeds:
        if not isinstance(seed, dict):
            continue
        src = seed.get("source_annotation_id")
        obj_id = seed.get("obj_id")
        if not src or obj_id is None:
            continue
        try:
            mapping[str(obj_id)] = uuid.UUID(str(src))
        except (ValueError, TypeError):
            continue
    return mapping


def _new_discovered_track(target: _TrackTarget, output_geometry: str) -> Annotation:
    """为一个新发现的实例建一条空 video_track annotation。

    归属由 _TrackTarget 提供 (有源继承源 label, 无源用 job 显式目标类别), 标
    source="ai_tracker" (与批量预标 prediction_based 区分, 不被批量清理误删), 新 track_id
    走统一工厂 _new_track_id()。几何先空, 由 apply_tracker_results 填预测帧。
    """
    track_id = _new_track_id()
    geom_type = {
        "polygon": "video_track_polygon",
        "mask": "video_track_mask",
    }.get(output_geometry, "video_track_bbox")
    return Annotation(
        id=uuid.uuid4(),
        task_id=target.task_id,
        project_id=target.project_id,
        video_segment_id=target.video_segment_id,
        user_id=target.user_id,
        source="ai_tracker",
        annotation_type=geom_type,
        tool_unit_id="region" if output_geometry == "mask" else target.tool_unit_id,
        class_name=target.class_name,
        geometry={
            "type": geom_type,
            "track_id": track_id,
            "keyframes": [],
            "outside": [],
        },
        track_id=track_id,
    )


def _discovered_track_has_keyframes(annotation: Annotation) -> bool:
    return bool((annotation.geometry or {}).get("keyframes"))


def _persist_tracker_results(
    db: AsyncSession,
    source_map: dict[str, Annotation],
    target: _TrackTarget,
    job: VideoTrackerJob,
    results: list[TrackerFrameResult],
    grid_step: int,
    output_geometry: str,
) -> list[Annotation]:
    """落库逐帧结果: 按 instance_id 分组, 命中 source_map 的实例回填对应源 annotation,
    未命中的每个 instance 各建一条新 track。

    source_map 语义 (instance_id → 源 annotation):
    - 单源延展 (0.22.1 及以前): 恰一个条目 {主实例 id: 源};
    - 多源批量 (v0.22.2 · M · 多选): N 个条目 {obj_id: 各自源}, 各回填各源;
    - 无源检测 / B-combo 发现: 空映射, 全部新建 (归属取自 target)。

    无 instance_id 的老单实例 backend: 整体回填 source_map 的唯一源 (若有), 否则新建 ——
    与既有单 track 行为等价, 零回归。返回新建的 annotation 列表 (调用方负责 commit)。
    """
    created: list[Annotation] = []
    if all(r.instance_id is None for r in results):
        sole_source = next(iter(source_map.values()), None)
        if sole_source is not None:
            apply_tracker_results(sole_source, job, results, grid_step, output_geometry)
        else:
            main_ann = _new_discovered_track(target, output_geometry)
            apply_tracker_results(main_ann, job, results, grid_step, output_geometry)
            if _discovered_track_has_keyframes(main_ann):
                db.add(main_ann)
                created.append(main_ann)
        return created

    by_instance: dict[str, list[TrackerFrameResult]] = {}
    for result in results:
        by_instance.setdefault(result.instance_id or "", []).append(result)
    for instance_id in sorted(by_instance):
        inst_results = by_instance[instance_id]
        source = source_map.get(instance_id)
        if source is not None:
            apply_tracker_results(source, job, inst_results, grid_step, output_geometry)
        else:
            new_ann = _new_discovered_track(target, output_geometry)
            apply_tracker_results(
                new_ann, job, inst_results, grid_step, output_geometry
            )
            if _discovered_track_has_keyframes(new_ann):
                db.add(new_ann)
                created.append(new_ann)
    return created


# ---------- v0.21.28 · 候选/接受流: 暂存 + accept/discard ----------


def _serialize_results(results: list[TrackerFrameResult]) -> list[dict]:
    rows: list[dict] = []
    for result in results:
        row = {
            "frame_index": result.frame_index,
            "geometry": result.geometry,
            "confidence": result.confidence,
            "outside": result.outside,
            "instance_id": result.instance_id,
            "primary": result.primary,
        }
        row["geometry_digest"] = _tracker_candidate_digest(row)
        row["candidate_key"] = _tracker_candidate_key(row)
        rows.append(row)
    return rows


def _tracker_instance_key(row: dict) -> str:
    value = row.get("instance_id")
    return str(value) if value is not None else "1"


def _tracker_candidate_digest(row: dict) -> str:
    payload = {
        "geometry": row.get("geometry") or {},
        "outside": bool(row.get("outside", False)),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _tracker_candidate_key(row: dict) -> str:
    digest = str(row.get("geometry_digest") or _tracker_candidate_digest(row))
    return f"{_tracker_instance_key(row)}:{int(row['frame_index'])}:{digest}"


def _ensure_candidate_contract(row: dict) -> dict:
    enriched = dict(row)
    enriched["geometry_digest"] = str(
        enriched.get("geometry_digest") or _tracker_candidate_digest(enriched)
    )
    enriched["candidate_key"] = str(
        enriched.get("candidate_key") or _tracker_candidate_key(enriched)
    )
    return enriched


def _deserialize_results(rows: list[dict]) -> list[TrackerFrameResult]:
    return [
        TrackerFrameResult(
            frame_index=int(row["frame_index"]),
            geometry=row.get("geometry") or {},
            confidence=row.get("confidence"),
            outside=bool(row.get("outside", False)),
            instance_id=row.get("instance_id"),
            primary=bool(row.get("primary", False)),
        )
        for row in rows
    ]


def _stage_tracker_results(
    job: VideoTrackerJob,
    results: list[TrackerFrameResult],
    grid_step: int,
    output_geometry: str,
) -> None:
    """把逐帧结果暂存进 job.staged_result (候选), **不碰 annotation**。用户接受时反序列化后
    走 _persist_tracker_results 落库。grid_step / output_geometry 一并存, 供 accept 复用同一
    落库逻辑 (无需在 accept 时重新推导)。"""
    serialized = _serialize_results(results)
    source_instance_id = None
    if job.annotation_id is not None and not _seed_source_map(job):
        source_instance_id = _primary_instance_id(results) or "1"
    staged = {
        "results": serialized,
        "grid_step": grid_step,
        "output_geometry": output_geometry,
        "source_instance_id": source_instance_id,
    }
    if (
        len(json.dumps(staged, separators=(",", ":")).encode())
        > MAX_TRACKER_STAGED_BYTES
    ):
        raise ValueError("tracker_candidate_too_large: staged payload exceeds 64 MiB")
    job.staged_result = staged


async def _assert_source_versions_unchanged(
    db: AsyncSession, job: VideoTrackerJob, source_ids: set[uuid.UUID]
) -> dict[uuid.UUID, Annotation]:
    """v0.23.5 · WS-D · D4 · reject accept when a source annotation was mutated
    after the job was created.

    ``create_tracker_job`` snapshotted each source annotation's ``version`` into
    ``job.prompt["expected_source_versions"]`` (keyed by stringified annotation
    id). Here we re-read each source with ``with_for_update`` (row lock, same
    transaction as the rest of accept) and compare. Any drift → rollback +
    ``TrackerJobStateConflict`` (409), so the caller can surface the conflict
    instead of last-writer-wins overwriting the user's edit.

    Legacy jobs with sources and no version snapshot cannot prove that their
    source is unchanged, so acceptance fails closed.
    """
    prompt = job.prompt or {}
    expected = prompt.get("expected_source_versions")
    if not expected:
        if source_ids:
            raise TrackerJobStateConflict(
                "source version snapshot is missing; candidate cannot be accepted safely"
            )
        expected = {}
    if not isinstance(expected, dict):
        raise TrackerJobStateConflict(
            "expected_source_versions is malformed; cannot verify source versions"
        )
    if expected and not {str(source_id) for source_id in source_ids}.issubset(expected):
        raise TrackerJobStateConflict(
            "expected_source_versions does not cover the selected tracker sources",
            reason="source_version_conflict",
        )
    locked: dict[uuid.UUID, Annotation] = {}
    for annotation_id in sorted(source_ids, key=str):
        key = str(annotation_id)
        recorded = expected.get(key)
        if expected and recorded is None:
            # Source is in source_map but not in the snapshot — only happens if
            # the snapshot was built inconsistently with seed_sources. Treat as
            # conflict (fail closed).
            raise TrackerJobStateConflict(
                "source annotation version changed; conflict "
                f"(missing snapshot for annotation {key})"
            )
        # Re-read under row lock so a concurrent mutation blocks until we
        # decide. ``with_for_update`` runs in the same transaction as accept's
        # later writes, so the lock is held through commit/rollback.
        row = (
            await db.execute(
                select(Annotation)
                .where(Annotation.id == annotation_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        current_version = int(row.version) if row is not None else None
        if (
            row is None
            or not row.is_active
            or row.task_id != job.task_id
            or row.is_locked
        ):
            raise TrackerJobStateConflict(
                f"source annotation {key} is missing, inactive, moved, or locked"
            )
        if recorded is not None and current_version != int(recorded):
            raise TrackerJobStateConflict(
                "source annotation version changed; conflict "
                f"(annotation {key}: expected {recorded}, got {current_version})",
                reason="source_version_conflict",
                conflicts=[
                    {
                        "annotation_id": key,
                        "expected_version": recorded,
                        "current_version": current_version,
                    }
                ],
            )
        locked[row.id] = row
    return locked


def _review_state(job: VideoTrackerJob) -> dict:
    raw = (job.prompt or {}).get("review_state")
    return dict(raw) if isinstance(raw, dict) else {}


def _review_source_map_ids(job: VideoTrackerJob, staged: dict) -> dict[str, uuid.UUID]:
    mapping = _seed_source_map(job)
    state = _review_state(job)
    for instance_id, annotation_id in (state.get("instance_annotations") or {}).items():
        try:
            mapping[str(instance_id)] = uuid.UUID(str(annotation_id))
        except (TypeError, ValueError):
            continue
    if job.annotation_id is not None and not _seed_source_map(job):
        source_instance = staged.get("source_instance_id")
        if not source_instance:
            rows = [
                _ensure_candidate_contract(row)
                for row in (staged.get("results") or [])
                if isinstance(row, dict)
            ]
            primary = next((row for row in rows if row.get("primary")), None)
            source_instance = _tracker_instance_key(primary or rows[0]) if rows else "1"
        mapping[str(source_instance)] = job.annotation_id
    return mapping


def _geometry_sha256(geometry: dict) -> str:
    raw = json.dumps(geometry or {}, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


_BUSY_SQLSTATES = {"55P03", "40001", "40P01"}


def _busy_lock_error(exc: BaseException) -> bool:
    orig = getattr(exc, "orig", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return sqlstate in _BUSY_SQLSTATES


async def _job_project_id(db: AsyncSession, job_id: uuid.UUID):
    """Lightweight project id for a job, used for account-first membership locks."""

    return (
        await db.execute(
            select(Task.project_id)
            .select_from(VideoTrackerJob)
            .join(Task, Task.id == VideoTrackerJob.task_id)
            .where(VideoTrackerJob.id == job_id)
        )
    ).scalar_one_or_none()


async def _lock_actor_membership(
    db: AsyncSession, actor_id: uuid.UUID | None, job_id: uuid.UUID
) -> None:
    from fastapi import HTTPException

    from app.services.project_write_guard import lock_actor_scope

    project_id = await _job_project_id(db, job_id)
    if project_id is None:
        return
    try:
        await lock_actor_scope(db, actor_id, project_id)
    except HTTPException as exc:
        raise TrackerJobStateConflict(
            "project membership is busy", reason="tracker_job_busy"
        ) from exc


async def _lock_actor_share(db: AsyncSession, actor_id: uuid.UUID | None):
    """Account-first bounded share lock for a tracker final write."""

    from sqlalchemy.exc import DBAPIError

    from app.db.models.user import User

    if actor_id is None:
        raise TrackerJobStateConflict(
            "an explicit actor is required for tracker final writes",
            reason="permission_changed",
        )
    try:
        actor = (
            await db.execute(
                select(User)
                .where(User.id == actor_id)
                .with_for_update(read=True, nowait=True)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
    except DBAPIError as exc:
        if _busy_lock_error(exc):
            raise TrackerJobStateConflict(
                "tracker actor is busy", reason="tracker_job_busy"
            ) from exc
        raise
    if actor is None or not actor.is_active:
        raise TrackerJobStateConflict(
            "tracker actor is unavailable", reason="permission_changed"
        )
    return actor


async def _lock_task_bounded(db: AsyncSession, task_id: uuid.UUID):
    """Bounded NOWAIT task lock for the mixed route-Task/runner-Job order."""

    from sqlalchemy.exc import DBAPIError

    try:
        return (
            await db.execute(
                select(Task)
                .where(Task.id == task_id)
                .with_for_update(nowait=True)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
    except DBAPIError as exc:
        if _busy_lock_error(exc):
            raise TrackerJobStateConflict("task is busy", reason="task_locked") from exc
        raise


async def _lock_segment_bounded(db: AsyncSession, segment_id: uuid.UUID):
    """Bounded NOWAIT segment lock for the final-write path."""

    from sqlalchemy.exc import DBAPIError

    try:
        return (
            await db.execute(
                select(VideoSegment)
                .where(VideoSegment.id == segment_id)
                .with_for_update(nowait=True)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
    except DBAPIError as exc:
        if _busy_lock_error(exc):
            raise TrackerJobStateConflict(
                "video segment is busy", reason="segment_lease_changed"
            ) from exc
        raise


async def _assert_tracker_actor_authority(
    db: AsyncSession,
    task: Task,
    actor_id: uuid.UUID | None,
) -> None:
    """Recheck the actual final-write actor after the Task/Job locks.

    Requires an explicit actor (never the job creator), refreshes the account
    and re-resolves the current project membership/phase/frozen evidence.
    Raises ``TrackerJobStateConflict`` so the existing route error mapping is
    unchanged and no partial write is applied.
    """

    from fastapi import HTTPException

    from app.db.models.user import User
    from app.services.project_write_guard import assert_phase_write_allowed

    if actor_id is None:
        raise TrackerJobStateConflict(
            "an explicit actor is required for tracker final writes",
            reason="permission_changed",
        )
    actor = await db.get(User, actor_id)
    if actor is None or not actor.is_active:
        raise TrackerJobStateConflict(
            "tracker actor is unavailable", reason="permission_changed"
        )
    try:
        await assert_phase_write_allowed(db, task, actor, action="tracker_write")
    except HTTPException as exc:
        detail = exc.detail
        reason = detail.get("reason") if isinstance(detail, dict) else None
        raise TrackerJobStateConflict(
            "project authority changed before tracker write",
            reason=reason or "permission_changed",
        ) from exc


async def _lock_tracker_review_context(
    db: AsyncSession,
    job: VideoTrackerJob,
    *,
    actor_id: uuid.UUID | None,
    privileged: bool,
    source_ids: set[uuid.UUID],
) -> tuple[Task, dict[uuid.UUID, Annotation]]:
    task = await _lock_task_bounded(db, job.task_id)
    if task is None:
        raise TrackerJobStateConflict("task no longer exists", reason="task_changed")
    if actor_id is None:
        raise TrackerJobStateConflict(
            "an explicit actor is required for tracker final writes",
            reason="permission_changed",
        )
    effective_actor = actor_id
    if task.status == "completed":
        raise TrackerJobStateConflict(
            f"task is locked in status {task.status}", reason="task_locked"
        )
    if task.status == "review":
        if not privileged and (
            task.reviewer_id != effective_actor or task.reviewer_claimed_at is None
        ):
            raise TrackerJobStateConflict(
                "task review is not claimed by the current reviewer",
                reason="task_review_not_claimed_by_user",
            )
    elif (
        not privileged
        and task.assignee_id is not None
        and task.assignee_id != effective_actor
    ):
        raise TrackerJobStateConflict(
            "task assignment changed before review", reason="task_assignment_changed"
        )
    # After the fresh Task lock and the specific claim/assignment checks,
    # reauthorize the actual actor against current project membership, phase
    # capability and frozen review evidence (no creator fallback).
    await _assert_tracker_actor_authority(db, task, actor_id)
    if job.segment_id is not None:
        segment = await _lock_segment_bounded(db, job.segment_id)
        now = _now()
        work_start, work_end = (
            await segment_work_bounds_for_task(db, task=task, segment=segment)
            if segment is not None
            else (-1, -1)
        )
        invalid_scope = (
            segment is None
            or segment.dataset_item_id != job.dataset_item_id
            or job.from_frame < work_start
            or job.to_frame > work_end
        )
        active_other_lock = bool(
            segment
            and segment.locked_by not in (None, effective_actor)
            and segment.lock_expires_at is not None
            and segment.lock_expires_at > now
        )
        annotating_lease_invalid = bool(
            segment
            and task.status != "review"
            and not privileged
            and (
                segment.assignee_id not in (None, effective_actor)
                or segment.locked_by != effective_actor
                or segment.lock_expires_at is None
                or segment.lock_expires_at <= now
            )
        )
        if invalid_scope or active_other_lock or annotating_lease_invalid:
            raise TrackerJobStateConflict(
                "video segment lease changed before review",
                reason="segment_lease_changed",
            )
    locked_sources = await _assert_source_versions_unchanged(db, job, source_ids)
    return task, locked_sources


async def _assert_review_scopes_available(
    db: AsyncSession,
    *,
    annotation: Annotation,
    from_frame: int,
    to_frame: int,
    region_rle: dict | None = None,
    frame_indices: set[int] | None = None,
) -> None:
    scopes = list(
        (
            await db.execute(
                select(MaskReviewScope)
                .where(
                    MaskReviewScope.annotation_id == annotation.id,
                    MaskReviewScope.result_annotation_version
                    == int(annotation.version or 1),
                    MaskReviewScope.frame_start <= to_frame,
                    MaskReviewScope.frame_end >= from_frame,
                )
                .order_by(MaskReviewScope.id)
                .with_for_update()
            )
        ).scalars()
    )
    conflicts: list[dict] = []
    for scope in scopes:
        if frame_indices is not None and not any(
            scope.frame_start <= frame_index <= scope.frame_end
            for frame_index in frame_indices
        ):
            continue
        if region_rle is not None:
            try:
                protected_region = await load_coco_rle(scope.region_mask_ref)
                if coco_rle_area(rle_and(region_rle, protected_region)) == 0:
                    continue
            except (KeyError, TypeError, ValueError) as exc:
                raise TrackerJobStateConflict(
                    "reviewed Mask scope content is unavailable",
                    reason="reviewed_scope_unavailable",
                ) from exc
        conflicts.append(
            {
                "scope_id": str(scope.id),
                "frame_start": scope.frame_start,
                "frame_end": scope.frame_end,
                "region_digest": scope.region_digest,
                "decision": scope.decision,
            }
        )
    if conflicts:
        raise TrackerJobStateConflict(
            "selected candidates overlap a current reviewed Mask scope",
            reason="reviewed_scope_protected",
            conflicts=conflicts,
        )


def _region_decision_history_key(
    *, issue_id: uuid.UUID, candidate_digest: str, region_digest: str
) -> str:
    raw = f"{issue_id}:{candidate_digest}:{region_digest}".encode()
    return f"region:{hashlib.sha256(raw).hexdigest()}"


async def _decide_tracker_issue_region(
    db: AsyncSession,
    *,
    job: VideoTrackerJob,
    staged: dict,
    rows: list[dict],
    issue_id: uuid.UUID,
    candidate_digest: str,
    decision: str,
    expected_source_versions: dict[uuid.UUID, int],
    job_revision: int,
    override_manual: bool,
    actor_id: uuid.UUID | None,
    privileged: bool,
    publisher: TrackerEventPublisher,
    commit: bool,
) -> VideoTrackerJob:
    issue_snapshot = await db.get(MaskQCIssue, issue_id)
    if issue_snapshot is None:
        raise TrackerJobStateConflict(
            "Mask QC issue no longer exists", reason="mask_qc_issue_not_found"
        )
    if (
        issue_snapshot.task_id != job.task_id
        or issue_snapshot.frame_start is None
        or issue_snapshot.frame_start != issue_snapshot.frame_end
        or not isinstance(issue_snapshot.region_mask_ref, dict)
        or not issue_snapshot.region_digest
    ):
        raise TrackerJobStateConflict(
            "Mask QC issue is not a single-frame regional issue",
            reason="mask_qc_issue_region_invalid",
        )
    state = _review_state(job)
    history = dict(state.get("decisions") or {})
    history_key = _region_decision_history_key(
        issue_id=issue_snapshot.id,
        candidate_digest=candidate_digest,
        region_digest=issue_snapshot.region_digest,
    )
    previous = history.get(history_key)
    if isinstance(previous, dict):
        if previous.get("decision") == decision:
            job._review_replayed = True
            if commit:
                await db.commit()
            return job
        raise TrackerJobStateConflict(
            "QC region already has the opposite decision",
            reason="candidate_decision_conflict",
        )
    frame_index = int(issue_snapshot.frame_start)
    source_ids_by_instance = _review_source_map_ids(job, staged)
    instance_annotations = dict(state.get("instance_annotations") or {})
    selected: list[dict] = []
    for row in rows:
        if int(row.get("frame_index", -1)) != frame_index:
            continue
        if row.get("geometry_digest") != candidate_digest:
            continue
        instance_id = _tracker_instance_key(row)
        source_id = source_ids_by_instance.get(instance_id)
        target_id = instance_annotations.get(instance_id) or source_id
        if str(target_id or "") != str(issue_snapshot.annotation_id):
            continue
        selected.append(row)
    if len(selected) != 1:
        raise TrackerJobStateConflict(
            "QC issue does not resolve to one staged Mask candidate",
            reason=(
                "candidate_selection_ambiguous"
                if selected
                else "candidate_digest_conflict"
            ),
        )
    selected_row = selected[0]
    instance_id = _tracker_instance_key(selected_row)
    source_annotation_id = source_ids_by_instance.get(instance_id)
    if source_annotation_id != issue_snapshot.annotation_id:
        raise TrackerJobStateConflict(
            "regional decisions require an existing source annotation",
            reason="mask_qc_issue_source_conflict",
        )

    expected = {
        uuid.UUID(str(annotation_id)): int(version)
        for annotation_id, version in expected_source_versions.items()
    }
    expected_version = expected.get(source_annotation_id)
    prompt_versions = (job.prompt or {}).get("expected_source_versions") or {}
    if (
        expected_version is None
        or int(prompt_versions.get(str(source_annotation_id), -1)) != expected_version
    ):
        raise TrackerJobStateConflict(
            "source version snapshot differs from the current preview",
            reason="source_version_conflict",
        )

    task, locked_sources = await _lock_tracker_review_context(
        db,
        job,
        actor_id=actor_id,
        privileged=privileged,
        source_ids={source_annotation_id},
    )
    annotation = locked_sources[source_annotation_id]
    issue = (
        await db.execute(
            select(MaskQCIssue)
            .where(MaskQCIssue.id == issue_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if issue is None:
        raise TrackerJobStateConflict(
            "Mask QC issue no longer exists", reason="mask_qc_issue_not_found"
        )
    if (
        issue.task_id != job.task_id
        or issue.project_id != task.project_id
        or issue.annotation_id != annotation.id
        or issue.annotation_version != int(annotation.version or 1)
        or int(issue.source_versions.get(str(annotation.id), -1))
        != int(annotation.version or 1)
        or issue.status != "open"
        or issue.frame_start != frame_index
        or issue.frame_end != frame_index
        or issue.region_digest != issue_snapshot.region_digest
        or issue.region_mask_ref != issue_snapshot.region_mask_ref
    ):
        raise TrackerJobStateConflict(
            "Mask QC issue is stale or no longer matches the candidate source",
            reason="mask_qc_issue_stale",
        )

    reviewable = job.status in {
        VideoTrackerJobStatus.PENDING_REVIEW.value,
        VideoTrackerJobStatus.PARTIALLY_REVIEWED.value,
        VideoTrackerJobStatus.CANCELLED.value,
    }
    if not reviewable or not rows:
        raise TrackerJobStateConflict(
            f"Video tracker job cannot be reviewed from status {job.status}",
            reason="tracker_job_not_reviewable",
        )
    if int(job.revision or 1) != job_revision:
        raise TrackerJobStateConflict(
            "tracker job revision changed; refresh the preview",
            reason="job_revision_conflict",
            conflicts=[
                {
                    "expected_revision": job_revision,
                    "current_revision": int(job.revision or 1),
                }
            ],
        )
    if expected_version != int(annotation.version or 1):
        raise TrackerJobStateConflict(
            "source annotation version changed",
            reason="source_version_conflict",
        )
    if override_manual and not privileged:
        raise TrackerJobStateConflict(
            "manual keyframe override requires project privilege",
            reason="manual_override_forbidden",
        )
    if staged.get("output_geometry") != "mask" or bool(
        selected_row.get("outside", False)
    ):
        raise TrackerJobStateConflict(
            "regional decisions require a visible Mask candidate",
            reason="mask_candidate_required",
        )
    candidate_reference = (selected_row.get("geometry") or {}).get("mask")
    if not isinstance(candidate_reference, dict):
        raise TrackerJobStateConflict(
            "regional decisions require immutable candidate content",
            reason="mask_candidate_required",
        )
    geometry = annotation.geometry or {}
    if geometry.get("type") != "video_track_mask":
        raise TrackerJobStateConflict(
            "regional decisions require a video Mask source",
            reason="video_mask_track_required",
        )
    resolved = resolve_mask_track_state_at_frame(geometry, frame_index)
    current_reference = resolved.get("mask")
    if not isinstance(current_reference, dict) or not current_reference:
        raise TrackerJobStateConflict(
            "the QC issue frame has no current Mask content",
            reason="mask_qc_issue_source_conflict",
        )
    manual_conflicts: list[dict] = []
    if resolved.get("state") == "exact" and resolved.get("source") == "manual":
        manual_conflicts.append(
            {
                "annotation_id": str(annotation.id),
                "frame_index": frame_index,
                "before_digest": _geometry_sha256(
                    next(
                        item
                        for item in geometry.get("keyframes") or []
                        if int(item.get("frame_index", -1)) == frame_index
                    )
                ),
            }
        )
    if decision == "accept" and manual_conflicts and not override_manual:
        raise TrackerJobStateConflict(
            "selected candidate overlaps a protected manual keyframe",
            reason="manual_keyframe_protected",
            conflicts=manual_conflicts,
        )

    try:
        current_rle = await load_coco_rle(current_reference)
        candidate_rle = await load_coco_rle(candidate_reference)
        region_rle = await load_coco_rle(issue.region_mask_ref)
        changed_in_region = rle_and(rle_xor(current_rle, candidate_rle), region_rle)
    except (KeyError, TypeError, ValueError) as exc:
        raise TrackerJobStateConflict(
            "regional decision Mask content is invalid",
            reason="mask_region_content_invalid",
        ) from exc
    if coco_rle_area(changed_in_region) == 0:
        raise TrackerJobStateConflict(
            "candidate has no changed pixels inside the QC issue region",
            reason="mask_qc_issue_candidate_mismatch",
        )
    await _assert_review_scopes_available(
        db,
        annotation=annotation,
        from_frame=frame_index,
        to_frame=frame_index,
        region_rle=region_rle,
    )

    source_version = int(annotation.version or 1)
    after_rle = current_rle
    touched = False
    if decision == "accept":
        after_rle = rle_replace_region(current_rle, candidate_rle, region_rle)
        if coco_rle_area(after_rle) == 0:
            result = TrackerFrameResult(
                frame_index=frame_index,
                geometry={},
                confidence=selected_row.get("confidence"),
                outside=True,
                instance_id=instance_id,
                primary=bool(selected_row.get("primary", False)),
            )
        else:
            result_reference = await store_coco_rle(after_rle)
            result = TrackerFrameResult(
                frame_index=frame_index,
                geometry={
                    "type": "mask",
                    "mask": result_reference,
                    "bbox": coco_rle_bbox_norm(after_rle),
                },
                confidence=selected_row.get("confidence"),
                outside=False,
                instance_id=instance_id,
                primary=bool(selected_row.get("primary", False)),
            )
        apply_tracker_results(
            annotation,
            job,
            [result],
            1,
            "mask",
            override_manual_frames={frame_index} if override_manual else None,
        )
        await validate_mask_geometry_for_task(db, task, annotation.geometry or {})
        await lock_raster_mask_references(
            db, annotation.geometry or {}, task_id=task.id
        )
        touched = True

    residual_rle = (
        candidate_rle
        if decision == "accept"
        else rle_replace_region(candidate_rle, current_rle, region_rle)
    )
    residual_changed = coco_rle_area(rle_xor(after_rle, residual_rle)) > 0
    remaining: list[dict] = []
    for row in rows:
        if row["candidate_key"] != selected_row["candidate_key"]:
            remaining.append(row)
            continue
        if not residual_changed:
            continue
        if decision == "accept":
            remaining.append(row)
            continue
        residual_reference = await store_coco_rle(residual_rle)
        residual_geometry = {
            **(row.get("geometry") or {}),
            "mask": residual_reference,
            "bbox": coco_rle_bbox_norm(residual_rle),
        }
        updated = {**row, "geometry": residual_geometry}
        updated.pop("geometry_digest", None)
        updated.pop("candidate_key", None)
        remaining.append(_ensure_candidate_contract(updated))

    next_revision = int(job.revision or 1) + 1
    history[history_key] = {
        "decision": decision,
        "selector": "qc_issue",
        "qc_issue_id": str(issue.id),
        "instance_id": instance_id,
        "frame_index": frame_index,
        "geometry_digest": candidate_digest,
        "region_digest": issue.region_digest,
        "revision": next_revision,
    }
    manual_overrides: list[dict] = []
    if touched and manual_conflicts and override_manual:
        keyframe = next(
            item
            for item in (annotation.geometry or {}).get("keyframes") or []
            if int(item.get("frame_index", -1)) == frame_index
        )
        manual_overrides = [
            {**manual_conflicts[0], "after_digest": _geometry_sha256(keyframe)}
        ]
    state = {
        **state,
        "decisions": history,
        "last_decision": {
            "decision": decision,
            "selector": "qc_issue",
            "qc_issue_id": str(issue.id),
            "instance_ids": [instance_id],
            "from_frame": frame_index,
            "to_frame": frame_index,
            "candidate_digest": candidate_digest,
            "region_digest": issue.region_digest,
            "candidate_count": 1,
            "revision_before": job_revision,
            "revision_after": next_revision,
            "manual_overrides": manual_overrides,
        },
    }
    updated_prompt = {**(job.prompt or {}), "review_state": state}
    if touched:
        touched_ids = {
            *[
                str(value)
                for value in updated_prompt.get("touched_annotation_ids") or []
            ],
            str(annotation.id),
        }
        updated_prompt["touched_annotation_ids"] = sorted(touched_ids)
        source_versions = dict(updated_prompt.get("expected_source_versions") or {})
        source_versions[str(annotation.id)] = int(annotation.version or 1)
        updated_prompt["expected_source_versions"] = source_versions
    job.prompt = updated_prompt
    job.revision = next_revision
    job._review_replayed = False
    if remaining:
        job.status = VideoTrackerJobStatus.PARTIALLY_REVIEWED.value
        job.staged_result = {**staged, "results": remaining}
        await lock_raster_mask_references(db, job.staged_result, task_id=task.id)
    else:
        accepted_any = any(
            item.get("decision") == "accept"
            for item in history.values()
            if isinstance(item, dict)
        )
        job.status = (
            VideoTrackerJobStatus.ACCEPTED.value
            if accepted_any
            else VideoTrackerJobStatus.DISCARDED.value
        )
        job.staged_result = None

    db.add(
        MaskReviewScope(
            project_id=task.project_id,
            task_id=task.id,
            annotation_id=annotation.id,
            qc_issue_id=issue.id,
            source_job_id=job.id,
            reviewer_id=actor_id,
            source_annotation_version=source_version,
            result_annotation_version=int(annotation.version or 1),
            source_job_revision=job_revision,
            frame_start=frame_index,
            frame_end=frame_index,
            region_mask_ref=dict(issue.region_mask_ref),
            region_digest=issue.region_digest,
            candidate_digest=candidate_digest,
            decision=decision,
        )
    )
    await db.flush()
    if commit:
        await db.commit()
        await db.refresh(job)
    else:
        await db.refresh(job)
    event_type = (
        "job_partially_reviewed"
        if remaining
        else "job_accepted"
        if job.status == VideoTrackerJobStatus.ACCEPTED.value
        else "job_discarded"
    )
    if commit:
        await publisher(job.event_channel, _event(job, event_type))
    return job


async def decide_tracker_job(
    db: AsyncSession,
    job_id: uuid.UUID,
    *,
    instance_ids: list[str] | None,
    from_frame: int | None,
    to_frame: int | None,
    decision: str,
    expected_source_versions: dict[uuid.UUID, int],
    job_revision: int,
    qc_issue_id: uuid.UUID | None = None,
    candidate_digest: str | None = None,
    override_manual: bool = False,
    actor_id: uuid.UUID | None = None,
    privileged: bool = False,
    publisher: TrackerEventPublisher = publish_tracker_event,
    commit: bool = True,
) -> VideoTrackerJob | None:
    """Atomically accept/reject one explicit instance/window candidate slice."""

    # Account-first bounded lock order: actor (share) -> job -> task.  Authority
    # is rechecked before the QC-region dispatch and any idempotent replay exit.
    await _lock_actor_share(db, actor_id)
    await _lock_actor_membership(db, actor_id, job_id)
    job = await _load_job_for_update(db, job_id)
    if job is None:
        return None
    early_task = await _lock_task_bounded(db, job.task_id)
    if early_task is None:
        raise TrackerJobStateConflict("task no longer exists", reason="task_changed")
    await _assert_tracker_actor_authority(db, early_task, actor_id)
    staged = dict(job.staged_result or {})
    rows = [
        _ensure_candidate_contract(row)
        for row in (staged.get("results") or [])
        if isinstance(row, dict)
    ]
    candidate_keys = [row["candidate_key"] for row in rows]
    if len(candidate_keys) != len(set(candidate_keys)):
        raise TrackerJobStateConflict(
            "staged candidates contain duplicate candidate keys",
            reason="duplicate_candidate_key",
        )
    if qc_issue_id is not None:
        if candidate_digest is None:
            raise TrackerJobStateConflict(
                "QC issue selector requires a candidate digest",
                reason="candidate_digest_required",
            )
        return await _decide_tracker_issue_region(
            db,
            job=job,
            staged=staged,
            rows=rows,
            issue_id=qc_issue_id,
            candidate_digest=candidate_digest,
            decision=decision,
            expected_source_versions=expected_source_versions,
            job_revision=job_revision,
            override_manual=override_manual,
            actor_id=actor_id,
            privileged=privileged,
            publisher=publisher,
            commit=commit,
        )
    if instance_ids is None or from_frame is None or to_frame is None:
        raise TrackerJobStateConflict(
            "instance/window decision selector is incomplete",
            reason="candidate_selector_invalid",
        )
    if from_frame < job.from_frame or to_frame > job.to_frame or from_frame > to_frame:
        raise TrackerJobStateConflict(
            "candidate selector is outside the tracker job window",
            reason="invalid_candidate_window",
        )
    wanted_instances = set(instance_ids)
    selected = [
        row
        for row in rows
        if _tracker_instance_key(row) in wanted_instances
        and from_frame <= int(row["frame_index"]) <= to_frame
    ]
    state = _review_state(job)
    history = dict(state.get("decisions") or {})
    matching_history = [
        item
        for item in history.values()
        if isinstance(item, dict)
        and str(item.get("instance_id")) in wanted_instances
        and from_frame <= int(item.get("frame_index", -1)) <= to_frame
    ]
    if not selected and matching_history:
        if all(item.get("decision") == decision for item in matching_history):
            job._review_replayed = True
            if commit:
                await db.commit()
            return job
        raise TrackerJobStateConflict(
            "selected candidates already have the opposite decision",
            reason="candidate_decision_conflict",
        )
    if matching_history and any(
        item.get("decision") != decision for item in matching_history
    ):
        raise TrackerJobStateConflict(
            "selector overlaps candidates with the opposite decision",
            reason="candidate_decision_conflict",
        )
    reviewable = job.status in {
        VideoTrackerJobStatus.PENDING_REVIEW.value,
        VideoTrackerJobStatus.PARTIALLY_REVIEWED.value,
        VideoTrackerJobStatus.CANCELLED.value,
    }
    if not reviewable or not rows:
        raise TrackerJobStateConflict(
            f"Video tracker job cannot be reviewed from status {job.status}",
            reason="tracker_job_not_reviewable",
        )
    if int(job.revision or 1) != job_revision:
        raise TrackerJobStateConflict(
            "tracker job revision changed; refresh the preview",
            reason="job_revision_conflict",
            conflicts=[
                {
                    "expected_revision": job_revision,
                    "current_revision": int(job.revision or 1),
                }
            ],
        )
    if override_manual and not privileged:
        raise TrackerJobStateConflict(
            "manual keyframe override requires project privilege",
            reason="manual_override_forbidden",
        )
    if not selected:
        raise TrackerJobStateConflict(
            "selector does not match any undecided candidate",
            reason="empty_candidate_selection",
        )

    source_ids_by_instance = _review_source_map_ids(job, staged)
    if (
        job.annotation_id is not None
        and not _seed_source_map(job)
        and not staged.get("source_instance_id")
    ):
        source_instance = next(
            (
                instance_id
                for instance_id, annotation_id in source_ids_by_instance.items()
                if annotation_id == job.annotation_id
            ),
            None,
        )
        if source_instance is not None:
            # Keep the inferred legacy mapping after a partial decision removes
            # the primary instance from the staged rows.
            staged["source_instance_id"] = source_instance
    selected_source_ids = {
        source_ids_by_instance[instance_id]
        for instance_id in wanted_instances
        if instance_id in source_ids_by_instance
    }
    expected = {
        uuid.UUID(str(annotation_id)): int(version)
        for annotation_id, version in expected_source_versions.items()
    }
    expected_for_selected = {
        annotation_id: expected.get(annotation_id)
        for annotation_id in selected_source_ids
    }
    if any(version is None for version in expected_for_selected.values()):
        raise TrackerJobStateConflict(
            "expected_source_versions does not cover selected sources",
            reason="source_version_conflict",
        )
    prompt_versions = (job.prompt or {}).get("expected_source_versions") or {}
    if any(
        int(prompt_versions.get(str(annotation_id), -1)) != int(version)
        for annotation_id, version in expected_for_selected.items()
        if version is not None
    ):
        raise TrackerJobStateConflict(
            "source version snapshot differs from the current preview",
            reason="source_version_conflict",
        )

    task, locked_sources = await _lock_tracker_review_context(
        db,
        job,
        actor_id=actor_id,
        privileged=privileged,
        source_ids=selected_source_ids,
    )
    source_map = {
        instance_id: locked_sources[annotation_id]
        for instance_id, annotation_id in source_ids_by_instance.items()
        if instance_id in wanted_instances and annotation_id in locked_sources
    }
    manual_conflicts: list[dict] = []
    output_geometry = str(staged.get("output_geometry", "bbox"))
    expected_geometry_type = {
        "bbox": "video_track_bbox",
        "polygon": "video_track_polygon",
        "mask": "video_track_mask",
    }.get(output_geometry, "video_track_bbox")
    for instance_id, source in source_map.items():
        source_type = (source.geometry or {}).get("type")
        if (
            source_type
            in {
                "video_track_bbox",
                "video_track_polygon",
                "video_track_mask",
            }
            and source_type != expected_geometry_type
        ):
            raise TrackerJobStateConflict(
                "local review cannot change an existing track geometry type",
                reason="tracker_geometry_type_conflict",
                conflicts=[
                    {
                        "annotation_id": str(source.id),
                        "source_type": source_type,
                        "candidate_type": expected_geometry_type,
                    }
                ],
            )
        selected_frames = {
            int(row["frame_index"])
            for row in selected
            if _tracker_instance_key(row) == instance_id
        }
        for keyframe in (source.geometry or {}).get("keyframes") or []:
            frame_index = int(keyframe.get("frame_index", -1))
            if (
                keyframe.get("source", "manual") == "manual"
                and frame_index in selected_frames
            ):
                manual_conflicts.append(
                    {
                        "annotation_id": str(source.id),
                        "frame_index": frame_index,
                        "before_digest": _geometry_sha256(keyframe),
                    }
                )
    if decision == "accept" and manual_conflicts and not override_manual:
        raise TrackerJobStateConflict(
            "selected candidates overlap protected manual keyframes",
            reason="manual_keyframe_protected",
            conflicts=manual_conflicts,
        )
    for instance_id, source in source_map.items():
        selected_frames = [
            int(row["frame_index"])
            for row in selected
            if _tracker_instance_key(row) == instance_id
        ]
        if selected_frames:
            await _assert_review_scopes_available(
                db,
                annotation=source,
                from_frame=min(selected_frames),
                to_frame=max(selected_frames),
                frame_indices=set(selected_frames),
            )

    touched: list[Annotation] = []
    instance_annotations = dict(state.get("instance_annotations") or {})
    deferred_results = {
        str(instance_id): [dict(row) for row in instance_rows if isinstance(row, dict)]
        for instance_id, instance_rows in (staged.get("deferred_results") or {}).items()
        if isinstance(instance_rows, list)
    }
    if decision == "accept":
        grid_step = int(staged.get("grid_step", 1))
        by_instance: dict[str, list[TrackerFrameResult]] = defaultdict(list)
        for row in selected:
            by_instance[_tracker_instance_key(row)].extend(_deserialize_results([row]))
        target = (
            _target_from_source(next(iter(source_map.values())))
            if source_map
            else _target_from_job(job, task)
        )
        for instance_id in sorted(by_instance):
            annotation = source_map.get(instance_id)
            discovered = annotation is None
            if discovered:
                annotation = _new_discovered_track(target, output_geometry)
            instance_results = [
                *_deserialize_results(deferred_results.pop(instance_id, [])),
                *by_instance[instance_id],
            ]
            frames = {result.frame_index for result in instance_results}
            apply_tracker_results(
                annotation,
                job,
                instance_results,
                grid_step,
                output_geometry,
                override_manual_frames=frames if override_manual else None,
            )
            if discovered:
                if not _discovered_track_has_keyframes(annotation):
                    deferred_results[instance_id] = _serialize_results(instance_results)
                    continue
                db.add(annotation)
                instance_annotations[instance_id] = str(annotation.id)
            touched.append(annotation)
        try:
            for annotation in touched:
                await validate_mask_geometry_for_task(
                    db, task, annotation.geometry or {}
                )
            await lock_raster_mask_references(
                db,
                [annotation.geometry or {} for annotation in touched],
                task_id=task.id,
            )
        except ValueError:
            await db.rollback()
            raise

    next_revision = int(job.revision or 1) + 1
    job._review_replayed = False
    for row in selected:
        history[row["candidate_key"]] = {
            "decision": decision,
            "instance_id": _tracker_instance_key(row),
            "frame_index": int(row["frame_index"]),
            "geometry_digest": row["geometry_digest"],
            "revision": next_revision,
        }
    selected_keys = {row["candidate_key"] for row in selected}
    remaining = [row for row in rows if row["candidate_key"] not in selected_keys]
    manual_overrides: list[dict] = []
    for conflict in manual_conflicts:
        if not override_manual:
            continue
        source = next(
            item for item in touched if str(item.id) == conflict["annotation_id"]
        )
        keyframe = next(
            (
                item
                for item in (source.geometry or {}).get("keyframes") or []
                if int(item.get("frame_index", -1)) == conflict["frame_index"]
            ),
            {},
        )
        manual_overrides.append(
            {**conflict, "after_digest": _geometry_sha256(keyframe)}
        )
    state = {
        **state,
        "decisions": history,
        "instance_annotations": instance_annotations,
        "last_decision": {
            "decision": decision,
            "instance_ids": sorted(wanted_instances),
            "from_frame": from_frame,
            "to_frame": to_frame,
            "candidate_count": len(selected),
            "revision_before": job_revision,
            "revision_after": next_revision,
            "manual_overrides": manual_overrides,
        },
    }
    updated_prompt = {**(job.prompt or {}), "review_state": state}
    if touched:
        touched_ids = {
            *[
                str(value)
                for value in updated_prompt.get("touched_annotation_ids") or []
            ],
            *[str(annotation.id) for annotation in touched],
        }
        updated_prompt["touched_annotation_ids"] = sorted(touched_ids)
        source_versions = dict(updated_prompt.get("expected_source_versions") or {})
        for annotation in touched:
            source_versions[str(annotation.id)] = int(annotation.version)
        updated_prompt["expected_source_versions"] = source_versions
    job.prompt = updated_prompt
    job.revision = next_revision
    if remaining:
        job.status = VideoTrackerJobStatus.PARTIALLY_REVIEWED.value
        job.staged_result = {
            **staged,
            "results": remaining,
            "deferred_results": deferred_results,
        }
    else:
        accepted_any = any(
            item.get("decision") == "accept"
            for item in history.values()
            if isinstance(item, dict)
        )
        job.status = (
            VideoTrackerJobStatus.ACCEPTED.value
            if accepted_any
            else VideoTrackerJobStatus.DISCARDED.value
        )
        job.staged_result = None
    await db.flush()
    if commit:
        await db.commit()
        await db.refresh(job)
    else:
        # Server/on-update columns are expired after flush; load them while the
        # async greenlet is active so the API can serialize before its audit commit.
        await db.refresh(job)
    event_type = (
        "job_partially_reviewed"
        if remaining
        else "job_accepted"
        if job.status == VideoTrackerJobStatus.ACCEPTED.value
        else "job_discarded"
    )
    if commit:
        await publisher(job.event_channel, _event(job, event_type))
    return job


async def accept_tracker_job(
    db: AsyncSession,
    job_id: uuid.UUID,
    *,
    publisher: TrackerEventPublisher = publish_tracker_event,
    actor_id: uuid.UUID | None = None,
    privileged: bool = False,
) -> VideoTrackerJob | None:
    """接受候选: 把 job.staged_result 应用到 annotation (主实例回填源 + 每个新 instance 各建
    一条 track), status=ACCEPTED。幂等 (已 ACCEPTED 直接返回)。状态不符 / 无 staged → 原样返回。"""
    # Account-first bounded lock order: actor (share) -> job -> task.  Authority
    # is rechecked before ANY replay/status early return so an idempotent replay
    # cannot bypass current project/phase authority.
    await _lock_actor_share(db, actor_id)
    await _lock_actor_membership(db, actor_id, job_id)
    job = await _load_job_for_update(db, job_id)
    if job is None:
        return None
    task = await _lock_task_bounded(db, job.task_id)
    if task is None:
        raise ValueError("Task not found")
    await _assert_tracker_actor_authority(db, task, actor_id)
    if job.status == VideoTrackerJobStatus.ACCEPTED.value:
        await db.commit()
        return job
    staged = job.staged_result or {}
    rows = staged.get("results") or []
    reviewable = job.status in (
        VideoTrackerJobStatus.PENDING_REVIEW.value,
        VideoTrackerJobStatus.CANCELLED.value,
    )
    if not reviewable or not rows:
        current_status = job.status
        await db.rollback()
        raise TrackerJobStateConflict(
            f"Video tracker job cannot be accepted from status {current_status}"
        )
    # v0.22.1 · B · 源轨迹可选 (无源检测 → 全新建); v0.22.2 · M · 多选批量: prompt.seeds
    # 每条可带 source_annotation_id (obj_id ↔ 源轨迹), 各实例回填各自源。
    results = _deserialize_results(rows)
    # Explicit actor only; never impersonate the job creator.
    effective_actor = actor_id
    if task.status in {"review", "completed"}:
        raise TrackerJobStateConflict(f"task is locked in status {task.status}")
    if (
        not privileged
        and task.assignee_id is not None
        and task.assignee_id != effective_actor
    ):
        raise TrackerJobStateConflict("task assignment changed before accept")
    if job.segment_id is not None:
        segment = await _lock_segment_bounded(db, job.segment_id)
        now = _now()
        work_start, work_end = (
            await segment_work_bounds_for_task(db, task=task, segment=segment)
            if segment is not None
            else (-1, -1)
        )
        if (
            segment is None
            or segment.dataset_item_id != job.dataset_item_id
            or job.from_frame < work_start
            or job.to_frame > work_end
            or (
                not privileged
                and (
                    segment.assignee_id not in (None, effective_actor)
                    or segment.locked_by != effective_actor
                    or segment.lock_expires_at is None
                    or segment.lock_expires_at <= now
                )
            )
        ):
            raise TrackerJobStateConflict("video segment lease changed before accept")
    seed_sources = _seed_source_map(job)
    source_ids = set(seed_sources.values())
    if not source_ids and job.annotation_id is not None:
        source_ids.add(job.annotation_id)
    locked_sources = await _assert_source_versions_unchanged(db, job, source_ids)
    source_map: dict[str, Annotation] = {}
    if seed_sources:
        for instance_id, ann_id in seed_sources.items():
            source_map[instance_id] = locked_sources[ann_id]
        target = (
            _target_from_source(next(iter(source_map.values())))
            if source_map
            else _target_from_job(job, task)
        )
    elif job.annotation_id is not None:
        # 单源延展 (0.22.1 及以前): 主实例回填源; 整源软删 → 丢弃孤儿候选 (409 告知前端)。
        annotation = locked_sources[job.annotation_id]
        primary_iid = _primary_instance_id(results)
        source_map = {
            primary_iid if primary_iid is not None else _SOLE_SOURCE_KEY: annotation
        }
        target = _target_from_source(annotation)
    else:
        # 无源检测 (画布级文本/种子发起): 全部新建, 归属取 job 显式目标类别。
        target = _target_from_job(job, task)
    # v0.23.5 · WS-D · D4 · refuse accept if any source annotation was mutated
    # between job creation and accept. Runs before _persist_tracker_results so
    # a conflict leaves annotations untouched (no last-writer-wins).
    created = _persist_tracker_results(
        db,
        source_map,
        target,
        job,
        results,
        int(staged.get("grid_step", 1)),
        staged.get("output_geometry", "bbox"),
    )
    try:
        for src in source_map.values():
            await validate_mask_geometry_for_task(db, task, src.geometry or {})
        for created_annotation in created:
            await validate_mask_geometry_for_task(
                db, task, created_annotation.geometry or {}
            )
        await lock_raster_mask_references(
            db,
            [
                *(src.geometry or {} for src in source_map.values()),
                *(annotation.geometry or {} for annotation in created),
            ],
            task_id=task.id,
        )
    except ValueError:
        await db.rollback()
        raise
    # A2 · accept holds source/Annotation locks, so the task rows are acquired
    # with NOWAIT: a busy task rolls back to a retryable 409 instead of a cycle.
    await record_annotation_actors_for_tasks(
        db,
        {task.id, *(source.task_id for source in source_map.values())},
        effective_actor,
        nowait=True,
    )
    # v0.22.2 · M · 记录本 job 触及的轨迹 id (回填源 + 新建) 供前端刷新/审计。落 job.prompt JSONB
    # (免 DB 迁移); accept 后 job 终态, prompt 不再被 runner 读, 写此键安全。JSONB 须重赋新 dict
    # 才能让 SQLAlchemy 检测到脏 (同一 dict 引用 in-place 改不触发 UPDATE)。
    touched = [str(src.id) for src in source_map.values()] + [
        str(created_annotation.id) for created_annotation in created
    ]
    job.prompt = {**(job.prompt or {}), "touched_annotation_ids": touched}
    job.status = VideoTrackerJobStatus.ACCEPTED.value
    # Accepted refs are now reachable from annotations; the candidate copy no
    # longer needs a grace period. Discard/failure likewise clear immediately.
    job.staged_result = None
    await db.commit()
    await db.refresh(job)
    await publisher(job.event_channel, _event(job, "job_accepted"))
    return job


async def discard_tracker_job(
    db: AsyncSession,
    job_id: uuid.UUID,
    *,
    publisher: TrackerEventPublisher = publish_tracker_event,
) -> VideoTrackerJob | None:
    """丢弃可审候选并保持 annotation 零改动；重复丢弃幂等。"""
    job = await _load_job_for_update(db, job_id)
    if job is None:
        return None
    if job.status == VideoTrackerJobStatus.DISCARDED.value:
        await db.commit()
        return job
    staged_rows = (job.staged_result or {}).get("results") or []
    reviewable = job.status in (
        VideoTrackerJobStatus.PENDING_REVIEW.value,
        VideoTrackerJobStatus.CANCELLED.value,
    )
    if not reviewable or not staged_rows:
        current_status = job.status
        await db.rollback()
        raise TrackerJobStateConflict(
            f"Video tracker job cannot be discarded from status {current_status}"
        )
    job.status = VideoTrackerJobStatus.DISCARDED.value
    job.staged_result = None
    await db.commit()
    await db.refresh(job)
    await publisher(job.event_channel, _event(job, "job_discarded"))
    return job


async def _load_job_for_update(
    db: AsyncSession, job_id: uuid.UUID
) -> VideoTrackerJob | None:
    from sqlalchemy.exc import DBAPIError

    try:
        return (
            await db.execute(
                select(VideoTrackerJob)
                .where(VideoTrackerJob.id == job_id)
                .with_for_update(nowait=True)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
    except DBAPIError as exc:
        if _busy_lock_error(exc):
            raise TrackerJobStateConflict(
                "tracker job is busy", reason="tracker_job_busy"
            ) from exc
        raise


async def _mark_failed(
    db: AsyncSession,
    job_id: uuid.UUID,
    message: str,
    publisher: TrackerEventPublisher,
    *,
    gpu_arbiter_error: dict | None = None,
) -> VideoTrackerJob | None:
    await db.rollback()
    job = await _load_job_for_update(db, job_id)
    if job is None:
        return None
    if job.status != VideoTrackerJobStatus.CANCELLED.value:
        job.status = VideoTrackerJobStatus.FAILED.value
        job.error_message = message[:2000]
        job.staged_result = None
        job.completed_at = _now()
    await db.commit()
    event = _event(job, "job_failed", error=message)
    if gpu_arbiter_error is not None:
        event["gpu_arbiter_error"] = gpu_arbiter_error
    await publisher(job.event_channel, event)
    return job


async def run_tracker_job(
    db: AsyncSession,
    job_id: uuid.UUID,
    *,
    publisher: TrackerEventPublisher = publish_tracker_event,
    shadow_session_factory: GPUShadowSessionFactory | None = None,
    dispatch_context_factory: GPUDispatchContextFactory | None = None,
    failure_recorder: Callable[[dict], None] | None = None,
) -> VideoTrackerJob | None:
    job = await _load_job_for_update(db, job_id)
    if job is None:
        return None
    if job.status == VideoTrackerJobStatus.CANCELLED.value:
        await db.commit()
        return job
    if job.status != VideoTrackerJobStatus.QUEUED.value:
        await db.commit()
        return job

    job.status = VideoTrackerJobStatus.RUNNING.value
    job.started_at = job.started_at or _now()
    await db.commit()
    await db.refresh(job)
    await publisher(job.event_channel, _event(job, "job_started"))

    tracker_ledger = None
    tracker_lease = None
    tracker_router = None
    heartbeat_task: asyncio.Task | None = None
    heartbeat_failed = False
    route_succeeded = False
    tracker_session_cleanup: Callable[[], Awaitable[None]] | None = None
    try:
        # v0.22.1 · B · 源轨迹可选: 无源检测 (job.annotation_id is None) 时不加载 source,
        # 种子来自 prompt (text/seeds), 主实例落库时新建。
        annotation = (
            await db.get(Annotation, job.annotation_id) if job.annotation_id else None
        )
        if job.annotation_id and (annotation is None or not annotation.is_active):
            raise ValueError("Annotation not found")
        task = await db.get(Task, job.task_id)
        if task is None:
            raise ValueError("Task not found")
        item = await db.get(DatasetItem, job.dataset_item_id)
        if item is None:
            raise ValueError("Dataset item not found")
        is_correction = job.job_kind == VideoTrackerJobKind.CORRECTION.value
        correction_seeds = (
            await _correction_seed(db, job, annotation)
            if is_correction and annotation is not None
            else None
        )
        # v0.22.2 · B-combo · sam3_video_combo = multiplex 发现 → PVS 追踪 两趟编排。
        # 两趟都在 sam3-backend (声明 sam3_video + sam3_video_interactive); 后端按 PVS 能力
        # 解析 (同一 backend), 追踪趟 adapter 用 PVS, 发现趟另取 multiplex adapter (见下)。
        is_combo = job.model_key == "sam3_video_combo"
        # v0.21.25 (阶段 R) · 按 tracker 能力选后端而非项目单一绑定: sam3_video 挑声明了
        # sam3_video 的 backend(sam3-backend), 而非静默落到项目绑定的 grounded-sam2。
        tracker_capability = "sam3_video_interactive" if is_combo else job.model_key
        ml_svc = MLBackendService(db)
        frozen_routing = (
            (((job.prompt or {}).get("correction") or {}).get("routing") or {})
            if is_correction
            else {}
        )
        if is_correction:
            try:
                requested_backend_id = uuid.UUID(
                    str(frozen_routing["requested_backend_id"])
                )
                tracker_pool_id = uuid.UUID(str(frozen_routing["backend_pool_id"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("correction_routing_snapshot_invalid") from exc
            backend = await ml_svc.get(requested_backend_id)
            if backend is None:
                raise ValueError("correction_backend_missing")
            current_pool_id = await ml_svc.pool_id_for_registry(backend.id)
            if current_pool_id != tracker_pool_id:
                raise ValueError("correction_backend_pool_changed")
        else:
            backend = await ml_svc.get_tracker_backend_for_capabilities(
                task.project_id,
                ["sam3_video", "sam3_video_interactive"]
                if is_combo
                else [tracker_capability],
            )
            tracker_pool_id = (
                await ml_svc.pool_id_for_registry(backend.id)
                if backend is not None
                else None
            )
        # v0.23.3 ADR-0050 §11 · tracker pins one instance per job (stateful session).
        # 经 MLBackendRouter 取得 job-scope route lease (enforce) 或记录 would-select
        # 证据 (observe); off 模式行为不变 (legacy instance dispatch)。lease 在整个 job
        # 期间按 heartbeat_interval 周期续命, job 结束时 finish/cancel exactly once。
        from app.services.ml_routing.router import (
            MLBackendRouter,
            make_ledger_from_settings,
        )

        adapter = get_tracker_adapter(tracker_capability)

        # 采样网格步长：只回填网格帧（见 apply_tracker_results）。
        project = await db.get(Project, task.project_id)
        source_fps = ((item.metadata_ or {}).get("video") or {}).get("fps")
        grid_step = derive_step(
            source_fps, (project.video_sampling or {}) if project else {}
        )

        results: list[TrackerFrameResult] = []
        staged_bytes = 0
        total = max(
            1,
            job.to_frame - job.from_frame + 1 - (1 if is_correction else 0),
        )
        progress = 0
        # v0.23.3 ADR-0050 §11 · 取得 job-scope route lease (enforce) / 记录 would-select
        # (observe)。tracker 整 job pin 一个实例, lease 周期 heartbeat 续命 (不能中途换实例)。
        if backend is not None and tracker_pool_id is not None:
            try:
                tracker_ledger = (
                    make_ledger_from_settings()
                    if RouterMode(settings.ml_backend_router_mode) != RouterMode.OFF
                    else None
                )
            except Exception:  # noqa: BLE001 — enforce will reject without a ledger
                tracker_ledger = None
            tracker_router = MLBackendRouter(db, ledger=tracker_ledger)
            _sel = await tracker_router.acquire(
                tracker_pool_id,
                owner=f"tracker:{job_id}",
                operation="tracker",
                project_id=task.project_id,
            )
            if _sel.rejection is not None or _sel.instance_id is None:
                rejection = _sel.rejection or RejectionReason.POOL_UNAVAILABLE
                raise RoutingError(
                    rejection,
                    f"tracker route acquire rejected: {rejection.value}",
                )
            selected_backend = await ml_svc.get(_sel.instance_id)
            if selected_backend is None:
                raise RoutingError(
                    RejectionReason.POOL_UNAVAILABLE,
                    f"tracker selected missing registry instance {_sel.instance_id}",
                )
            backend = selected_backend
            if _sel.instance_id is not None:
                # off/observe: instance = legacy (unchanged). enforce: router-selected.
                # tracker pins the acquired instance for the whole job (stateful session).
                if _sel.lease is not None:
                    tracker_lease = _sel.lease

                    # heartbeat loop: renew lease periodically until job ends.
                    async def _heartbeat_loop():
                        nonlocal heartbeat_failed
                        while True:
                            await asyncio.sleep(
                                settings.ml_backend_router_heartbeat_interval_seconds
                            )
                            if tracker_lease is None:
                                return
                            try:
                                ok = await tracker_router.heartbeat(tracker_lease)
                                if not ok:
                                    heartbeat_failed = True
                                    return
                            except Exception:  # noqa: BLE001
                                heartbeat_failed = True
                                return

                    heartbeat_task = asyncio.create_task(_heartbeat_loop())

                # observe: record would-select evidence (non-gating).
                if _sel.would_select is not None:
                    log.info(
                        "tracker job %s observe would_select=%s actual=%s pool=%s",
                        job_id,
                        _sel.would_select,
                        _sel.instance_id,
                        tracker_pool_id,
                    )
            # v0.23.3 · pool/instance dual-ID recorded via structured log (audit lineage, §5.4).
            log.info(
                "tracker job %s routed pool=%s instance=%s mode=%s",
                job_id,
                tracker_pool_id,
                backend.id,
                settings.ml_backend_router_mode,
            )
            if is_correction:
                correction = dict((job.prompt or {}).get("correction") or {})
                routing = dict(correction.get("routing") or {})
                routing["execution_backend_id"] = str(backend.id)
                correction["routing"] = routing
                job.prompt = {**(job.prompt or {}), "correction": correction}
                await db.commit()
                await db.refresh(job)
        task_data = {
            "id": str(task.id),
            "file_path": resolve_task_url(task),
            "dataset_item_id": str(item.id),
            "file_name": item.file_name,
            "file_type": item.file_type,
        }

        # Cross-window continuation: window 1 seeds from the original keyframe,
        # each subsequent window seeds from the previous window's last
        # non-outside frame geometry so the tracker keeps following a moving
        # target instead of restarting from the original box every window.
        source_geometry = (annotation.geometry or {}) if annotation else {}
        last_geometry = (
            await _mask_track_seed_geometry(source_geometry, job.from_frame)
            if is_mask_track(source_geometry)
            else source_geometry
        )
        # v0.21.27 · U-pvs-1 · 多目标跨窗续追: 记每个实例的末帧 (非 outside) 几何, 后续窗对
        # 每个实例各用其上一窗末帧几何 + 同一 obj_id 重播种 (否则非主实例过一窗即丢)。仅当
        # 有多实例 (backend 发了 ≥2 个 instance_id) 时启用; 单目标 / None-instance 不触发。
        last_geom_by_instance: dict[str, dict] = {}
        # v0.21.28 · B-mx · text-multiplex (sam3_video) 每窗独立会话重检测, 窗内 obj_id 局部;
        # 平台在窗边界帧按 IoU 关联把窗内 id remap 成跨窗稳定的全局 instance_id (未匹配 → 新
        # track)。mp_prev_boundary = 上一窗每全局实例的末帧几何; mp_next_global = 新全局 id 计数。
        associate_multiplex = job.model_key == "sam3_video"
        mp_prev_boundary: dict[str, dict] = {}
        mp_next_global = [1]
        # v0.21.20 · polygon track: 让 backend 逐帧保留 mask 矢量化的多边形而非降 bbox。
        output_geometry = (job.prompt or {}).get("output_geometry") or (
            (
                "mask"
                if is_mask_track(annotation.geometry or {})
                else "polygon"
                if is_polygon_track(annotation.geometry or {})
                else "bbox"
            )
            if annotation
            else "bbox"
        )

        execution_windows = (
            _correction_execution_windows(job)
            if is_correction
            else [
                (start, end, job.direction, index == 0)
                for index, (start, end) in enumerate(_tracker_windows(job))
            ]
        )
        correction = (job.prompt or {}).get("correction") or {}
        if is_correction and correction.get("seed_mode") == "native_mask":
            expected_directions = (
                {"backward", "forward"}
                if job.direction == "bidirectional"
                else {job.direction}
            )
            actual_directions = {window[2] for window in execution_windows}
            if (
                len(execution_windows) != len(expected_directions)
                or actual_directions != expected_directions
                or any(not window[3] for window in execution_windows)
            ):
                raise ValueError("native_correction_must_fit_one_window_per_direction")
        # v0.21.27 · U-pvs-1 · PVS 点/多目标种子 (画布点 → PVS track) 从 prompt JSONB 读出。
        # 只在种子窗 (首窗, 含原始种子帧) 下发: points 锚在种子帧, 后续窗靠 last_geometry
        # (上一窗末帧框) 续追, 不重发点种子。缺省无 seeds 时行为与 B-pvs 框种子完全一致。
        prompt_seeds = correction_seeds or (job.prompt or {}).get("seeds")

        session_mode, max_context_frames = _tracker_session_capability(
            backend, tracker_capability
        )
        session_spans = (
            _context_spans(execution_windows, max_context_frames)
            if session_mode == "session" and not is_combo
            else {}
        )
        active_session: TrackerSession | None = None
        active_session_span: tuple[int, int, str] | None = None
        context_boundaries: list[int] = []

        async def close_active_session() -> None:
            nonlocal active_session, active_session_span
            if active_session is None or active_session_span is None:
                return
            close_ctx = TrackerContext(
                job_id=job.id,
                task_id=task.id,
                project_id=task.project_id,
                dataset_item_id=job.dataset_item_id,
                annotation_id=job.annotation_id,
                from_frame=job.from_frame,
                to_frame=job.to_frame,
                direction=active_session_span[2],
                prompt=job.prompt or {},
                source_geometry=last_geometry,
                task_data=task_data,
                ml_backend=backend,
                shadow_session_factory=shadow_session_factory,
                dispatch_context_factory=dispatch_context_factory,
                sam_variant=(job.prompt or {}).get("sam_variant"),
                session=replace(active_session, action="close"),
            )
            async for _ in adapter.propagate(close_ctx):
                pass
            active_session = None
            active_session_span = None

        tracker_session_cleanup = close_active_session

        # v0.22.2 · B-combo · 发现趟 (先于追踪窗循环, 串行 → 中间 idle 可卸载 multiplex 再载
        # PVS, 避两模型同容峰值)。multiplex 在种子帧按 text 检测 → per-obj 框 → 铸成 PVS 种子
        # (无源, 落库全新建)。发现不到目标即失败 (无种子无法追踪)。
        if is_combo:
            discovery_text = (job.prompt or {}).get("text")
            if not discovery_text:
                raise ValueError("sam3_video_combo requires prompt.text for discovery")
            # 发现窗从种子帧向后铺 COMBO_DISCOVERY_WINDOW_FRAMES 帧 (受 job 范围与 backend
            # 窗上限约束); multiplex 传播这几帧后在种子帧填充 obj_id, 只取种子帧的框铸种。
            disc_span = min(
                COMBO_DISCOVERY_WINDOW_FRAMES,
                max(1, int(settings.video_tracker_sam3_window_size_frames)),
            )
            disc_to = min(job.to_frame, job.from_frame + disc_span - 1)
            discovery_ctx = TrackerContext(
                job_id=job.id,
                task_id=task.id,
                project_id=task.project_id,
                dataset_item_id=job.dataset_item_id,
                annotation_id=job.annotation_id,
                from_frame=job.from_frame,
                to_frame=disc_to,
                direction="forward",
                prompt=job.prompt or {},
                source_geometry={},
                task_data=task_data,
                ml_backend=backend,
                shadow_session_factory=shadow_session_factory,
                dispatch_context_factory=dispatch_context_factory,
                text=discovery_text,
                exemplars=(job.prompt or {}).get("exemplars"),
                output_geometry=output_geometry,
            )
            discovery_adapter = get_tracker_adapter("sam3_video")
            discovery_results: list[TrackerFrameResult] = []
            async for r in discovery_adapter.propagate(discovery_ctx):
                discovery_results.append(await _materialize_tracker_mask_result(r))
            prompt_seeds = _combo_seeds_from_discovery(
                discovery_results, seed_frame=job.from_frame
            )
            if not prompt_seeds:
                raise ValueError("sam3_video_combo discovery found no objects")

        for win_idx, (
            from_frame,
            to_frame,
            window_direction,
            is_seed_window,
        ) in enumerate(execution_windows):
            span = session_spans.get(win_idx)
            span_key = (*span, window_direction) if span is not None else None
            if active_session is not None and span_key != active_session_span:
                await close_active_session()
            if span_key is not None and active_session is None:
                active_session = TrackerSession(
                    span_start_frame=span[0],
                    span_end_frame=span[1],
                )
                active_session_span = span_key
                if win_idx:
                    context_boundaries.append(
                        from_frame if window_direction == "forward" else to_frame
                    )
            # 种子窗下发原始点/框种子; 后续窗若多实例则各自续种 (见 _continuation_seeds),
            # 单实例则靠 source_geometry=last_geometry 兜底 (零回归)。
            if is_correction and is_seed_window:
                last_geometry = source_geometry
                last_geom_by_instance.clear()
                mp_prev_boundary.clear()
                mp_next_global[0] = 1
                window_seeds = correction_seeds
                if window_seeds and correction.get("seed_mode") == "native_mask":
                    window_seeds = [
                        {
                            **seed,
                            "prompts": [
                                {
                                    **prompt,
                                    "direction": window_direction,
                                }
                                for prompt in seed.get("prompts") or []
                            ],
                        }
                        for seed in window_seeds
                    ]
            elif not is_correction and win_idx == 0:
                window_seeds = prompt_seeds or None
            elif len(last_geom_by_instance) > 1:
                window_seeds = _continuation_seeds(last_geom_by_instance)
            else:
                window_seeds = None
            ctx = TrackerContext(
                job_id=job.id,
                task_id=task.id,
                project_id=task.project_id,
                dataset_item_id=job.dataset_item_id,
                annotation_id=job.annotation_id,
                from_frame=from_frame,
                to_frame=to_frame,
                direction=window_direction,
                prompt=job.prompt or {},
                source_geometry=last_geometry,
                task_data=task_data,
                ml_backend=backend,
                shadow_session_factory=shadow_session_factory,
                dispatch_context_factory=dispatch_context_factory,
                sam_variant=(job.prompt or {}).get("sam_variant"),  # v0.10.36
                # v0.21.19 · text-driven 追踪的 text/exemplars 从 prompt JSONB 读出透传。
                text=(job.prompt or {}).get("text"),
                exemplars=(job.prompt or {}).get("exemplars"),
                # v0.21.20 · polygon track 回填: 期望输出几何 (polygon/bbox)。
                output_geometry=output_geometry,
                # v0.21.27 · U-pvs-1 · 种子窗原始种子 / 后续窗多实例续种 (见上)。
                seeds=window_seeds,
                session=active_session,
            )
            # v0.21.28 · B-mx · 逐窗缓冲结果; 窗末对 text-multiplex 做跨窗 IoU 关联 (remap
            # 窗内 obj_id → 全局 instance_id) 后再并入 results。非 multiplex 时缓冲即透传。
            window_results: list[TrackerFrameResult] = []
            async for result in adapter.propagate(ctx):
                if heartbeat_failed:
                    raise RuntimeError("tracker route lease heartbeat failed")
                if is_correction and result.frame_index == job.correction_frame:
                    continue
                result = await _materialize_tracker_mask_result(result)
                staged_bytes += _tracker_result_json_bytes(result)
                if staged_bytes > MAX_TRACKER_STAGED_BYTES:
                    raise ValueError(
                        "tracker_candidate_too_large: staged payload exceeds 64 MiB"
                    )
                await db.refresh(job)
                if (
                    job.cancel_requested_at is not None
                    or job.status == VideoTrackerJobStatus.CANCELLED.value
                ):
                    # v0.21.28 · 取消也暂存部分结果 (候选); multiplex 先关联本窗已收部分。
                    if associate_multiplex and window_results and not is_correction:
                        window_results = _associate_multiplex_window(
                            window_results, mp_prev_boundary, mp_next_global
                        )
                    if not is_correction:
                        results.extend(window_results)
                    if results and not is_correction:
                        _stage_tracker_results(job, results, grid_step, output_geometry)
                        await lock_raster_mask_references(db, job.staged_result)
                    elif is_correction:
                        job.staged_result = None
                    job.status = VideoTrackerJobStatus.CANCELLED.value
                    job.completed_at = job.completed_at or _now()
                    await db.commit()
                    await publisher(job.event_channel, _event(job, "job_cancelled"))
                    return job

                window_results.append(result)
                progress += 1
                # live 预览事件用窗内 id (瞬态); 最终暂存的 results 用关联后的全局 id。
                frame_payload = {
                    "frame_index": result.frame_index,
                    "geometry": result.geometry,
                    "confidence": result.confidence,
                    "outside": result.outside,
                    "source": "prediction",
                }
                await publisher(
                    job.event_channel, _event(job, "frame_result", **frame_payload)
                )
                await publisher(
                    job.event_channel,
                    _event(
                        job,
                        "job_progress",
                        current=min(progress, total),
                        total=total,
                    ),
                )

            if heartbeat_failed:
                raise RuntimeError("tracker route lease heartbeat failed")

            # Backend 响应数组不保证按帧有序；统一转为追踪方向的遍历顺序，
            # 使窗口末尾几何和多实例跨窗关联都使用真实边界帧。
            window_results.sort(
                key=lambda result: result.frame_index,
                reverse=window_direction == "backward",
            )

            # 窗末: text-multiplex 关联 remap 窗内 obj_id → 跨窗全局 instance_id。
            if associate_multiplex:
                window_results = _associate_multiplex_window(
                    window_results, mp_prev_boundary, mp_next_global
                )
            if is_correction:
                primary_results, _ = _partition_results_by_instance(window_results)
                window_results = [
                    replace(result, instance_id="1", primary=True)
                    for result in primary_results
                ]
            results.extend(window_results)
            # PVS 单目标结果会带 instance_id，但不保证额外标记 primary。按与最终落库相同的
            # 主实例规则推断本窗主目标，并始终用它最后一个有效几何续下一窗；否则无源任务
            # 的 source_geometry 会一直为空，第二窗被 backend 以“缺少种子”拒绝。
            primary_results, _ = _partition_results_by_instance(window_results)
            for primary_result in reversed(primary_results):
                if not primary_result.outside and primary_result.geometry:
                    last_geometry = primary_result.geometry
                    break
            # 续种状态 (关联后 id): 逐实例记末帧几何 (供多实例续种); 主实例 (或单实例 None)
            # 另记 last_geometry 供 source_geometry 兜底 (与既有单 track 续追一致)。
            for result in window_results:
                if not result.outside and result.geometry:
                    if result.instance_id is not None:
                        last_geom_by_instance[result.instance_id] = result.geometry
                    if result.instance_id is None or result.primary:
                        last_geometry = result.geometry

        await close_active_session()

        await db.refresh(job)
        if heartbeat_failed:
            raise RuntimeError("tracker route lease heartbeat failed")
        if job.cancel_requested_at is not None:
            # v0.21.28 · 取消也暂存部分结果 (候选)。
            if results and not is_correction:
                _stage_tracker_results(job, results, grid_step, output_geometry)
                await lock_raster_mask_references(db, job.staged_result)
            elif is_correction:
                job.staged_result = None
            job.status = VideoTrackerJobStatus.CANCELLED.value
            job.completed_at = job.completed_at or _now()
            await db.commit()
            await publisher(job.event_channel, _event(job, "job_cancelled"))
            return job

        if is_correction and not results:
            raise ValueError("correction_empty_result")

        # v0.21.28 · 候选/接受流: 完成时**暂存**结果 (不落 annotation), 待用户接受/丢弃。
        # PENDING_REVIEW = 追踪完、结果已暂存、committed annotations 未改。
        _stage_tracker_results(job, results, grid_step, output_geometry)
        if context_boundaries and job.staged_result is not None:
            job.staged_result["tracker_context_boundaries"] = context_boundaries
        await lock_raster_mask_references(db, job.staged_result)
        job.status = VideoTrackerJobStatus.PENDING_REVIEW.value
        job.completed_at = _now()
        await db.commit()
        await db.refresh(job)
        await publisher(job.event_channel, _event(job, "job_completed"))
        route_succeeded = True
        return job
    except Exception as exc:
        log.exception("video tracker job failed job_id=%s", job_id)
        gpu_arbiter_error = gpu_arbiter_failure_record(exc)
        if gpu_arbiter_error is not None and failure_recorder is not None:
            failure_recorder(gpu_arbiter_error)
        message = (
            gpu_arbiter_error["message"] if gpu_arbiter_error is not None else str(exc)
        )
        return await _mark_failed(
            db,
            job_id,
            message,
            publisher,
            gpu_arbiter_error=gpu_arbiter_error,
        )
    finally:
        if tracker_session_cleanup is not None:
            try:
                await tracker_session_cleanup()
            except Exception:  # noqa: BLE001
                log.warning("tracker context cleanup failed job_id=%s", job_id)
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task
        if tracker_lease is not None and tracker_router is not None:
            try:
                if route_succeeded:
                    await tracker_router.finish(
                        tracker_lease, RouteOutcome.SUCCESS, duration_ms=0
                    )
                else:
                    await tracker_router.cancel(tracker_lease)
            except Exception:  # noqa: BLE001
                log.warning("tracker route lease cleanup failed job_id=%s", job_id)
        if tracker_ledger is not None:
            try:
                await tracker_ledger.aclose()
            except Exception:  # noqa: BLE001
                log.warning("tracker routing ledger close failed job_id=%s", job_id)
