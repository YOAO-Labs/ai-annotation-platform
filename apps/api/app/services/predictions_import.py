"""外部预测导入服务 (v0.10.15).

支持三种 input format:
- AAP JSON v1.0: 平台原生无损中间格式 (双数组 annotations[]/predictions[]);
  本期仅消费 predictions[], annotations[] 警告日志保留供后续 epic.
- COCO: 标准 COCO Detection 格式 (images + annotations + categories), 用 image
  的 file_name 匹配 task.file_path.
- YOLO: zip 包, 内含 classes.txt / data.yaml 与每图一个 label txt.

写入路径: 把内部 geometry 反向适配回 LabelStudio shape -> 复用
ApplyPredictionResultPort, 标记 source='external_import'.

ROADMAP §6 决策底线 lenient 精神:
- schema_version > 当前 major 直接 422; 同 major 内未知字段忽略.
- task 不匹配 / geometry kind 不支持 / class_name 缺失 -> errors[] 累计, 不让整批失败.
- dry_run=true 走全部校验路径但不入库.
"""

from __future__ import annotations

import ast
import io
import json
import logging
import math
import uuid
import zipfile
from typing import Any

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.dataset import DatasetItem
from app.services.project_access import get_workspace_project
from app.db.models.prediction import Prediction
from app.schemas.aap_json import (
    AAPImportErrorEntry,
    AAPImportResult,
    AAPJsonV1Envelope,
    AAPPredictionEntry,
    check_schema_major,
)
from app.schemas._jsonb_types import Geometry
from app.services.prediction import PredictionService, derive_tool_unit_from_result
from app.services.labeling_prediction_records import purge_imported_prediction_records
from app.services.labeling_prediction_contract import (
    ApplyPredictionResultPort,
    ApplyPredictionResultRequest,
)
from app.services.labeling_write_scope import capture_labeling_write_scope
from app.services.mask_formats.image_codecs import normalize_coco_segmentation_rle
from aap_workspace_contract import derive_classes_list
from app.services.task_matcher import (
    normalize_file_stem_path,
    resolve_task,
    resolve_task_by_file_stem,
)
from app.services.raster_mask_storage import (
    assert_raster_mask_write_enabled,
    build_rle_reference,
    resolve_mask_reference_objects,
    store_coco_rle,
    store_generated_mask_content,
    store_mask_reference_objects,
    validate_mask_geometry_for_task,
)
from app.utils.raster_mask_rle import coco_rle_area

logger = logging.getLogger(__name__)

_GEOMETRY_ADAPTER = TypeAdapter(Geometry)
_VIDEO_PREDICTION_UNITS = {
    "video_bbox": "bbox",
    "video_track_bbox": "bbox",
    "video_track_polygon": "region",
    "video_track_polyline": "polyline",
    "video_track_mask": "region",
}


# ── geometry kind → LabelStudio shape 适配 ─────────────────────────


def internal_geometry_to_ls_shape(
    geometry: dict[str, Any],
    class_name: str,
    confidence: float | None,
) -> dict[str, Any] | None:
    """把内部 Geometry 反向适配为 LabelStudio shape.

    平台 prediction.result JSONB 列存的是 LS 标准格式 (apps/api/app/services/
    prediction.py:to_internal_shape 是读路径的反向). 这里走"导入端 -> LS shape"
    适配, 之后复用 labeling prediction-result port 写入.

    返回 None 表示不支持的 kind, 调用方应把 errors[] 累计.

    几何归一化坐标 [0, 1]; LabelStudio 历史用 [0, 100] 表示百分比, 所以 *100.
    """
    kind = geometry.get("type")
    score = confidence if confidence is not None else None

    if kind == "bbox":
        try:
            return {
                "type": "rectanglelabels",
                "value": {
                    "x": float(geometry["x"]) * 100,
                    "y": float(geometry["y"]) * 100,
                    "width": float(geometry["w"]) * 100,
                    "height": float(geometry["h"]) * 100,
                    "rectanglelabels": [class_name],
                },
                "score": score,
            }
        except (KeyError, TypeError, ValueError):
            return None

    if kind == "polygon":
        points = geometry.get("points") or []
        if len(points) < 3:
            return None
        # LabelStudio polygonlabels: points 是 [[x*100, y*100], ...]
        ls_points = [
            [float(pt[0]) * 100, float(pt[1]) * 100] for pt in points if len(pt) == 2
        ]
        if len(ls_points) < 3:
            return None
        value: dict[str, Any] = {
            "points": ls_points,
            "polygonlabels": [class_name],
        }
        holes = geometry.get("holes") or []
        if holes:
            value["holes"] = [
                [
                    [float(pt[0]) * 100, float(pt[1]) * 100]
                    for pt in hole
                    if len(pt) == 2
                ]
                for hole in holes
            ]
        return {"type": "polygonlabels", "value": value, "score": score}

    if kind == "multi_polygon":
        polygons = geometry.get("polygons") or []
        if not polygons:
            return None
        # v0.9.14 multi_polygon LS shape: value.polygons = [{points, holes?}, ...]
        ls_polys: list[dict[str, Any]] = []
        for poly in polygons:
            pts = poly.get("points") or []
            if len(pts) < 3:
                continue
            entry: dict[str, Any] = {
                "points": [
                    [float(pt[0]) * 100, float(pt[1]) * 100]
                    for pt in pts
                    if len(pt) == 2
                ]
            }
            holes = poly.get("holes") or []
            if holes:
                entry["holes"] = [
                    [
                        [float(pt[0]) * 100, float(pt[1]) * 100]
                        for pt in hole
                        if len(pt) == 2
                    ]
                    for hole in holes
                ]
            ls_polys.append(entry)
        if not ls_polys:
            return None
        return {
            "type": "polygonlabels",
            "value": {"polygons": ls_polys, "polygonlabels": [class_name]},
            "score": score,
        }

    if kind == "polyline":
        points = geometry.get("points") or []
        if len(points) < 2:
            return None
        ls_points = [
            [float(pt[0]) * 100, float(pt[1]) * 100] for pt in points if len(pt) == 2
        ]
        if len(ls_points) < 2:
            return None
        return {
            "type": "polylinelabels",
            "value": {"points": ls_points, "polylinelabels": [class_name]},
            "score": score,
        }

    if kind == "rotated_bbox":
        try:
            cx = float(geometry["cx"])
            cy = float(geometry["cy"])
            w = float(geometry["w"])
            h = float(geometry["h"])
            angle = float(geometry["angle"]) % 360
        except (KeyError, TypeError, ValueError):
            return None
        if w <= 0 or h <= 0:
            return None

        rad = math.radians(angle)
        cos_a = math.cos(rad)
        sin_a = math.sin(rad)
        # LabelStudio rectangle rotation is anchored at the rotated top-left corner.
        x = cx + (-w / 2) * cos_a - (-h / 2) * sin_a
        y = cy + (-w / 2) * sin_a + (-h / 2) * cos_a
        return {
            "type": "rectanglelabels",
            "value": {
                "x": x * 100,
                "y": y * 100,
                "width": w * 100,
                "height": h * 100,
                "rotation": angle,
                "rectanglelabels": [class_name],
            },
            "score": score,
        }

    if kind == "keypoint":
        points = geometry.get("points") or []
        if not points:
            return None
        ls_points: list[dict[str, Any]] = []
        for pt in points:
            try:
                if isinstance(pt, dict):
                    x = float(pt["x"])
                    y = float(pt["y"])
                    v = int(pt.get("v", 2))
                elif isinstance(pt, (list, tuple)) and len(pt) >= 2:
                    x = float(pt[0])
                    y = float(pt[1])
                    v = int(pt[2]) if len(pt) >= 3 else 2
                else:
                    continue
            except (KeyError, TypeError, ValueError):
                continue
            ls_points.append({"x": x * 100, "y": y * 100, "v": v})
        if not ls_points:
            return None
        return {
            "type": "keypointlabels",
            "value": {"points": ls_points, "keypointlabels": [class_name]},
            "score": score,
        }

    if kind == "raster_mask":
        return {
            "type": "raster_mask",
            "class_name": class_name,
            "geometry": geometry,
            "confidence": score,
            "tool_unit_id": "region",
            "attributes": {},
        }

    if kind in _VIDEO_PREDICTION_UNITS:
        return {
            "type": kind,
            "class_name": class_name,
            "geometry": geometry,
            "confidence": score,
            "tool_unit_id": _VIDEO_PREDICTION_UNITS[kind],
            "attributes": {},
        }

    return None


# ── purge / overwrite 语义 ────────────────────────────────────────────


def _empty_purge_counts() -> dict[str, int]:
    return {"ml_backend": 0, "external_import": 0, "unknown": 0, "total": 0}


async def _purge_predictions(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    task_ids: list[uuid.UUID] | set[uuid.UUID] | None = None,
    source_scope: str = "external_import",
    dry_run: bool = False,
) -> dict[str, int]:
    """Count and optionally delete predictions by project, task scope, and source.

    PredictionMeta 通过 FK 引用 predictions.id (无 CASCADE), 必须先删 meta 再删 prediction.
    """
    if source_scope not in {"ml_backend", "external_import", "all"}:
        raise ValueError(
            "source_scope must be one of: ml_backend, external_import, all"
        )

    normalized_task_ids = None
    if task_ids is not None:
        normalized_task_ids = list(dict.fromkeys(task_ids))
        if not normalized_task_ids:
            return _empty_purge_counts()

    if not dry_run:
        data = await purge_imported_prediction_records(
            db,
            project_id=project_id,
            task_ids=normalized_task_ids,
            source_scope=source_scope,
        )
        return {
            "ml_backend": data.ml_backend,
            "external_import": data.external_import,
            "unknown": data.unknown,
            "total": data.total,
        }

    filters = [Prediction.project_id == project_id]
    if normalized_task_ids is not None:
        filters.append(Prediction.task_id.in_(normalized_task_ids))
    if source_scope != "all":
        filters.append(Prediction.source == source_scope)

    target_rows = (
        await db.execute(select(Prediction.id, Prediction.source).where(*filters))
    ).all()
    target_ids = [row[0] for row in target_rows]
    counts = _empty_purge_counts()
    for _, source in target_rows:
        key = source if source in {"ml_backend", "external_import"} else "unknown"
        counts[key] += 1
        counts["total"] += 1
    if not target_ids:
        return counts

    return counts


async def _hold_import_authority(
    db: AsyncSession, *, actor_id: uuid.UUID | None, project_id: uuid.UUID
) -> None:
    from fastapi import HTTPException

    from app.services.project_access import (
        bind_project_write_policy_resolver,
        restore_project_write_policy_failure,
    )
    from aap_workspace_contract.access import (
        ProjectWritePolicyFailure,
        ProjectWritePolicyRequest,
    )

    if actor_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    resolver = bind_project_write_policy_resolver(db)
    try:
        access = await resolver.resolve(
            ProjectWritePolicyRequest(actor_id=actor_id, project_id=project_id)
        )
    except ProjectWritePolicyFailure as exc:
        restore_project_write_policy_failure(exc)
    if not access.is_manager:
        raise HTTPException(status_code=403, detail="项目管理权限已变化")


# ── AAP JSON importer ──────────────────────────────────────────────


async def import_aap_json(
    db: AsyncSession,
    project_id: uuid.UUID,
    file_bytes: bytes,
    *,
    actor_id: uuid.UUID | None,
    job_id: uuid.UUID | None,
    model_version_fallback: str | None = None,
    overwrite_existing: bool = False,
    dry_run: bool = False,
    purged_tasks: set[uuid.UUID] | None = None,
) -> AAPImportResult:
    try:
        raw = json.loads(file_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"AAP JSON 文件无法解析: {exc}") from exc

    schema_version = raw.get("schema_version") if isinstance(raw, dict) else None
    if not isinstance(schema_version, str):
        raise ValueError("AAP JSON 缺少 schema_version 字段 (1.0)")
    check_schema_major(schema_version)

    try:
        envelope = AAPJsonV1Envelope.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"AAP JSON envelope 校验失败: {exc.errors()[:3]}") from exc
    if not dry_run:
        await _hold_import_authority(db, actor_id=actor_id, project_id=project_id)

    result = AAPImportResult(dry_run=dry_run)
    svc: ApplyPredictionResultPort = PredictionService(db)
    stored_mask_keys: set[str] = set()
    project = await get_workspace_project(db, project_id=project_id)

    purged_tasks = purged_tasks if purged_tasks is not None else set()

    for block in envelope.tasks:
        match_dict = block.task_match.model_dump(exclude_none=True)
        # 兜底用 task block 顶层 file_path (兼容客户手工组装 task_match 漏写 file_path).
        if "file_path" not in match_dict and block.file_path:
            match_dict["file_path"] = block.file_path

        if block.annotations:
            # v0.10.15 不消费 annotations[]; 仅警告.
            logger.warning(
                "AAP JSON import: annotations[] 字段在 v0.10.15 暂不消费, "
                "已跳过 %d 条 (task_match=%s)",
                len(block.annotations),
                match_dict,
            )

        if not block.predictions:
            continue

        task = await resolve_task(db, project_id, match_dict)
        if task is None:
            for _ in block.predictions:
                result.errors.append(
                    AAPImportErrorEntry(
                        task_match=match_dict,
                        reason="task not found in project",
                    )
                )
                result.skipped += 1
            continue

        # AAP JSON 每个 prediction entry 对应一条平台 Prediction 行; entry.shapes
        # 可把多个 shape 合并进同一 Prediction.result.
        for entry in block.predictions:
            mask_geometries: list[dict[str, Any]] = []
            prepared_shapes: list[dict[str, Any]] = []
            preparation_errors: list[str] = []
            frame_count: int | None = None
            for raw_shape in _entry_shape_sources(entry):
                if not isinstance(raw_shape, dict):
                    prepared_shapes.append(raw_shape)
                    continue
                geometry = _shape_geometry(raw_shape)
                kind = geometry.get("type")
                if kind not in _VIDEO_PREDICTION_UNITS:
                    prepared_shapes.append(raw_shape)
                    continue
                try:
                    if block.media_type != "video":
                        raise ValueError(
                            "video prediction requires task block media_type=video"
                        )
                    if frame_count is None:
                        frame_count = await _source_video_frame_count(db, task)
                    normalized = _normalize_video_prediction_geometry(
                        geometry, frame_count=frame_count
                    )
                    prepared_shapes.append({**raw_shape, "geometry": normalized})
                except (ValidationError, ValueError) as exc:
                    preparation_errors.append(f"invalid geometry: {exc}")

            try:
                for raw_shape in prepared_shapes:
                    geometry = _shape_geometry(raw_shape)
                    if geometry.get("type") not in {
                        "raster_mask",
                        "video_track_mask",
                    }:
                        continue
                    validated = _GEOMETRY_ADAPTER.validate_python(geometry)
                    normalized = validated.model_dump(
                        mode="json", by_alias=True, exclude_unset=True
                    )
                    await validate_mask_geometry_for_task(db, task, normalized)
                    mask_geometries.append(normalized)
                resolved_masks = resolve_mask_reference_objects(
                    mask_geometries, envelope.mask_objects
                )
                if any(coco_rle_area(rle) == 0 for _, rle in resolved_masks):
                    raise ValueError("raster mask must contain foreground pixels")
                if mask_geometries:
                    assert_raster_mask_write_enabled(project)
            except (ValidationError, ValueError) as exc:
                result.errors.append(
                    AAPImportErrorEntry(
                        task_match=match_dict,
                        reason=f"invalid geometry: {exc}",
                    )
                )
                result.skipped += 1
                continue

            ls_shapes, errors = _entry_to_ls_shapes(entry, prepared_shapes)
            errors = [*preparation_errors, *errors]
            units = {derive_tool_unit_from_result([shape]) for shape in ls_shapes}
            if len(units) > 1:
                ls_shapes = []
                errors.append("prediction entry shapes must use one tool unit")
            for reason in errors:
                result.errors.append(
                    AAPImportErrorEntry(task_match=match_dict, reason=reason)
                )
                result.skipped += 1

            if not ls_shapes:
                if not errors:
                    result.errors.append(
                        AAPImportErrorEntry(
                            task_match=match_dict,
                            reason="prediction entry has no shapes",
                        )
                    )
                    result.skipped += 1
                continue

            if dry_run:
                result.imported += 1
                continue

            if overwrite_existing and task.id not in purged_tasks:
                await _purge_predictions(
                    db,
                    project_id=project_id,
                    task_ids=[task.id],
                    source_scope="external_import",
                )
                purged_tasks.add(task.id)

            if resolved_masks:
                unstored = [
                    item
                    for item in resolved_masks
                    if str(item[0]["object_key"]) not in stored_mask_keys
                ]
                await store_mask_reference_objects(
                    db,
                    mask_geometries,
                    unstored,
                    task_id=task.id,
                )
                stored_mask_keys.update(
                    str(reference["object_key"]) for reference, _ in unstored
                )

            await svc.apply_prediction_result(
                ApplyPredictionResultRequest(
                    task_id=task.id,
                    project_id=project_id,
                    ml_backend_id=None,
                    result=ls_shapes,
                    score=entry.score if entry.score is not None else entry.confidence,
                    model_version=entry.model_version or model_version_fallback,
                    source="external_import",
                    write_scope=capture_labeling_write_scope(
                        task,
                        actor_id=actor_id,
                        actor_known=actor_id is not None,
                        operation="prediction_import",
                        execution_scope=None,
                        execution_scope_known=True,
                        job_id=job_id,
                        job_id_known=True,
                    ),
                ),
            )
            result.imported += 1

    return result


def _entry_shape_sources(entry: AAPPredictionEntry) -> list[dict[str, Any]]:
    if entry.shapes is not None:
        return entry.shapes
    if isinstance(entry.geometry, dict):
        return [entry.geometry]
    return []


def _shape_geometry(shape: dict[str, Any]) -> dict[str, Any]:
    raw_geometry = shape.get("geometry")
    if isinstance(raw_geometry, dict):
        return raw_geometry
    return shape


def _shape_class_name(shape: dict[str, Any], entry: AAPPredictionEntry) -> str | None:
    raw = shape.get("class_name")
    if isinstance(raw, str) and raw:
        return raw
    return entry.class_name


def _shape_confidence(shape: dict[str, Any], entry: AAPPredictionEntry) -> float | None:
    raw = shape.get("confidence")
    if raw is None:
        return entry.confidence
    try:
        return float(raw)
    except (TypeError, ValueError):
        return entry.confidence


def _entry_to_ls_shapes(
    entry: AAPPredictionEntry,
    shapes: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    ls_shapes: list[dict[str, Any]] = []
    errors: list[str] = []

    for raw_shape in shapes if shapes is not None else _entry_shape_sources(entry):
        if not isinstance(raw_shape, dict):
            errors.append("unsupported geometry kind: None")
            continue

        class_name = _shape_class_name(raw_shape, entry)
        if not class_name:
            errors.append("missing class_name")
            continue

        geometry = _shape_geometry(raw_shape)
        ls_shape = internal_geometry_to_ls_shape(
            geometry,
            class_name,
            _shape_confidence(raw_shape, entry),
        )
        if ls_shape is None:
            kind = geometry.get("type") if isinstance(geometry, dict) else None
            errors.append(f"unsupported geometry kind: {kind!r}")
            continue

        ls_shapes.append(ls_shape)

    return ls_shapes, errors


async def _source_video_frame_count(db: AsyncSession, task: Any) -> int:
    if task.file_type != "video" or task.dataset_item_id is None:
        raise ValueError("video prediction requires a video task and dataset item")
    from app.services.assets_media_summary import AssetsTaskMediaSummaryReader

    video = await AssetsTaskMediaSummaryReader(db).current_video_metadata(
        task.dataset_item_id
    )
    if video is None:
        raise ValueError("video prediction requires a video task and dataset item")
    raw = video.get("frame_count") if isinstance(video, dict) else None
    if isinstance(raw, bool):
        raw = None
    try:
        frame_count = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("source video frame_count is required") from exc
    if frame_count <= 0:
        raise ValueError("source video frame_count is required")
    return frame_count


def _normalize_video_prediction_geometry(
    geometry: dict[str, Any], *, frame_count: int
) -> dict[str, Any]:
    validated = _GEOMETRY_ADAPTER.validate_python(geometry)
    normalized = validated.model_dump(mode="json", by_alias=True, exclude_unset=True)
    kind = normalized.get("type")
    if kind not in _VIDEO_PREDICTION_UNITS:
        raise ValueError(f"unsupported video geometry kind: {kind!r}")

    if kind == "video_bbox":
        frames = [int(normalized["frame_index"])]
    else:
        keyframes = list(normalized.get("keyframes") or [])
        frames = [int(keyframe["frame_index"]) for keyframe in keyframes]
        if frames != sorted(frames) or len(frames) != len(set(frames)):
            raise ValueError(
                "video track keyframes must be strictly increasing and unique"
            )
        for keyframe in keyframes:
            keyframe["source"] = "prediction"
        for outside in normalized.get("outside") or []:
            start = int(outside["from"])
            end = int(outside["to"])
            if start > end:
                raise ValueError("outside range must satisfy from <= to")
            if end >= frame_count:
                raise ValueError(
                    f"outside range must be within source frame_count {frame_count}"
                )
            outside["source"] = "prediction"

    if any(frame >= frame_count for frame in frames):
        raise ValueError(f"frame_index must be < source frame_count {frame_count}")
    return normalized


# ── COCO importer ──────────────────────────────────────────────────


def _coco_bbox_geometry(
    bbox: Any,
    *,
    image_width: float,
    image_height: float,
) -> dict[str, Any]:
    if not isinstance(bbox, list) or len(bbox) != 4:
        raise ValueError("bbox must be [x, y, width, height]")
    try:
        x, y, width, height = (float(value) for value in bbox)
    except (TypeError, ValueError) as exc:
        raise ValueError("bbox values must be numbers") from exc
    if not all(math.isfinite(value) for value in (x, y, width, height)):
        raise ValueError("bbox values must be finite")
    if width < 0 or height < 0:
        raise ValueError("bbox width / height must be non-negative")
    return {
        "type": "bbox",
        "x": x / image_width,
        "y": y / image_height,
        "w": width / image_width,
        "h": height / image_height,
    }


def _coco_polygon_geometry(
    segmentation: Any,
    *,
    image_width: float,
    image_height: float,
) -> dict[str, Any]:
    if not isinstance(segmentation, list) or not segmentation:
        raise ValueError("polygon segmentation must contain at least one ring")
    polygons: list[dict[str, Any]] = []
    for ring in segmentation:
        if not isinstance(ring, list) or len(ring) < 6 or len(ring) % 2:
            raise ValueError("polygon ring must contain at least three x/y pairs")
        try:
            values = [float(value) for value in ring]
        except (TypeError, ValueError) as exc:
            raise ValueError("polygon coordinates must be numbers") from exc
        if not all(math.isfinite(value) for value in values):
            raise ValueError("polygon coordinates must be finite")
        polygons.append(
            {
                "type": "polygon",
                "points": [
                    [values[index] / image_width, values[index + 1] / image_height]
                    for index in range(0, len(values), 2)
                ],
            }
        )
    if len(polygons) == 1:
        return polygons[0]
    return {"type": "multi_polygon", "polygons": polygons}


def _coco_uncompressed_rle(
    segmentation: Any,
    *,
    image_width: float,
    image_height: float,
) -> dict[str, Any]:
    if not isinstance(segmentation, dict):
        raise ValueError("RLE segmentation must be an object")
    if not image_width.is_integer() or not image_height.is_integer():
        raise ValueError("COCO image width / height must be integers")
    return normalize_coco_segmentation_rle(
        segmentation,
        expected_width=int(image_width),
        expected_height=int(image_height),
    )


async def import_coco(
    db: AsyncSession,
    project_id: uuid.UUID,
    file_bytes: bytes,
    *,
    actor_id: uuid.UUID | None,
    job_id: uuid.UUID | None,
    model_version_fallback: str | None = None,
    overwrite_existing: bool = False,
    dry_run: bool = False,
    image_size_hint: tuple[int, int] | None = None,
    purged_tasks: set[uuid.UUID] | None = None,
) -> AAPImportResult:
    """COCO detection / polygon / compressed or uncompressed RLE importer.

    bbox 保持历史行为；polygon-only annotation 转内部 polygon；uncompressed RLE
    写入内容寻址对象后，以 raster_mask 内部 prediction shape 保存。

    匹配: 用 image.file_name 当 task.file_path (调用方应保证 dataset 命名一致).
    """

    try:
        raw = json.loads(file_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"COCO JSON 文件无法解析: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError("COCO JSON 顶层必须是 object")
    if not dry_run:
        await _hold_import_authority(db, actor_id=actor_id, project_id=project_id)

    images_raw = raw.get("images") or []
    annotations_raw = raw.get("annotations") or []
    categories_raw = raw.get("categories") or []

    cat_map: dict[int, str] = {}
    for cat in categories_raw:
        if not isinstance(cat, dict):
            continue
        cat_id = cat.get("id")
        cat_name = cat.get("name")
        if isinstance(cat_id, int) and isinstance(cat_name, str):
            cat_map[cat_id] = cat_name

    image_map: dict[int, dict[str, Any]] = {}
    for img in images_raw:
        if not isinstance(img, dict):
            continue
        img_id = img.get("id")
        if isinstance(img_id, int):
            image_map[img_id] = img

    result = AAPImportResult(dry_run=dry_run)
    svc: ApplyPredictionResultPort = PredictionService(db)
    project = await get_workspace_project(db, project_id=project_id)
    purged_tasks = purged_tasks if purged_tasks is not None else set()
    stored_rle_references: dict[str, dict[str, Any]] = {}

    for ann in annotations_raw:
        if not isinstance(ann, dict):
            continue
        img_id = ann.get("image_id")
        bbox = ann.get("bbox")
        segmentation = ann.get("segmentation")
        cat_id = ann.get("category_id")

        img = image_map.get(img_id) if isinstance(img_id, int) else None
        if img is None:
            result.errors.append(
                AAPImportErrorEntry(
                    task_match={"coco_image_id": img_id},
                    reason="invalid COCO annotation: image not found",
                )
            )
            result.skipped += 1
            continue

        file_name = img.get("file_name") or ""
        img_w = float(
            img.get("width") or (image_size_hint[0] if image_size_hint else 0)
        )
        img_h = float(
            img.get("height") or (image_size_hint[1] if image_size_hint else 0)
        )
        if img_w <= 0 or img_h <= 0:
            result.errors.append(
                AAPImportErrorEntry(
                    task_match={"file_path": file_name},
                    reason="coco image missing width/height; image_size_hint not provided",
                )
            )
            result.skipped += 1
            continue

        class_name = cat_map.get(cat_id) if isinstance(cat_id, int) else None
        if not class_name:
            result.errors.append(
                AAPImportErrorEntry(
                    task_match={"file_path": file_name},
                    reason=f"unknown category_id: {cat_id!r}",
                )
            )
            result.skipped += 1
            continue

        # 匹配: 优先用 file_name 当 file_path; 兼容 file_name 顶层带斜杠子目录.
        match_dict = {"file_path": file_name}
        task = await resolve_task(db, project_id, match_dict)
        if task is None:
            result.errors.append(
                AAPImportErrorEntry(
                    task_match=match_dict,
                    reason="task not found in project",
                )
            )
            result.skipped += 1
            continue

        score = ann.get("score")
        # 预标注导入缺省满分: COCO GT 标注通常无 score, 缺省视作 100% 置信(确定标签)。
        # 否则 score=null 经 to_internal_shape 落为 confidence=0.0, 被工作台默认 50% 阈值
        # 全量过滤而不可见。COCO 结果格式若带 score 则保留原值。
        confidence = float(score) if isinstance(score, (int, float)) else 1.0

        rle: dict[str, Any] | None = None
        try:
            if isinstance(segmentation, dict):
                rle = _coco_uncompressed_rle(
                    segmentation,
                    image_width=img_w,
                    image_height=img_h,
                )
                if coco_rle_area(rle) == 0:
                    raise ValueError("raster mask must contain foreground pixels")
                assert_raster_mask_write_enabled(project)
                geometry = {
                    "type": "raster_mask",
                    "mask": build_rle_reference(rle),
                }
            elif isinstance(segmentation, list):
                geometry = _coco_polygon_geometry(
                    segmentation,
                    image_width=img_w,
                    image_height=img_h,
                )
            elif isinstance(bbox, list):
                geometry = _coco_bbox_geometry(
                    bbox,
                    image_width=img_w,
                    image_height=img_h,
                )
            else:
                raise ValueError("annotation requires bbox or segmentation")
        except ValueError as exc:
            result.errors.append(
                AAPImportErrorEntry(
                    task_match=match_dict,
                    reason=f"invalid COCO annotation: {exc}",
                )
            )
            result.skipped += 1
            continue

        ls_shape = internal_geometry_to_ls_shape(geometry, class_name, confidence)
        if ls_shape is None:
            result.errors.append(
                AAPImportErrorEntry(
                    task_match=match_dict,
                    reason="failed to build prediction shape from COCO annotation",
                )
            )
            result.skipped += 1
            continue

        if dry_run:
            result.imported += 1
            continue

        if overwrite_existing and task.id not in purged_tasks:
            await _purge_predictions(
                db,
                project_id=project_id,
                task_ids=[task.id],
                source_scope="external_import",
            )
            purged_tasks.add(task.id)

        if rle is not None:
            object_key = str(geometry["mask"]["object_key"])
            stored = stored_rle_references.get(object_key)
            if stored is None:
                stored = await store_generated_mask_content(
                    db, rle, writer=store_coco_rle
                )
                if stored["object_key"] != object_key:
                    raise ValueError("stored COCO RLE does not match content reference")
                stored_rle_references[object_key] = stored
            ls_shape["geometry"] = {"type": "raster_mask", "mask": stored}

        await svc.apply_prediction_result(
            ApplyPredictionResultRequest(
                task_id=task.id,
                project_id=project_id,
                ml_backend_id=None,
                result=[ls_shape],
                score=confidence,
                model_version=model_version_fallback,
                source="external_import",
                write_scope=capture_labeling_write_scope(
                    task,
                    actor_id=actor_id,
                    actor_known=actor_id is not None,
                    operation="prediction_import",
                    execution_scope=None,
                    execution_scope_known=True,
                    job_id=job_id,
                    job_id_known=True,
                ),
            ),
        )
        result.imported += 1

    return result


# ── YOLO zip importer (det / obb / seg) ─────────────────────────────


YOLO_VARIANTS = {"det", "obb", "seg"}


async def import_yolo(
    db: AsyncSession,
    project_id: uuid.UUID,
    file_bytes: bytes,
    *,
    actor_id: uuid.UUID | None,
    job_id: uuid.UUID | None,
    yolo_variant: str = "det",
    model_version_fallback: str | None = None,
    overwrite_existing: bool = False,
    dry_run: bool = False,
    purged_tasks: set[uuid.UUID] | None = None,
) -> AAPImportResult:
    """YOLO label zip importer.

    One non-empty label file becomes one platform Prediction row containing all
    valid shapes from that file.  Empty label files are treated as no-op, which
    keeps platform-exported YOLO packages round-trippable.
    """

    if yolo_variant not in YOLO_VARIANTS:
        raise ValueError("yolo_variant must be one of: det, obb, seg")

    try:
        zf = zipfile.ZipFile(io.BytesIO(file_bytes))
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValueError(f"YOLO zip 文件无法解析: {exc}") from exc

    project = await get_workspace_project(db, project_id=project_id)
    project_classes = derive_classes_list(project.tool_bindings if project else None)
    class_names = _read_yolo_class_names(zf) or project_classes
    if not class_names:
        raise ValueError(
            "YOLO zip 缺少 classes.txt/data.yaml, 且项目没有可回退的类别顺序"
        )
    if not dry_run:
        await _hold_import_authority(db, actor_id=actor_id, project_id=project_id)

    project_class_set = set(project_classes)
    result = AAPImportResult(dry_run=dry_run)
    svc: ApplyPredictionResultPort = PredictionService(db)
    purged_tasks = purged_tasks if purged_tasks is not None else set()

    for label_name in _iter_yolo_label_files(zf):
        match_dict = {"file_stem": normalize_file_stem_path(label_name)}
        try:
            text = zf.read(label_name).decode("utf-8-sig")
        except UnicodeDecodeError:
            result.errors.append(
                AAPImportErrorEntry(
                    task_match=match_dict,
                    reason="label file is not valid utf-8",
                )
            )
            result.skipped += 1
            continue

        lines = [
            (i, line.strip())
            for i, line in enumerate(text.splitlines(), start=1)
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if not lines:
            continue

        task, match_error = await resolve_task_by_file_stem(db, project_id, label_name)
        if task is None:
            result.errors.append(
                AAPImportErrorEntry(
                    task_match=match_dict,
                    reason=match_error or "task not found in project",
                )
            )
            result.skipped += 1
            continue

        image_size = await _task_image_size(db, task)
        if yolo_variant == "obb" and image_size is None:
            result.errors.append(
                AAPImportErrorEntry(
                    task_match=match_dict,
                    reason="dataset item width/height required for YOLO OBB",
                )
            )
            result.skipped += 1
            continue

        if overwrite_existing and not dry_run and task.id not in purged_tasks:
            await _purge_predictions(
                db,
                project_id=project_id,
                task_ids=[task.id],
                source_scope="external_import",
            )
            purged_tasks.add(task.id)

        ls_shapes: list[dict[str, Any]] = []
        for line_no, line in lines:
            parsed, error = _parse_yolo_line(
                line,
                yolo_variant,
                class_names,
                project_class_set,
                image_size=image_size,
            )
            if error:
                result.errors.append(
                    AAPImportErrorEntry(
                        task_match=match_dict,
                        reason=f"{label_name}:{line_no}: {error}",
                    )
                )
                result.skipped += 1
                continue
            assert parsed is not None
            geometry, class_name = parsed
            # 预标注导入缺省满分: YOLO label 无置信度列, 缺省视作 100%(确定标签)。
            # 否则经 to_internal_shape 落为 confidence=0.0, 被工作台默认 50% 阈值过滤而不可见。
            ls_shape = internal_geometry_to_ls_shape(geometry, class_name, 1.0)
            if ls_shape is None:
                result.errors.append(
                    AAPImportErrorEntry(
                        task_match=match_dict,
                        reason=f"{label_name}:{line_no}: failed to build ls shape",
                    )
                )
                result.skipped += 1
                continue
            ls_shapes.append(ls_shape)

        if not ls_shapes:
            continue

        if dry_run:
            result.imported += 1
            continue

        await svc.apply_prediction_result(
            ApplyPredictionResultRequest(
                task_id=task.id,
                project_id=project_id,
                ml_backend_id=None,
                result=ls_shapes,
                score=1.0,  # YOLO label 无分数 → 预测级 score 同样缺省满分(与逐 shape 一致)
                model_version=model_version_fallback,
                source="external_import",
                write_scope=capture_labeling_write_scope(
                    task,
                    actor_id=actor_id,
                    actor_known=actor_id is not None,
                    operation="prediction_import",
                    execution_scope=None,
                    execution_scope_known=True,
                    job_id=job_id,
                    job_id_known=True,
                ),
            ),
        )
        result.imported += 1

    return result


def _iter_yolo_label_files(zf: zipfile.ZipFile) -> list[str]:
    names: list[str] = []
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/")
        leaf = name.rsplit("/", 1)[-1].lower()
        if leaf == "classes.txt" or leaf.endswith(".attrs.json"):
            continue
        if leaf.startswith(".") or name.startswith("__MACOSX/"):
            continue
        if leaf.endswith(".txt"):
            names.append(info.filename)
    return sorted(names)


def _read_yolo_class_names(zf: zipfile.ZipFile) -> list[str]:
    classes_files = sorted(
        n
        for n in zf.namelist()
        if n.replace("\\", "/").rsplit("/", 1)[-1] == "classes.txt"
    )
    for name in classes_files:
        try:
            names = [
                line.strip()
                for line in zf.read(name).decode("utf-8-sig").splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
        except UnicodeDecodeError:
            continue
        if names:
            return names

    yaml_files = sorted(
        n
        for n in zf.namelist()
        if n.replace("\\", "/").rsplit("/", 1)[-1] in {"data.yaml", "data.yml"}
    )
    for name in yaml_files:
        try:
            names = _parse_yolo_yaml_names(zf.read(name).decode("utf-8-sig"))
        except UnicodeDecodeError:
            continue
        if names:
            return names
    return []


def _parse_yolo_yaml_names(text: str) -> list[str]:
    lines = text.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("names:"):
            continue
        tail = stripped[len("names:") :].strip()
        if tail:
            parsed = _parse_inline_names(tail)
            if parsed:
                return parsed
        out: list[tuple[int, str] | str] = []
        for child in lines[i + 1 :]:
            if not child.startswith((" ", "\t")):
                break
            item = child.strip()
            if not item or item.startswith("#"):
                continue
            if item.startswith("- "):
                out.append(item[2:].strip().strip("'\""))
                continue
            if ":" in item:
                key, value = item.split(":", 1)
                try:
                    out.append((int(key.strip()), value.strip().strip("'\"")))
                except ValueError:
                    continue
        if out and isinstance(out[0], tuple):
            pairs = [p for p in out if isinstance(p, tuple)]
            return [name for _, name in sorted(pairs, key=lambda p: p[0]) if name]
        return [name for name in out if isinstance(name, str) and name]
    return []


def _parse_inline_names(raw: str) -> list[str]:
    try:
        value = ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        value = None
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, dict):
        pairs: list[tuple[int, str]] = []
        for k, v in value.items():
            try:
                pairs.append((int(k), str(v).strip()))
            except (TypeError, ValueError):
                continue
        return [name for _, name in sorted(pairs, key=lambda p: p[0]) if name]
    if raw.startswith("[") and raw.endswith("]"):
        return [p.strip().strip("'\"") for p in raw[1:-1].split(",") if p.strip()]
    if raw.startswith("{") and raw.endswith("}"):
        pairs: list[tuple[int, str]] = []
        for item in raw[1:-1].split(","):
            if ":" not in item:
                continue
            key, value = item.split(":", 1)
            try:
                pairs.append((int(key.strip()), value.strip().strip("'\"")))
            except ValueError:
                continue
        return [name for _, name in sorted(pairs, key=lambda p: p[0]) if name]
    return []


async def _task_image_size(
    db: AsyncSession,
    task: Any,
) -> tuple[int, int] | None:
    if not getattr(task, "dataset_item_id", None):
        return None
    item = await db.get(DatasetItem, task.dataset_item_id)
    if not item or not item.width or not item.height:
        return None
    if item.width <= 0 or item.height <= 0:
        return None
    return int(item.width), int(item.height)


def _parse_yolo_line(
    line: str,
    variant: str,
    class_names: list[str],
    project_class_set: set[str],
    *,
    image_size: tuple[int, int] | None,
) -> tuple[tuple[dict[str, Any], str] | None, str | None]:
    parts = line.split()
    try:
        class_idx = int(parts[0])
    except (IndexError, ValueError):
        return None, "invalid class index"
    if class_idx < 0 or class_idx >= len(class_names):
        return None, f"class index out of range: {class_idx}"
    class_name = class_names[class_idx]
    if project_class_set and class_name not in project_class_set:
        return None, f"class not found in project: {class_name}"

    try:
        values = [float(p) for p in parts[1:]]
    except ValueError:
        return None, "coordinates must be numeric"
    if any(not math.isfinite(v) for v in values):
        return None, "coordinates must be finite"

    if variant == "det":
        if len(values) != 4:
            return None, "YOLO det row must be: cls cx cy w h"
        cx, cy, w, h = values
        if not _all_unit_interval(values) or w <= 0 or h <= 0:
            return None, "YOLO det coordinates must be normalized and positive"
        x = cx - w / 2
        y = cy - h / 2
        if x < 0 or y < 0 or x + w > 1 or y + h > 1:
            return None, "YOLO det bbox falls outside image bounds"
        return ({"type": "bbox", "x": x, "y": y, "w": w, "h": h}, class_name), None

    if variant == "seg":
        if len(values) < 6 or len(values) % 2 != 0:
            return None, "YOLO seg row must contain at least 3 points"
        if not _all_unit_interval(values):
            return None, "YOLO seg coordinates must be normalized"
        points = [[values[i], values[i + 1]] for i in range(0, len(values), 2)]
        return ({"type": "polygon", "points": points}, class_name), None

    if len(values) != 8:
        return None, "YOLO obb row must be: cls x1 y1 ... x4 y4"
    if not _all_unit_interval(values):
        return None, "YOLO obb coordinates must be normalized"
    if image_size is None:
        return None, "dataset item width/height required for YOLO OBB"
    geometry = _yolo_obb_to_geometry(values, image_size)
    return (geometry, class_name), None


def _all_unit_interval(values: list[float]) -> bool:
    return all(0 <= v <= 1 for v in values)


def _yolo_obb_to_geometry(
    values: list[float],
    image_size: tuple[int, int],
) -> dict[str, Any]:
    img_w, img_h = image_size
    points = [[values[i], values[i + 1]] for i in range(0, len(values), 2)]
    px = [(x * img_w, y * img_h) for x, y in points]
    edges = [
        (px[(i + 1) % 4][0] - px[i][0], px[(i + 1) % 4][1] - px[i][1]) for i in range(4)
    ]
    lengths = [math.hypot(dx, dy) for dx, dy in edges]

    def close(a: float, b: float) -> bool:
        return math.isclose(a, b, rel_tol=0.03, abs_tol=1e-6)

    def orthogonal(a: tuple[float, float], b: tuple[float, float]) -> bool:
        denom = max(math.hypot(*a) * math.hypot(*b), 1e-9)
        return abs(a[0] * b[0] + a[1] * b[1]) / denom < 0.03

    is_rectangle = (
        min(lengths) > 0
        and close(lengths[0], lengths[2])
        and close(lengths[1], lengths[3])
        and orthogonal(edges[0], edges[1])
        and orthogonal(edges[1], edges[2])
    )
    if not is_rectangle:
        return {"type": "polygon", "points": points}

    cx = sum(x for x, _ in px) / 4 / img_w
    cy = sum(y for _, y in px) / 4 / img_h
    width = lengths[0] / img_w
    height = lengths[1] / img_h
    angle = math.degrees(math.atan2(edges[0][1], edges[0][0])) % 360
    return {
        "type": "rotated_bbox",
        "cx": cx,
        "cy": cy,
        "w": width,
        "h": height,
        "angle": angle,
    }
