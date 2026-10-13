from __future__ import annotations

import io
import json
import uuid
import zipfile
from datetime import datetime
from types import SimpleNamespace
from typing import AsyncIterator, cast
from xml.etree.ElementTree import Element, SubElement, tostring

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.annotation import Annotation
from app.db.models.dataset import Dataset, DatasetItem, Scene
from app.db.models.prediction import Prediction
from app.db.models.project import Project
from app.db.models.scene_track import SceneTrack, SceneTrackInterval
from app.db.models.task import Task
from app.services.project import (
    derive_attribute_keys,
    derive_attribute_schema,
    derive_classes_config,
    derive_classes_list,
    sanitize_annotation_attributes,
)
from app.services.prediction import to_internal_shape
from app.services.axis_convention import (
    AxisFrame,
    transform_box_geometry_axis_frame,
)
from app.schemas.aap_json import (
    AAP_SCHEMA_VERSION,
    AAPAnnotationEntry,
    AAPExportedFrom,
    AAPJsonV1Envelope,
    AAPPredictionEntry,
    AAPProjectMeta,
    AAPSceneTrackEntry,
    AAPSceneTrackInterval,
    AAPTaskBlock,
    AAPTaskMatch,
)
from app.services.exporting.video import (
    VIDEO_LOSSLESS_SINGLE_FRAME_GEOMETRY_TYPES,
    VIDEO_TRACK_GEOMETRY_TYPES,
)
from app.services.exporting.video_scope import (
    VideoExportScope,
    clip_video_geometry,
)
from app.services.mask_formats.image_codecs import compress_coco_rle
from app.services.video_tracks import (
    VIDEO_FRAME_MODES,
    clean_keyframe,
    normalize_outside_ranges,
    resolve_track_at_frame,
    resolved_track_frames,
    sorted_keyframes,
)
from app.services.raster_mask_storage import collect_mask_references, load_coco_rle
from app.utils.raster_mask_rle import (
    coco_rle_area,
    coco_rle_bbox_norm,
    validate_coco_rle,
)


def _sanitize_export_geometry(
    geometry: dict | None,
    allowed_attribute_keys: set[str],
) -> dict:
    if not isinstance(geometry, dict):
        return {}
    if geometry.get("type") != "video_track_bbox":
        return dict(geometry)

    changed = False
    keyframes = []
    for kf in geometry.get("keyframes") or []:
        if not isinstance(kf, dict):
            keyframes.append(kf)
            continue
        next_kf = dict(kf)
        if isinstance(kf.get("attributes"), dict):
            next_attrs = sanitize_annotation_attributes(
                kf.get("attributes"),
                allowed_attribute_keys,
            )
            if next_attrs:
                next_kf["attributes"] = next_attrs
            else:
                next_kf.pop("attributes", None)
            changed = changed or next_attrs != kf.get("attributes")
        keyframes.append(next_kf)

    if not changed:
        return dict(geometry)
    return {**geometry, "keyframes": keyframes}


def _export_annotation_copy(
    ann: Annotation,
    allowed_attribute_keys: set[str],
) -> Annotation:
    data = {
        column.name: getattr(ann, column.name)
        for column in Annotation.__table__.columns
    }
    data["attributes"] = sanitize_annotation_attributes(
        ann.attributes,
        allowed_attribute_keys,
    )
    data["geometry"] = _sanitize_export_geometry(
        ann.geometry,
        allowed_attribute_keys,
    )
    return cast(Annotation, SimpleNamespace(**data))


async def _collect_portable_mask_objects(
    geometry: dict,
    mask_objects: dict[str, dict],
) -> None:
    """Load each referenced RLE once for an AAP portable envelope."""
    for reference in collect_mask_references(geometry):
        digest = str(reference.get("sha256") or "")
        if digest and digest not in mask_objects:
            mask_objects[digest] = await load_coco_rle(reference)


IMG_W, IMG_H = 1920, 1280


class UnsupportedExportError(ValueError):
    pass


class VideoBoundaryExportError(UnsupportedExportError):
    def __init__(self, detail: dict):
        super().__init__(str(detail))
        self.detail = detail


def _assert_image_export_supported(project: Project, export_format: str) -> None:
    # v0.10.28 · 媒体维度判断改用 data_type; video 子类型路由 (video-track vs
    # video-mm) 仍用 type_key.
    if project.data_type == "video":
        if project.type_key == "video-track":
            raise UnsupportedExportError(
                f"video-track projects do not support {export_format.upper()} export yet"
            )
        raise UnsupportedExportError(
            f"Video annotation export is not supported for {project.type_key} projects"
        )


def _bbox_geometry(annotation: Annotation) -> dict | None:
    geometry = annotation.geometry or {}
    if geometry.get("type") not in {"bbox", None}:
        return None
    if not all(k in geometry for k in ("x", "y", "w", "h")):
        return None
    return geometry


def _video_metadata(item: DatasetItem | None) -> dict:
    if not item:
        return {}
    metadata = item.metadata_ or {}
    video = metadata.get("video")
    return video if isinstance(video, dict) else {}


def _clean_video_single_frame_geometry(geometry: dict) -> dict:
    """单帧视频几何 → video_json 行。与 track 侧 ``clean_keyframe`` 对称：
    按存在的形状键保留 —— polygon/polyline 出 ``points``，bbox 出 ``bbox``。

    ``video_json`` 是平台自有的保真格式，故这里不降级为外接框（bbox-only 的
    MOT/KITTI/YOLO 才降级，见 ``export_video.single_frame_bbox``）。
    """
    row: dict = {
        "type": geometry.get("type"),
        "frame_index": int(geometry.get("frame_index", 0)),
    }
    if geometry.get("type") == "video_keypoint":
        row["points"] = [dict(point) for point in (geometry.get("points") or [])]
    elif geometry.get("type") == "video_rotated_bbox":
        row.update(
            {key: geometry.get(key, 0) for key in ("cx", "cy", "w", "h", "angle")}
        )
    elif geometry.get("points") is not None:
        row["points"] = [list(pt) for pt in (geometry.get("points") or [])]
    else:
        row["bbox"] = {
            "x": geometry.get("x", 0),
            "y": geometry.get("y", 0),
            "w": geometry.get("w", 0),
            "h": geometry.get("h", 0),
        }
    return row


# ── v0.10.43 · 多几何 → COCO 写入 helper（坐标归一化 [0,1]，写入时去归一化到像素） ──


def _aabb_from_rings(
    rings: list[list[list[float]]],
) -> tuple[float, float, float, float] | None:
    """从若干环顶点求归一化外接框 (x, y, w, h)。"""
    xs: list[float] = []
    ys: list[float] = []
    for ring in rings:
        for pt in ring:
            if len(pt) >= 2:
                xs.append(pt[0])
                ys.append(pt[1])
    if not xs:
        return None
    return min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)


def _coco_aabb_norm(geometry: dict) -> tuple[float, float, float, float] | None:
    """归一化外接框 (x,y,w,h)，覆盖 COCO 消费的几何：bbox/polygon/multi_polygon/keypoint。

    rotated_bbox / polyline 不进 COCO（各有专属目标 / 仅 AAP），返回 None 表示跳过。
    """
    t = geometry.get("type")
    if t in ("bbox", None):
        if not all(k in geometry for k in ("x", "y", "w", "h")):
            return None
        return geometry["x"], geometry["y"], geometry["w"], geometry["h"]
    if t == "polygon":
        return _aabb_from_rings([geometry.get("points") or []])
    if t == "multi_polygon":
        rings = [p.get("points") or [] for p in geometry.get("polygons") or []]
        return _aabb_from_rings(rings)
    if t == "keypoint":
        pts = [
            [p["x"], p["y"]]
            for p in geometry.get("points") or []
            if isinstance(p, dict) and int(p.get("v", 0)) > 0
        ]
        return _aabb_from_rings([pts]) if pts else None
    return None


def _flatten_ring(points: list[list[float]], w: int, h: int) -> list[float]:
    out: list[float] = []
    for pt in points:
        if len(pt) >= 2:
            out.append(round(pt[0] * w, 2))
            out.append(round(pt[1] * h, 2))
    return out


def _coco_segmentation(geometry: dict, w: int, h: int) -> list | None:
    """polygon / multi_polygon → COCO segmentation（多边形顶点像素坐标）。

    仅取外环（holes 多连通孔洞还原留作触发，见计划 §5）。
    """
    t = geometry.get("type")
    if t == "polygon":
        ring = _flatten_ring(geometry.get("points") or [], w, h)
        return [ring] if ring else None
    if t == "multi_polygon":
        segs = [
            _flatten_ring(p.get("points") or [], w, h)
            for p in geometry.get("polygons") or []
        ]
        segs = [s for s in segs if s]
        return segs or None
    return None


def _coco_keypoints(geometry: dict, w: int, h: int) -> tuple[list[float], int] | None:
    """keypoint 实例 → COCO (keypoints[x,y,v,...], num_keypoints)。"""
    if geometry.get("type") != "keypoint":
        return None
    flat: list[float] = []
    n = 0
    for p in geometry.get("points") or []:
        if not isinstance(p, dict):
            continue
        v = int(p.get("v", 0))
        flat.extend(
            [round(float(p.get("x", 0)) * w, 2), round(float(p.get("y", 0)) * h, 2), v]
        )
        if v > 0:
            n += 1
    return (flat, n) if flat else None


class ExportService:
    def __init__(
        self,
        db: AsyncSession,
        *,
        task_ids: list[uuid.UUID] | None = None,
    ) -> None:
        self.db = db
        self.task_ids = task_ids

    async def _load_data(
        self,
        project_id: uuid.UUID,
        batch_id: uuid.UUID | None = None,
        *,
        video_scope: VideoExportScope | None = None,
    ):
        project = await self.db.get(Project, project_id)
        if not project:
            return None, [], []

        task_q = select(Task).where(Task.project_id == project_id)
        if batch_id:
            task_q = task_q.where(Task.batch_id == batch_id)
        if self.task_ids is not None:
            task_q = task_q.where(Task.id.in_(self.task_ids))
        if video_scope is not None:
            task_q = task_q.where(Task.id == video_scope.task_id)
        task_q = task_q.order_by(Task.sequence_order, Task.created_at, Task.id)
        tasks_result = await self.db.execute(task_q)
        tasks = list(tasks_result.scalars().all())

        task_ids = [t.id for t in tasks]
        if not task_ids:
            return project, [], []

        ann_q = select(Annotation).where(
            Annotation.project_id == project_id,
            Annotation.is_active.is_(True),
            Annotation.was_cancelled.is_(False),
        )
        ann_q = ann_q.where(Annotation.task_id.in_(task_ids))
        ann_q = ann_q.order_by(Annotation.created_at, Annotation.id)
        annotations_result = await self.db.execute(ann_q)
        annotations = list(annotations_result.scalars().all())
        class_names = set(derive_classes_list(project.tool_bindings))
        attribute_keys = derive_attribute_keys(project.tool_bindings)
        exported_annotations = []
        for ann in annotations:
            if ann.class_name not in class_names:
                continue
            exported = _export_annotation_copy(ann, attribute_keys)
            exported_annotations.append(exported)

        if project.data_type == "video":
            from app.services.video_canonical import (
                VideoBoundaryUnreconciledError,
                canonical_task_annotations,
            )

            by_task: dict[uuid.UUID, list[Annotation]] = {}
            for annotation in exported_annotations:
                by_task.setdefault(annotation.task_id, []).append(annotation)
            projected: list[Annotation] = []
            try:
                for task in tasks:
                    projected.extend(
                        await canonical_task_annotations(
                            self.db,
                            project=project,
                            task=task,
                            annotations=by_task.get(task.id, []),
                            scope=(
                                video_scope
                                if video_scope is not None
                                and video_scope.task_id == task.id
                                else None
                            ),
                        )
                    )
            except VideoBoundaryUnreconciledError as exc:
                raise VideoBoundaryExportError(exc.detail) from exc
            exported_annotations = projected
        elif video_scope is not None:
            scoped_annotations = []
            for exported in exported_annotations:
                geometry = clip_video_geometry(exported.geometry, video_scope)
                if geometry is not None:
                    exported.geometry = geometry
                    scoped_annotations.append(exported)
            exported_annotations = scoped_annotations

        return project, tasks, exported_annotations

    async def _load_predictions(
        self, project_id: uuid.UUID, task_ids: list[uuid.UUID]
    ) -> list[Prediction]:
        """v0.10.15 · AAP JSON 导出需要 predictions[] 双数组."""
        if not task_ids:
            return []
        pred_q = (
            select(Prediction)
            .where(
                Prediction.project_id == project_id,
                Prediction.task_id.in_(task_ids),
            )
            .order_by(Prediction.created_at, Prediction.id)
        )
        result = await self.db.execute(pred_q)
        return list(result.scalars().all())

    async def _load_dataset_items(
        self, tasks: list[Task]
    ) -> dict[uuid.UUID, DatasetItem]:
        item_ids = [t.dataset_item_id for t in tasks if t.dataset_item_id]
        if not item_ids:
            return {}
        result = await self.db.execute(
            select(DatasetItem).where(DatasetItem.id.in_(item_ids))
        )
        return {item.id: item for item in result.scalars().all()}

    async def _axis_convention_by_task(
        self,
        tasks: list[Task],
    ) -> dict[uuid.UUID, str | None]:
        dataset_items = await self._load_dataset_items(tasks)
        dataset_ids = {
            item.dataset_id
            for item in dataset_items.values()
            if item.dataset_id is not None
        }
        if not dataset_ids:
            return {}
        result = await self.db.execute(
            select(Dataset).where(Dataset.id.in_(dataset_ids))
        )
        datasets = {ds.id: ds for ds in result.scalars().all()}
        out: dict[uuid.UUID, str | None] = {}
        for task in tasks:
            if not task.dataset_item_id:
                continue
            item = dataset_items.get(task.dataset_item_id)
            if item is None:
                continue
            ds = datasets.get(item.dataset_id)
            out[task.id] = (ds.metadata_ or {}).get("axis_convention") if ds else None
        return out

    async def iter_export_chunks(
        self,
        project_id: uuid.UUID,
        batch_id: uuid.UUID | None = None,
        *,
        chunk_size: int = 1000,
        with_annotations: bool = True,
        video_scope: VideoExportScope | None = None,
    ) -> AsyncIterator[
        tuple[
            list[Task], dict[uuid.UUID, list[Annotation]], dict[uuid.UUID, DatasetItem]
        ]
    ]:
        """v0.12.1 · B6-1 · 按 task 分块惰性产出 (tasks, ann_by_task, dataset_items)。

        导出 per-file 格式（YOLO 镜像 / 视频逐序列）的内存与 task 数解耦：不再
        `_load_data` 全量 `list().all()`，而是先取**轻量 task id 列表**（十万 UUID ≈ 1.6MB，
        可忽略），再按 chunk_size 水合整行 Task + 该块 annotation + dataset_item，逐块 yield。

        不用 `db.stream()` 服务端游标：游标占用连接，迭代中再发 annotation/item 查询会
        在同一连接上冲突（"another operation in progress"）。id 列表 + 分块水合规避该坑，
        与 v0.12.0 split 分块同构。

        annotation 已做 export 副本 + class 过滤（与 `_load_data` 语义一致）；几何/格式维度的
        分组留给打包层（YOLO bbox vs 视频 track/bbox）。
        """
        project = await self.db.get(Project, project_id)
        if project is None:
            return
        class_names = set(derive_classes_list(project.tool_bindings))
        attribute_keys = derive_attribute_keys(project.tool_bindings)

        id_q = select(Task.id).where(Task.project_id == project_id)
        if batch_id:
            id_q = id_q.where(Task.batch_id == batch_id)
        if self.task_ids is not None:
            id_q = id_q.where(Task.id.in_(self.task_ids))
        if video_scope is not None:
            id_q = id_q.where(Task.id == video_scope.task_id)
        id_q = id_q.order_by(Task.sequence_order, Task.created_at, Task.id)
        all_ids = [row[0] for row in (await self.db.execute(id_q)).all()]

        for start in range(0, len(all_ids), chunk_size):
            chunk_ids = all_ids[start : start + chunk_size]
            task_rows = (
                (
                    await self.db.execute(
                        select(Task)
                        .where(Task.id.in_(chunk_ids))
                        .order_by(Task.sequence_order, Task.created_at, Task.id)
                    )
                )
                .scalars()
                .all()
            )
            tasks = list(task_rows)

            ann_by_task: dict[uuid.UUID, list[Annotation]] = {}
            if with_annotations:
                ann_q = (
                    select(Annotation)
                    .where(
                        Annotation.project_id == project_id,
                        Annotation.is_active.is_(True),
                        Annotation.was_cancelled.is_(False),
                        Annotation.task_id.in_(chunk_ids),
                    )
                    .order_by(Annotation.created_at, Annotation.id)
                )
                for ann in (await self.db.execute(ann_q)).scalars().all():
                    if ann.class_name not in class_names:
                        continue
                    exported = _export_annotation_copy(ann, attribute_keys)
                    ann_by_task.setdefault(ann.task_id, []).append(exported)

                if project.data_type == "video":
                    from app.services.video_canonical import (
                        VideoBoundaryUnreconciledError,
                        canonical_task_annotations,
                    )

                    try:
                        for task in tasks:
                            ann_by_task[task.id] = await canonical_task_annotations(
                                self.db,
                                project=project,
                                task=task,
                                annotations=ann_by_task.get(task.id, []),
                                scope=(
                                    video_scope
                                    if video_scope is not None
                                    and video_scope.task_id == task.id
                                    else None
                                ),
                            )
                    except VideoBoundaryUnreconciledError as exc:
                        raise VideoBoundaryExportError(exc.detail) from exc
                elif video_scope is not None:
                    for task_id, rows in list(ann_by_task.items()):
                        scoped_rows = []
                        for exported in rows:
                            geometry = clip_video_geometry(
                                exported.geometry, video_scope
                            )
                            if geometry is not None:
                                exported.geometry = geometry
                                scoped_rows.append(exported)
                        ann_by_task[task_id] = scoped_rows

            dataset_items = await self._load_dataset_items(tasks)
            yield tasks, ann_by_task, dataset_items
            # v0.12.1 · 释放 session 身份映射，否则分块加载的 Task/Annotation/DatasetItem
            # ORM 行会全部滞留 identity map，内存仍随 task 数线性增长（B6-1 失效）。
            # 消费方在本轮已写盘完成；expunge 后已加载属性仍可读（expire_on_commit=False），
            # 不影响 dataset_items_all 等后续引用。
            self.db.expunge_all()

    async def export_video_tracks(
        self,
        project_id: uuid.UUID,
        *,
        batch_id: uuid.UUID | None = None,
        include_attributes: bool = True,
        video_frame_mode: str = "keyframes",
        video_scope: VideoExportScope | None = None,
    ) -> str:
        if video_frame_mode not in VIDEO_FRAME_MODES:
            raise UnsupportedExportError(
                "video_frame_mode must be one of: keyframes, all_frames"
            )

        project, tasks, annotations = await self._load_data(
            project_id, batch_id, video_scope=video_scope
        )
        if not project:
            return json.dumps({})
        if project.type_key != "video-track":
            raise UnsupportedExportError(
                "Video JSON export is only supported for video-track projects"
            )

        dataset_items = await self._load_dataset_items(tasks)
        task_by_id = {task.id: task for task in tasks}
        categories = [
            {"id": i, "name": name}
            for i, name in enumerate(derive_classes_list(project.tool_bindings))
        ]

        exported_tasks = []
        video_metadata_by_task: dict[uuid.UUID, dict] = {}
        for index, task in enumerate(tasks):
            item = (
                dataset_items.get(task.dataset_item_id)
                if task.dataset_item_id
                else None
            )
            video = _video_metadata(item)
            video_metadata_by_task[task.id] = video
            exported_tasks.append(
                {
                    "id": str(task.id),
                    "display_id": task.display_id,
                    "file_name": task.file_name,
                    "file_path": task.file_path,
                    "file_type": task.file_type,
                    "sequence_order": task.sequence_order,
                    "batch_id": str(task.batch_id) if task.batch_id else None,
                    "video_metadata": video,
                    "order": index,
                }
            )

        tracks = []
        flattened_keyframes = []
        legacy_video_bbox = []
        for ann in annotations:
            task = task_by_id.get(ann.task_id)
            if not task:
                continue
            geometry = ann.geometry or {}
            if geometry.get("type") in VIDEO_TRACK_GEOMETRY_TYPES:
                keyframes = [
                    clean_keyframe(kf, include_attributes=include_attributes)
                    for kf in sorted_keyframes(geometry)
                ]
                track = {
                    "geometry_type": geometry.get("type"),
                    "annotation_id": str(ann.id),
                    "task_id": str(ann.task_id),
                    "task_display_id": task.display_id,
                    "track_id": geometry.get("track_id"),
                    "class_name": ann.class_name,
                    "source": ann.source,
                    "confidence": ann.confidence,
                    "keyframes": keyframes,
                    "outside": normalize_outside_ranges(geometry.get("outside") or []),
                }
                if include_attributes:
                    track["attributes"] = ann.attributes or {}
                if video_frame_mode == "all_frames":
                    max_keyframe = max(
                        (kf["frame_index"] for kf in keyframes),
                        default=0,
                    )
                    frame_count = int(
                        video_metadata_by_task.get(ann.task_id, {}).get(
                            "frame_count", max_keyframe + 1
                        )
                        or max_keyframe + 1
                    )
                    frame_count = max(frame_count, max_keyframe + 1)
                    if video_scope is None:
                        track["frames"] = resolved_track_frames(
                            geometry,
                            frame_mode="all_frames",
                            frame_count=frame_count,
                        )
                    else:
                        track["frames"] = [
                            resolved
                            for frame_index in range(
                                video_scope.from_frame, video_scope.to_frame + 1
                            )
                            if (
                                resolved := resolve_track_at_frame(
                                    geometry, frame_index
                                )
                            )
                        ]
                tracks.append(track)
                for kf in keyframes:
                    flattened_keyframes.append(
                        {
                            "annotation_id": str(ann.id),
                            "task_id": str(ann.task_id),
                            "track_id": geometry.get("track_id"),
                            "class_name": ann.class_name,
                            **kf,
                        }
                    )
            elif geometry.get("type") in VIDEO_LOSSLESS_SINGLE_FRAME_GEOMETRY_TYPES:
                row: dict = {
                    "annotation_id": str(ann.id),
                    "task_id": str(ann.task_id),
                    "task_display_id": task.display_id,
                    "class_name": ann.class_name,
                    "source": ann.source,
                    **_clean_video_single_frame_geometry(geometry),
                }
                if include_attributes:
                    row["attributes"] = ann.attributes or {}
                legacy_video_bbox.append(row)

        project_row = {
            "id": str(project.id),
            "display_id": project.display_id,
            "name": project.name,
            "type_key": project.type_key,
        }
        if include_attributes:
            project_row["attribute_schema"] = derive_attribute_schema(
                project.tool_bindings
            )

        payload = {
            "export_type": "video_tracks",
            "exported_at": datetime.utcnow().isoformat(),
            "frame_mode": video_frame_mode,
            "project": project_row,
            "categories": categories,
            "tasks": exported_tasks,
            "tracks": tracks,
            "keyframes": flattened_keyframes,
            "video_bbox": legacy_video_bbox,
            "video_metadata": {
                str(task_id): metadata
                for task_id, metadata in video_metadata_by_task.items()
            },
        }
        if video_scope is not None:
            payload["export_scope"] = video_scope.as_dict()
        return json.dumps(payload, ensure_ascii=False, indent=2)

    async def export_coco(
        self,
        project_id: uuid.UUID,
        *,
        batch_id: uuid.UUID | None = None,
        include_attributes: bool = True,
        video_frame_mode: str = "keyframes",
        dataset_items: dict[uuid.UUID, DatasetItem] | None = None,
        axis_frame: AxisFrame = "iso",
        video_scope: VideoExportScope | None = None,
    ) -> str:
        project, tasks, annotations = await self._load_data(
            project_id, batch_id, video_scope=video_scope
        )
        if not project:
            return json.dumps({})
        if project.type_key == "video-track":
            return await self.export_video_tracks(
                project_id,
                batch_id=batch_id,
                include_attributes=include_attributes,
                video_frame_mode=video_frame_mode,
                video_scope=video_scope,
            )
        _assert_image_export_supported(project, "coco")

        # v0.10.17 · COCO categories 按 tool_unit 分组. tool_bindings 提供 N 个工具
        # 单位, 每单位贡献一段 categories; supercategory = tool_unit_id; category.id
        # 全局唯一. 同名类不去重 (强隔离). cat_map 由 (tool_unit_id, class_name) → id 查.
        # 兼容: tool_bindings 为空 (理论上不应出现, 全部已 backfill) 时派生空类集.
        categories: list[dict] = []
        cat_map: dict[tuple[str, str], int] = {}
        tb = project.tool_bindings or {}
        next_cat_id = 0
        if tb:
            for unit_id, binding in tb.items():
                if not isinstance(binding, dict) or not binding.get("enabled"):
                    continue
                # v0.10.43 · keypoint 单元的类别附 COCO keypoints(节点名) + skeleton(edges,
                # COCO 1-indexed)，直接派生自 ToolBinding.keypoint_schema（v0.10.28 已落）。
                kp_names: list[str] | None = None
                kp_skeleton: list[list[int]] | None = None
                if unit_id == "keypoint":
                    kp_schema = binding.get("keypoint_schema") or {}
                    nodes = kp_schema.get("nodes") or []
                    # 有 sublabel 时拼成 name_sublabel，保证 COCO keypoints 名唯一可辨。
                    names = [
                        f"{n['name']}_{n['sublabel']}"
                        if n.get("sublabel")
                        else n["name"]
                        for n in nodes
                        if isinstance(n, dict) and n.get("name")
                    ]
                    if names:
                        kp_names = names
                        kp_skeleton = [
                            [int(e[0]) + 1, int(e[1]) + 1]
                            for e in kp_schema.get("edges") or []
                            if len(e) == 2
                        ]
                for cls in binding.get("classes") or []:
                    if not isinstance(cls, dict):
                        continue
                    name = cls.get("name")
                    if not name:
                        continue
                    cat: dict = {
                        "id": next_cat_id,
                        "name": name,
                        "supercategory": unit_id,
                    }
                    if kp_names:
                        cat["keypoints"] = kp_names
                        cat["skeleton"] = kp_skeleton or []
                    categories.append(cat)
                    cat_map[(unit_id, name)] = next_cat_id
                    next_cat_id += 1
        else:
            for name in derive_classes_list(project.tool_bindings):
                categories.append(
                    {"id": next_cat_id, "name": name, "supercategory": "bbox"}
                )
                cat_map[("bbox", name)] = next_cat_id
                next_cat_id += 1

        def _find_category_id(ann_obj) -> int:
            unit = getattr(ann_obj, "tool_unit_id", None) or "bbox"
            cid = cat_map.get((unit, ann_obj.class_name))
            if cid is not None:
                return cid
            # 兜底: 强隔离同名不存在时, 跨 unit 同名取第一个; 都没有就 0.
            for (_, n), v in cat_map.items():
                if n == ann_obj.class_name:
                    return v
            return 0

        # v0.10.27 · 像素坐标改用 DatasetItem.width/height 真值, 缺失再回退常量。
        items = (
            dataset_items
            if dataset_items is not None
            else await self._load_dataset_items(tasks)
        )

        def _dims_for(task: Task) -> tuple[int, int]:
            item = items.get(task.dataset_item_id) if task.dataset_item_id else None
            w = item.width if item and item.width else IMG_W
            h = item.height if item and item.height else IMG_H
            return w, h

        images = []
        dims_by_task: dict[uuid.UUID, tuple[int, int]] = {}
        for i, t in enumerate(tasks):
            w, h = _dims_for(t)
            dims_by_task[t.id] = (w, h)
            images.append(
                {
                    "id": i,
                    "file_name": t.file_name,
                    "width": w,
                    "height": h,
                }
            )
        task_id_to_img_id = {t.id: i for i, t in enumerate(tasks)}

        coco_annotations = []
        skipped = 0
        for ann in annotations:
            img_id = task_id_to_img_id.get(ann.task_id)
            if img_id is None:
                continue
            g = ann.geometry or {}
            img_w, img_h = dims_by_task.get(ann.task_id, (IMG_W, IMG_H))
            raster_rle: dict | None = None
            if g.get("type") == "raster_mask":
                raster_rle = await load_coco_rle(g.get("mask") or {})
                rle_h, rle_w, _ = validate_coco_rle(raster_rle)
                if (rle_w, rle_h) != (img_w, img_h):
                    raise ValueError(
                        "raster mask size does not match COCO image dimensions"
                    )
                aabb = coco_rle_bbox_norm(raster_rle)
                x_px = aabb.get("x", 0.0) * img_w
                y_px = aabb.get("y", 0.0) * img_h
                w_px = aabb.get("w", 0.0) * img_w
                h_px = aabb.get("h", 0.0) * img_h
                area = coco_rle_area(raster_rle)
            else:
                aabb = _coco_aabb_norm(g)
                if aabb is None:
                    # rotated_bbox / polyline / 空几何不进 COCO（各有专属目标）。
                    skipped += 1
                    continue
                x_px = aabb[0] * img_w
                y_px = aabb[1] * img_h
                w_px = aabb[2] * img_w
                h_px = aabb[3] * img_h
                area = w_px * h_px
            row = {
                "id": len(coco_annotations),
                "image_id": img_id,
                "category_id": _find_category_id(ann),
                "bbox": [
                    round(x_px, 2),
                    round(y_px, 2),
                    round(w_px, 2),
                    round(h_px, 2),
                ],
                "area": round(area, 2),
                # COCO uses iscrowd=1 for RLE segmentation and 0 for polygons.
                "iscrowd": 1 if raster_rle is not None else 0,
            }
            if raster_rle is not None:
                row["segmentation"] = compress_coco_rle(raster_rle)
            else:
                seg = _coco_segmentation(g, img_w, img_h)
                if seg:
                    row["segmentation"] = seg
            kp = _coco_keypoints(g, img_w, img_h)
            if kp is not None:
                row["keypoints"], row["num_keypoints"] = kp
            if include_attributes:
                attrs = dict(ann.attributes or {})
                # v0.21.2 · ADR-0045 · 跨帧同一对象标识 → COCO attributes.__track_id
                # (原 group_id 高位段 __group_id; 跨帧语义已统一到 track_id)。
                if getattr(ann, "track_id", None) is not None:
                    attrs["__track_id"] = ann.track_id
                row["attributes"] = attrs
            coco_annotations.append(row)

        info = {
            "description": project.name,
            "version": "1.0",
            "date_created": datetime.utcnow().isoformat(),
            "skipped_annotations": skipped,
        }
        if include_attributes:
            info["attribute_schema"] = derive_attribute_schema(project.tool_bindings)
        if axis_frame != "iso":
            info["axis_frame"] = axis_frame

        coco = {
            "info": info,
            "images": images,
            "annotations": coco_annotations,
            "categories": categories,
        }
        return json.dumps(coco, ensure_ascii=False, indent=2)

    async def export_yolo(
        self,
        project_id: uuid.UUID,
        *,
        batch_id: uuid.UUID | None = None,
        include_attributes: bool = True,
    ) -> bytes:
        project, tasks, annotations = await self._load_data(project_id, batch_id)
        if not project:
            return b""
        _assert_image_export_supported(project, "yolo")

        yolo_classes = derive_classes_list(project.tool_bindings)
        cat_map = {name: i for i, name in enumerate(yolo_classes)}
        ann_by_task: dict[uuid.UUID, list[Annotation]] = {}
        for ann in annotations:
            ann_by_task.setdefault(ann.task_id, []).append(ann)

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            classes_txt = "\n".join(yolo_classes)
            zf.writestr("classes.txt", classes_txt)

            if include_attributes:
                # 包内根目录写一份属性 schema，下游训练 ingest 可解析
                zf.writestr(
                    "attribute_schema.json",
                    json.dumps(
                        derive_attribute_schema(project.tool_bindings),
                        ensure_ascii=False,
                        indent=2,
                    ),
                )

            for t in tasks:
                lines = []
                attrs_per_line: list[dict] = []
                for ann in ann_by_task.get(t.id, []):
                    g = _bbox_geometry(ann)
                    if g is None:
                        continue
                    cx = g["x"] + g["w"] / 2
                    cy = g["y"] + g["h"] / 2
                    cid = cat_map.get(ann.class_name, 0)
                    lines.append(f"{cid} {cx:.6f} {cy:.6f} {g['w']:.6f} {g['h']:.6f}")
                    if include_attributes:
                        attrs_per_line.append(ann.attributes or {})

                base = t.file_name.rsplit(".", 1)[0]
                zf.writestr(f"labels/{base}.txt", "\n".join(lines))
                if include_attributes and attrs_per_line:
                    # 伴生属性文件：行索引与 .txt 行号对齐
                    zf.writestr(
                        f"labels/{base}.attrs.json",
                        json.dumps({"attributes": attrs_per_line}, ensure_ascii=False),
                    )

        return buf.getvalue()

    async def export_voc(
        self,
        project_id: uuid.UUID,
        *,
        batch_id: uuid.UUID | None = None,
        include_attributes: bool = True,
    ) -> bytes:
        project, tasks, annotations = await self._load_data(project_id, batch_id)
        if not project:
            return b""
        _assert_image_export_supported(project, "voc")

        ann_by_task: dict[uuid.UUID, list[Annotation]] = {}
        for ann in annotations:
            ann_by_task.setdefault(ann.task_id, []).append(ann)

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for t in tasks:
                root = Element("annotation")
                SubElement(root, "filename").text = t.file_name
                size = SubElement(root, "size")
                SubElement(size, "width").text = str(IMG_W)
                SubElement(size, "height").text = str(IMG_H)
                SubElement(size, "depth").text = "3"

                for ann in ann_by_task.get(t.id, []):
                    g = _bbox_geometry(ann)
                    if g is None:
                        continue
                    obj = SubElement(root, "object")
                    SubElement(obj, "name").text = ann.class_name
                    SubElement(obj, "difficult").text = "0"
                    bndbox = SubElement(obj, "bndbox")
                    SubElement(bndbox, "xmin").text = str(round(g["x"] * IMG_W))
                    SubElement(bndbox, "ymin").text = str(round(g["y"] * IMG_H))
                    SubElement(bndbox, "xmax").text = str(
                        round((g["x"] + g["w"]) * IMG_W)
                    )
                    SubElement(bndbox, "ymax").text = str(
                        round((g["y"] + g["h"]) * IMG_H)
                    )
                    if include_attributes and ann.attributes:
                        extra = SubElement(obj, "extra")
                        for k, v in ann.attributes.items():
                            SubElement(extra, str(k)).text = (
                                str(v) if v is not None else ""
                            )

                xml_name = t.file_name.rsplit(".", 1)[0] + ".xml"
                zf.writestr(
                    f"Annotations/{xml_name}", tostring(root, encoding="unicode")
                )

        return buf.getvalue()

    async def export_aap_json(
        self,
        project_id: uuid.UUID,
        *,
        batch_id: uuid.UUID | None = None,
        axis_frame: AxisFrame = "iso",
        video_scope: VideoExportScope | None = None,
    ) -> str:
        """v0.10.15 · AAP JSON v1.0 无损中间格式.

        与 COCO/YOLO/VOC 并列, 但**包含**它们丢失的字段: attribute_schema 值、
        prediction.confidence、annotation.source、annotation_guide、classes_config.
        双数组 annotations[] / predictions[] 分开 (不混 type 字段).
        """
        project, tasks, annotations = await self._load_data(
            project_id, batch_id, video_scope=video_scope
        )
        if not project:
            return json.dumps({})

        task_ids = [t.id for t in tasks]
        predictions = await self._load_predictions(project_id, task_ids)

        ann_by_task: dict[uuid.UUID, list[Annotation]] = {}
        for ann in annotations:
            ann_by_task.setdefault(ann.task_id, []).append(ann)
        pred_by_task: dict[uuid.UUID, list[Prediction]] = {}
        for pred in predictions:
            pred_by_task.setdefault(pred.task_id, []).append(pred)

        exported_scene_track_ids = {
            ann.scene_track_id for ann in annotations if ann.scene_track_id is not None
        }
        scene_track_entries: list[AAPSceneTrackEntry] = []
        if exported_scene_track_ids:
            scene_track_rows = (
                await self.db.execute(
                    select(SceneTrack, Scene.name)
                    .join(Scene, Scene.id == SceneTrack.scene_id)
                    .where(SceneTrack.id.in_(exported_scene_track_ids))
                    .order_by(Scene.name, SceneTrack.track_id)
                )
            ).all()
            interval_rows = list(
                (
                    await self.db.execute(
                        select(SceneTrackInterval)
                        .where(
                            SceneTrackInterval.scene_track_id.in_(
                                exported_scene_track_ids
                            )
                        )
                        .order_by(
                            SceneTrackInterval.scene_track_id,
                            SceneTrackInterval.start_frame,
                        )
                    )
                ).scalars()
            )
            intervals_by_track: dict[uuid.UUID, list[SceneTrackInterval]] = {}
            for interval in interval_rows:
                intervals_by_track.setdefault(interval.scene_track_id, []).append(
                    interval
                )
            scene_track_entries = [
                AAPSceneTrackEntry(
                    track_id=track.track_id,
                    scene_name=scene_name,
                    class_name=track.class_name,
                    presence_mode=track.presence_mode,
                    attributes=track.attributes or {},
                    attributes_meta=track.attributes_meta or {},
                    intervals=[
                        AAPSceneTrackInterval(
                            start_frame=interval.start_frame,
                            end_frame=interval.end_frame,
                            source=interval.source,
                        )
                        for interval in intervals_by_track.get(track.id, [])
                    ],
                )
                for track, scene_name in scene_track_rows
            ]

        axis_by_task = (
            await self._axis_convention_by_task(tasks) if axis_frame == "source" else {}
        )

        # v0.10.31 · 视频项目: 给每个 task block 填 media_type + video 子块.
        is_video = project.data_type == "video"
        dataset_items = await self._load_dataset_items(tasks) if is_video else {}

        # batch display_id (项目级导出时为 None)
        batch_display_id: str | None = None
        if batch_id:
            from app.db.models.task_batch import TaskBatch  # 懒导入避免循环

            batch = await self.db.get(TaskBatch, batch_id)
            batch_display_id = batch.display_id if batch else None

        task_blocks: list[AAPTaskBlock] = []
        mask_objects: dict[str, dict] = {}
        for t in tasks:
            ann_entries: list[AAPAnnotationEntry] = []
            for ann in ann_by_task.get(t.id, []):
                geometry = transform_box_geometry_axis_frame(
                    ann.geometry or {},
                    dataset_convention=axis_by_task.get(t.id),
                    axis_frame=axis_frame,
                )
                await _collect_portable_mask_objects(geometry, mask_objects)
                ann_entries.append(
                    AAPAnnotationEntry(
                        geometry=geometry,
                        class_name=ann.class_name,
                        # v0.10.17 · 工具维度绑定 (1.1+).
                        tool_unit_id=getattr(ann, "tool_unit_id", None) or "bbox",
                        attributes=ann.attributes or {},
                        confidence=ann.confidence,
                        source=ann.source,
                        track_id=ann.track_id,
                        temporal_role=ann.temporal_role,
                        sensor_role=ann.sensor_role,
                        sensor_visibility=ann.sensor_visibility,
                        calibration_revision=ann.calibration_revision,
                        calibration_digest=ann.calibration_digest,
                        user_id=ann.user_id,
                        created_at=ann.created_at,
                        external_id=None,
                    )
                )

            pred_entries: list[AAPPredictionEntry] = []
            for pred in pred_by_task.get(t.id, []):
                # prediction.result 存 LS shape 数组; 每个 shape 对应一个目标物.
                # 走 to_internal_shape() 反推内部 geometry + class_name.
                pred_unit = getattr(pred, "tool_unit_id", None) or "bbox"
                for raw_shape in pred.result or []:
                    internal = to_internal_shape(raw_shape)
                    geometry = internal.get("geometry") or {}
                    if not geometry:
                        continue
                    geometry = transform_box_geometry_axis_frame(
                        geometry,
                        dataset_convention=axis_by_task.get(t.id),
                        axis_frame=axis_frame,
                    )
                    if video_scope is not None:
                        geometry = clip_video_geometry(geometry, video_scope)
                        if geometry is None:
                            continue
                    await _collect_portable_mask_objects(geometry, mask_objects)
                    pred_entries.append(
                        AAPPredictionEntry(
                            geometry=geometry,
                            class_name=internal.get("class_name") or None,
                            tool_unit_id=pred_unit,
                            confidence=internal.get("confidence"),
                            model_version=pred.model_version,
                            score=pred.score,
                            source=pred.source,
                            created_at=pred.created_at,
                            external_id=None,
                        )
                    )

            media_type = "lidar" if project.data_type == "lidar" else "image"
            video_block: dict | None = None
            if is_video:
                media_type = "video"
                item = (
                    dataset_items.get(t.dataset_item_id) if t.dataset_item_id else None
                )
                vmeta = _video_metadata(item)
                video_block = {
                    "sampling": project.video_sampling or {},
                    "fps": vmeta.get("fps"),
                    "frame_count": vmeta.get("frame_count"),
                    "duration_ms": vmeta.get("duration_ms"),
                    "width": vmeta.get("width"),
                    "height": vmeta.get("height"),
                }
                if video_scope is not None:
                    video_block["export_scope"] = video_scope.as_dict()

            task_blocks.append(
                AAPTaskBlock(
                    task_match=AAPTaskMatch(
                        display_id=t.display_id, file_path=t.file_path
                    ),
                    file_path=t.file_path,
                    media_type=media_type,
                    video=video_block,
                    external_id=None,
                    annotations=ann_entries,
                    predictions=pred_entries,
                )
            )

        envelope = AAPJsonV1Envelope(
            schema_version=AAP_SCHEMA_VERSION,
            exported_at=datetime.utcnow(),
            exported_from=AAPExportedFrom(
                platform="aap",
                platform_version=None,
                project_display_id=project.display_id,
                batch_display_id=batch_display_id,
            ),
            project=AAPProjectMeta(
                name=project.name,
                type_key=project.type_key,
                classes_config=derive_classes_config(project.tool_bindings),
                attribute_schema=derive_attribute_schema(project.tool_bindings),
                # v0.10.17 · 工具维度绑定 (1.1+).
                tool_bindings=project.tool_bindings or {},
                rendering_config=project.rendering_config or {},
                annotation_guide=getattr(project, "annotation_guide", None),
            ),
            mask_objects=mask_objects,
            scene_tracks=scene_track_entries,
            tasks=task_blocks,
        )

        # 严格写满 null: 走 mode="json" + exclude_none=False (默认).
        return envelope.model_dump_json(indent=2, exclude_none=False)
