from __future__ import annotations

import copy
import hashlib
import json
import logging
import uuid
from collections import Counter
from datetime import datetime
from fastapi import HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.annotation import Annotation
from app.db.models.scene_track import SceneTrack
from app.db.models.prediction import (
    INTERACTIVE_ACCEPT_PREDICTION_SOURCE,
    Prediction,
)
from app.db.models.task import Task
from app.db.models.task_lock import AnnotationDraft
from app.services.video_tracks import (
    frame_is_outside,
    normalize_outside_ranges,
    resolved_track_frames,
    resolve_track_at_frame,
    sorted_keyframes,
)

# v0.16.x 拆分：传播纯逻辑簇已抽到 annotation_propagation.py，此处 import 回来给
# 端点/类方法自用，并保持 `from app.services.annotation import ...` 旧入口经 re-export 不变。
from app.services.annotation_propagation import (
    _PropagateContext as _PropagateContext,
    _clean_bbox_geometry as _clean_bbox_geometry,
    _clip_outside_ranges as _clip_outside_ranges,
    _composition_keyframe as _composition_keyframe,
    _new_track_id as _new_track_id,
    _track_visible_keyframes as _track_visible_keyframes,
)
from app.services.annotation_evidence import (
    record_annotation_actor,
    record_annotation_actors_for_tasks,
)
from app.services.annotation_track_identity import prepare_compact_track_identity
from app.services.raster_mask_storage import (
    prepare_mask_geometry_for_annotation_write,
    prepare_mask_payload_for_write,
)
from app.services.scene_track_domain import (
    SceneTrackIntegrityError,
    bind_annotation_to_scene_track,
    ensure_scene_track,
    reclassify_single_member_scene_track,
    touch_annotation_scene_track,
)

logger = logging.getLogger("app.services.annotation")

VIDEO_BBOX_CONVERSION_LIMIT = 5000
ANNOTATION_CREATE_OPERATION_KIND = "create_annotation"


def annotation_create_request_digest(payload: dict) -> str:
    """Return a stable digest for one normalized annotation create request.

    The idempotency key is carried in a header, so it is deliberately excluded
    from this digest.  Pydantic has already normalized the request before the
    endpoint calls this helper; sorting keys keeps retries independent of JSON
    object insertion order.
    """
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def _raise_scene_track_conflict(exc: SceneTrackIntegrityError) -> None:
    raise HTTPException(
        status_code=409,
        detail={"reason": exc.code, "message": str(exc)},
    ) from exc


def validate_geometry_type_transition(
    *,
    tool_unit_id: str,
    previous_geometry: dict | None,
    next_geometry: dict,
) -> None:
    """Reject unsupported geometry type changes without storage or database I/O."""
    previous_type = str((previous_geometry or {}).get("type") or "")
    next_type = str(next_geometry.get("type") or "")
    if previous_type == next_type:
        return
    region_types = {"polygon", "multi_polygon", "raster_mask"}
    if (
        tool_unit_id == "region"
        and previous_type in region_types
        and next_type in region_types
    ):
        return

    from fastapi import HTTPException

    raise HTTPException(
        status_code=422,
        detail={
            "reason": "geometry_type_transition_not_allowed",
            "from_type": previous_type,
            "to_type": next_type,
        },
    )


def _sync_attributes_meta(
    old_attrs: dict | None,
    old_meta: dict | None,
    new_attrs: dict | None,
) -> dict:
    """v0.20.10 · 人工改属性时同步 attributes_meta（键同步是正确性红线）。

    meta 只存 `origin=ai` 的条目；human 属性用「缺省即 human」隐式表达。规则：
    - 某 AI key 的值**未变** → 保留其 meta（人工没动它，仍是 AI 产物）。
    - 某 AI key 被人工**改值** → 删 meta（人工覆盖即认领，回落隐式 human）。
    - 某 key 被**删除**（不在 new_attrs）→ meta 一并消失（不迭代即丢弃）。
    - 新增的人工 key → 无 meta（隐式 human）。
    内部键（`_shape_index` 等 `_` 前缀）不进 meta。
    """
    old_attrs = old_attrs or {}
    old_meta = old_meta or {}
    new_meta: dict = {}
    for key, val in (new_attrs or {}).items():
        if key.startswith("_"):
            continue
        entry = old_meta.get(key)
        if entry is None:
            continue  # 人工产物（隐式 human），不落 meta
        # 该 key 原是 AI 产物：仅当值未被人工改动才保留 AI 溯源。
        if key in old_attrs and old_attrs[key] == val:
            new_meta[key] = entry
    return new_meta


# v0.14.1 · 可跨帧 propagate 的几何类型。视频内 track 几何由 video_tracker_runner
# 处理(case A 内部), point_mask_3d 跨帧 point_indices 无意义(§5.4 留 v0.15+)。
PROPAGATABLE_GEOMETRY_TYPES = frozenset(
    {
        "box_3d",
        "bbox",
        "polygon",
        "multi_polygon",
        "rotated_bbox",
        "polyline",
        "keypoint",
    }
)


class AnnotationService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _validate_class_name(
        self, project_id: uuid.UUID | None, tool_unit_id: str, class_name: str
    ) -> None:
        """v0.10.17 · 软校验: 若 project.tool_bindings 中该 unit 给出了 classes 集合,
        class_name 必须在内. 集合为空 (未配置 / 历史项目) 时放行兼容旧数据.
        create / accept_prediction / accept_all 共用同一段以避免分支漏校验.

        例外: "__unknown" 是前端 (apps/web/.../stage/colors.ts UNKNOWN_CLASS) 的兜底
        sentinel — 用户画完框按 Esc / 点画布外时落该类, 表示"未分类待补", 不属于
        任何 unit 的类别集合, 任意 unit 都放行.
        """
        if project_id is None:
            return
        if class_name == "__unknown":
            return
        from app.db.models.project import Project
        from app.services.project import lookup_classes_for_tool_unit
        from fastapi import HTTPException

        project = await self.db.get(Project, project_id)
        if project is None:
            return
        allowed = lookup_classes_for_tool_unit(
            project.tool_bindings or {}, tool_unit_id
        )
        if allowed and class_name not in allowed:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"class_name '{class_name}' 不在工具单位 "
                    f"'{tool_unit_id}' 的类别集合内"
                ),
            )

    async def _validate_parent_annotation(
        self,
        task_id: uuid.UUID,
        parent_annotation_id: uuid.UUID,
        video_segment_id: uuid.UUID | None = None,
    ) -> None:
        """v0.20.9 · 父子标注仅一层深度约束（应用层校验，不下沉 DB 约束，留后手支持多层）。

        父框必须: 存在且 active、与子框同一 task（父子限帧内）、自身无 parent
        （即父不能再有父 → 只允许一层嵌套）。任一不满足返回 400。
        """
        from fastapi import HTTPException

        # Slice restore may deactivate a prospective parent. Serialize child
        # creation with it before validating, so a waiter cannot create a child
        # from an active-state snapshot taken before the restore committed.
        await self.db.execute(select(Task).where(Task.id == task_id).with_for_update())
        parent = await self.db.scalar(
            select(Annotation)
            .where(Annotation.id == parent_annotation_id)
            .execution_options(populate_existing=True)
        )
        if parent is None or not parent.is_active:
            raise HTTPException(
                status_code=400,
                detail=f"parent annotation {parent_annotation_id} not found or inactive",
            )
        if parent.task_id != task_id:
            raise HTTPException(
                status_code=400,
                detail="parent annotation must belong to the same task (parent-child is frame-internal)",
            )
        if parent.video_segment_id != video_segment_id:
            raise HTTPException(
                status_code=400,
                detail="parent annotation must belong to the same video segment",
            )
        if parent.parent_annotation_id is not None:
            raise HTTPException(
                status_code=400,
                detail="parent-child nesting is limited to one level",
            )

    async def create(
        self,
        task_id: uuid.UUID,
        user_id: uuid.UUID,
        annotation_type: str,
        class_name: str,
        geometry: dict,
        confidence: float | None = None,
        parent_prediction_id: uuid.UUID | None = None,
        parent_annotation_id: uuid.UUID | None = None,
        lead_time: float | None = None,
        attributes: dict | None = None,
        tool_unit_id: str = "bbox",
        video_segment_id: uuid.UUID | None = None,
    ) -> Annotation:
        task = await self.db.get(Task, task_id)
        # A2 · record before any Annotation/SceneTrack lock: this takes the task
        # row lock first, keeps the global Task -> Annotation order, and rolls
        # back with the mutation if the mutation fails.
        if task is not None:
            await record_annotation_actor(self.db, task, user_id)
        source = "prediction_based" if parent_prediction_id else "manual"

        if task and task.project_id:
            await self._validate_class_name(task.project_id, tool_unit_id, class_name)

        if parent_annotation_id is not None:
            await self._validate_parent_annotation(
                task_id, parent_annotation_id, video_segment_id
            )

        await prepare_mask_geometry_for_annotation_write(self.db, task, geometry)
        geometry, track_id = prepare_compact_track_identity(geometry)
        if geometry.get("type") == "box_3d" and track_id is None:
            track_id = _new_track_id()
        annotation = Annotation(
            id=uuid.uuid4(),
            task_id=task_id,
            project_id=task.project_id if task else None,
            video_segment_id=video_segment_id,
            user_id=user_id,
            source=source,
            annotation_type=str(geometry.get("type") or annotation_type),
            tool_unit_id=tool_unit_id,
            class_name=class_name,
            geometry=geometry,
            track_id=track_id,
            confidence=confidence,
            parent_prediction_id=parent_prediction_id,
            parent_annotation_id=parent_annotation_id,
            lead_time=lead_time,
            attributes=attributes or {},
            temporal_role="keyframe" if geometry.get("type") == "box_3d" else "sample",
        )
        self.db.add(annotation)
        if task is not None:
            try:
                await bind_annotation_to_scene_track(
                    self.db,
                    annotation=annotation,
                    task=task,
                    temporal_role="keyframe",
                    interval_source="manual",
                    actor_id=user_id,
                )
            except SceneTrackIntegrityError as exc:
                _raise_scene_track_conflict(exc)
        await self.db.flush()

        await self._update_task_stats(task_id)
        return annotation

    async def accept_prediction(
        self,
        prediction_id: uuid.UUID,
        user_id: uuid.UUID,
        shape_index: int | None = None,
        override_class_name: str | None = None,
        attribute_overrides: dict | None = None,
        video_segment_id: uuid.UUID | None = None,
    ) -> list[Annotation] | None:
        """采纳预测 → 转 annotation。

        - shape_index=None: 采纳整条 prediction 的所有 shape (旧默认, 用于"全部采纳"按钮).
        - shape_index=i:    仅采纳第 i 个 shape (用于画布单点"采纳"按钮, 避免一键采纳波及同 prediction 下其它框).
          每条 annotation 在 attributes 里写入 _shape_index, 让前端能按 (predictionId, shapeIndex) 双键判定.

        返回值 (v0.20.22 契约):
        - 找不到 prediction、交互式采纳溯源快照或 shape_index 越界 → None
          (路由层转 404)。
        - 成功 → 本次新建的 annotation 列表 (单 shape 场景返回 `[ann]`)。
          原实现只返回循环最后一条, 上游 route 忽略返回值、另跑 `list_by_task` 回整题全量,
          导致前端把整题当作"刚新建"逐条 PATCH 合并 AI 候选属性 → 污染人工标注 (改动 1 根因)。
        """
        # v0.10.25 · predictions 复合 PK (id, created_at) 后不能用 db.get(单值)，改按 id 查。
        prediction = (
            await self.db.execute(
                select(Prediction).where(Prediction.id == prediction_id)
            )
        ).scalar_one_or_none()
        if not prediction:
            return None
        if prediction.source == INTERACTIVE_ACCEPT_PREDICTION_SOURCE:
            return None

        # Convert and validate every selected geometry before constructing or adding
        # any Annotation row.  This closes the accept path that previously bypassed
        # the raster create gate and linked uploads immediately before flush.
        from app.services.prediction import to_internal_shape

        raw_shapes = list(prediction.result or [])
        if shape_index is not None:
            if not (0 <= shape_index < len(raw_shapes)):
                return None
            indexed = [(shape_index, to_internal_shape(raw_shapes[shape_index]))]
        else:
            indexed = [
                (index, to_internal_shape(raw_shape))
                for index, raw_shape in enumerate(raw_shapes)
            ]
        task = await self.db.get(Task, prediction.task_id)
        if task is not None:
            # A2 · task row lock before the accepted annotations are written.
            await record_annotation_actor(self.db, task, user_id)
        await prepare_mask_payload_for_write(
            self.db,
            task,
            [shape.get("geometry", {}) for _, shape in indexed],
        )

        # v0.20.10 · 属性级溯源: 从 PredictionMeta.extra.pipeline 建「AI 富集属性键 → model_ref」
        # 映射。pipeline 是 stage 级 (非 per-key), 但 enriched key = f"{label}_{k}" if label else k
        # (与 tasks.py:_run_task_pipeline 一致), 故能精确反推每个键出自哪个 stage 的 backend/model。
        # PredictionMeta 与 prediction 1:1 (prediction_id unique)。confidence 不在 extra 里, 不编造。
        from app.db.models.prediction import PredictionMeta

        ai_key_model: dict[str, dict] = {}
        pred_meta = (
            await self.db.execute(
                select(PredictionMeta).where(
                    PredictionMeta.prediction_id == prediction_id
                )
            )
        ).scalar_one_or_none()
        pipeline_meta = (pred_meta.extra or {}).get("pipeline") if pred_meta else None
        if isinstance(pipeline_meta, dict):
            for st in pipeline_meta.get("stages") or []:
                if not isinstance(st, dict) or st.get("write_target") != "attributes":
                    continue
                label = st.get("label")
                prefix = f"{label}_" if label else ""
                model_ref = {
                    "backend_id": st.get("ml_backend_id"),
                    "model_id": st.get("model_id"),
                }
                for k in st.get("write_keys") or []:
                    ai_key_model[f"{prefix}{k}"] = model_ref

        # v0.9.7 fix · prediction.result 是 LabelStudio 标准, 转内部 schema 后入 annotation
        from app.db.models.project import Project

        # B-11 · DINO 写入的 class_name 是项目类别的英文 alias; 采纳时反查
        # 对应 tool_unit 的 classes 把 alias 映射回原类别名 (中文 / 业务名).
        # v0.10.17 · 强隔离: 只查 prediction.tool_unit_id 对应 unit 的 classes,
        # 不再跨 unit 走派生 classes_config (不同 unit 同名类可有不同 alias).
        project = await self.db.get(Project, prediction.project_id)
        prediction_unit = getattr(prediction, "tool_unit_id", None) or "bbox"
        alias_to_name: dict[str, str] = {}
        # v0.18.0 · select/multiselect 属性的合法取值集合, 采纳时软校验 (不阻断, 仅 warning).
        attr_select_options: dict[str, set[str]] = {}
        if project is not None:
            binding = (project.tool_bindings or {}).get(prediction_unit) or {}
            for cls in binding.get("classes") or []:
                if not isinstance(cls, dict):
                    continue
                alias = cls.get("alias")
                cname = cls.get("name")
                if isinstance(alias, str) and alias.strip() and isinstance(cname, str):
                    alias_to_name[alias.strip().lower()] = cname
            for field in (binding.get("attribute_schema") or {}).get("fields") or []:
                if not isinstance(field, dict) or field.get("type") not in (
                    "select",
                    "multiselect",
                ):
                    continue
                fkey = field.get("key")
                opts = {
                    o.get("value")
                    for o in (field.get("options") or [])
                    if isinstance(o, dict)
                }
                if isinstance(fkey, str) and opts:
                    attr_select_options[fkey] = opts

        anns: list[Annotation] = []
        for idx, shape in indexed:
            raw_class = shape.get("class_name", "") or ""
            # v0.14.17 · 采纳时选类: override_class_name 非空时直接用它 (人工指定项目标签),
            # 跳过 alias 反查 — 用于"预测类名既不在标签集、又无 alias 命中"时让人当场选类落库,
            # 不再 422 拒死。仍走下面同一段软校验。平台不做自动映射 (NG6)。
            mapped_class = (
                override_class_name
                if override_class_name
                else alias_to_name.get(raw_class.strip().lower(), raw_class)
            )
            # v0.10.17 · 走与 create 同一段 class_name 软校验, 避免 accept_prediction 写入
            # 不在 unit allowed 集合内的"幽灵类别"导致 UI 看不见 DB 仍有的记录.
            await self._validate_class_name(
                prediction.project_id, prediction_unit, mapped_class
            )
            # v0.14.9 · 协议 v2 OCR / doc_layout: 把 prediction shape 的富属性 (text /
            # language / orientation) 透传到 annotation.attributes。tool_binding 的
            # attribute_schema 未定义对应 key 时仍原样保留 (不静默丢、不硬校验), 让 OCR
            # 文本随采纳进人工标注。_shape_index 始终保留供前端双键判定。
            attributes: dict = {}
            shape_attributes = shape.get("attributes")
            if isinstance(shape_attributes, dict):
                attributes.update(shape_attributes)
            # v0.18.3 · 采纳前在工作台审阅候选属性时改过的值按键覆盖 (edit-before-accept 原子落库)。
            # 内部键 (_shape_index 等) 不允许被 override 干扰, 下面权威重写。
            if isinstance(attribute_overrides, dict):
                attributes.update(
                    {
                        k: v
                        for k, v in attribute_overrides.items()
                        if not k.startswith("_")
                    }
                )
            # 权威 _shape_index 放在最后, 防止 backend 在 shape attributes 里同名覆盖
            # 导致前端按 (predictionId, shapeIndex) 双键命中错位。
            attributes["_shape_index"] = idx
            # v0.18.0 · 软校验 select/multiselect 属性值: 不在 options 内仍保留入库, 仅记 warning
            # (避免模型抖动产出越界值时整框采纳失败; 硬校验留给人工提交). _shape_index 等内部键跳过.
            for _akey, _aval in attributes.items():
                _allowed = attr_select_options.get(_akey)
                if _allowed is None:
                    continue
                _bad = [
                    v
                    for v in (_aval if isinstance(_aval, list) else [_aval])
                    if v not in _allowed
                ]
                if _bad:
                    logger.warning(
                        "accept_prediction %s shape %s: 属性 %s=%s 不在 select options 内 (保留入库)",
                        prediction_id,
                        idx,
                        _akey,
                        _bad,
                    )
            # v0.20.10 · per-key 溯源: pipeline 富集键标 origin=ai + model_ref; 采纳前
            # 被人工改过的键 (attribute_overrides) 视为 human 认领, 不标 ai。内部键跳过。
            overridden = (
                {k for k in attribute_overrides if not k.startswith("_")}
                if isinstance(attribute_overrides, dict)
                else set()
            )
            attributes_meta: dict = {}
            for _akey in attributes:
                if _akey.startswith("_") or _akey in overridden:
                    continue
                _mref = ai_key_model.get(_akey)
                if _mref is not None:
                    attributes_meta[_akey] = {"origin": "ai", "model_ref": _mref}
            geometry, track_id = prepare_compact_track_identity(
                shape.get("geometry", {})
            )
            if geometry.get("type") == "box_3d" and track_id is None:
                track_id = _new_track_id()
            annotation = Annotation(
                id=uuid.uuid4(),
                task_id=prediction.task_id,
                project_id=prediction.project_id,
                video_segment_id=video_segment_id,
                user_id=user_id,
                source="prediction_based",
                annotation_type=shape.get("type", "bbox"),
                # v0.10.17 · 沿用 prediction 的 tool_unit_id; to_internal_shape 输出的
                # shape.type 也可派生 unit (polygon → region), 但优先 prediction 行已落实.
                tool_unit_id=prediction_unit,
                class_name=mapped_class,
                geometry=geometry,
                track_id=track_id,
                confidence=shape.get("confidence"),
                parent_prediction_id=prediction_id,
                attributes=attributes,
                attributes_meta=attributes_meta,
                temporal_role=(
                    "keyframe" if geometry.get("type") == "box_3d" else "sample"
                ),
            )
            self.db.add(annotation)
            if task is not None:
                try:
                    await bind_annotation_to_scene_track(
                        self.db,
                        annotation=annotation,
                        task=task,
                        temporal_role="keyframe",
                        interval_source="manual",
                        actor_id=user_id,
                    )
                except SceneTrackIntegrityError as exc:
                    _raise_scene_track_conflict(exc)
            anns.append(annotation)

        await self.db.flush()
        await self._update_task_stats(prediction.task_id)
        return anns

    async def list_by_task(
        self,
        task_id: uuid.UUID,
        include_cancelled: bool = False,
        video_segment_id: uuid.UUID | None = None,
    ) -> list[Annotation]:
        q = select(Annotation).where(
            Annotation.task_id == task_id,
            Annotation.is_active.is_(True),
            Annotation.sensor_role.is_(None),
        )
        if not include_cancelled:
            q = q.where(Annotation.was_cancelled.is_(False))
        if video_segment_id is not None:
            q = q.where(Annotation.video_segment_id == video_segment_id)
        q = q.order_by(Annotation.created_at)
        result = await self.db.execute(q)
        return list(result.scalars().all())

    async def list_by_tasks(
        self,
        task_ids: list[uuid.UUID],
        track_id: str | None = None,
    ) -> list[Annotation]:
        """v0.15.17 · 一次性拉多个 task 的 active 标注(邻帧框叠加批量端点用)。

        v0.21.2 · ADR-0045 · track_id 非空 → 服务端只回该 track(scope=selected);
        省略 → 回全部(scope=all)。空 task_ids 直接回 []。
        """
        if not task_ids:
            return []
        q = select(Annotation).where(
            Annotation.task_id.in_(task_ids),
            Annotation.is_active.is_(True),
            Annotation.was_cancelled.is_(False),
            Annotation.sensor_role.is_(None),
        )
        if track_id is not None:
            q = q.where(Annotation.track_id == track_id)
        q = q.order_by(Annotation.task_id, Annotation.created_at)
        result = await self.db.execute(q)
        return list(result.scalars().all())

    async def list_by_task_keyset(
        self,
        task_id: uuid.UUID,
        *,
        limit: int = 200,
        cursor: tuple[datetime, uuid.UUID] | None = None,
        include_cancelled: bool = False,
        video_segment_id: uuid.UUID | None = None,
    ) -> tuple[list[Annotation], tuple[datetime, uuid.UUID] | None]:
        """v0.7.6 · keyset 分页：created_at DESC, id DESC。next_cursor=None 时已末页。

        cursor 元组语义：取「严格小于」该 (ts, id) 的下一页。
        排序键参考 alembic 0031 的 ix_annotations_task_created_id 复合索引。
        """
        q = select(Annotation).where(
            Annotation.task_id == task_id,
            Annotation.is_active.is_(True),
            Annotation.sensor_role.is_(None),
        )
        if not include_cancelled:
            q = q.where(Annotation.was_cancelled.is_(False))
        if video_segment_id is not None:
            q = q.where(Annotation.video_segment_id == video_segment_id)
        if cursor is not None:
            cur_ts, cur_id = cursor
            q = q.where(
                (Annotation.created_at < cur_ts)
                | ((Annotation.created_at == cur_ts) & (Annotation.id < cur_id))
            )
        q = q.order_by(Annotation.created_at.desc(), Annotation.id.desc()).limit(limit)
        rows = list((await self.db.execute(q)).scalars().all())
        next_cursor: tuple[datetime, uuid.UUID] | None = None
        if len(rows) == limit and rows:
            tail = rows[-1]
            next_cursor = (tail.created_at, tail.id)
        return rows, next_cursor

    async def delete(self, annotation_id: uuid.UUID) -> bool:
        annotation = await self.db.get(Annotation, annotation_id)
        if not annotation:
            return False
        if annotation.sensor_role is not None:
            raise HTTPException(
                status_code=422,
                detail="camera members must be deleted through the point-cloud camera member API",
            )
        annotation.is_active = False
        annotation.version += 1
        await touch_annotation_scene_track(self.db, annotation, make_keyframe=False)
        if annotation.sensor_role is None and annotation.scene_track_id is not None:
            camera_members = list(
                (
                    await self.db.execute(
                        select(Annotation)
                        .where(Annotation.task_id == annotation.task_id)
                        .where(Annotation.scene_track_id == annotation.scene_track_id)
                        .where(Annotation.sensor_role.is_not(None))
                        .where(Annotation.is_active.is_(True))
                        .where(Annotation.was_cancelled.is_(False))
                        .with_for_update()
                    )
                ).scalars()
            )
            for camera_member in camera_members:
                camera_member.is_active = False
                camera_member.version += 1
        # v0.20.9 · 级联软删子框: 删父框时其所有 active 子框一并软删, 不留 orphan。
        # 父子仅一层 (create 时约束), 故无需递归。
        children = (
            (
                await self.db.execute(
                    select(Annotation).where(
                        Annotation.parent_annotation_id == annotation_id,
                        Annotation.is_active.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
        for child in children:
            child.is_active = False
            child.version += 1
        await self.db.flush()
        await self._update_task_stats(annotation.task_id)
        return True

    async def bulk_update(
        self,
        ids: list[uuid.UUID],
        *,
        class_name: str | None = None,
        attributes: dict | None = None,
        z_order: int | None = None,
        is_locked: bool | None = None,
        is_hidden: bool | None = None,
    ) -> list[Annotation]:
        """I12 · 批量 patch N 个标注. 任一标注被锁/已软删则整体 422.

        调用方负责在外层加 _assert_task_editable + assert_project_visible.
        """
        from fastapi import HTTPException

        if not ids:
            return []
        rows = (
            (await self.db.execute(select(Annotation).where(Annotation.id.in_(ids))))
            .scalars()
            .all()
        )
        if len(rows) != len(ids):
            missing = set(ids) - {r.id for r in rows}
            raise HTTPException(
                status_code=404,
                detail=f"annotations not found: {sorted(str(m) for m in missing)}",
            )
        mask_task_ids = sorted(
            {
                row.task_id
                for row in rows
                if (row.geometry or {}).get("type")
                in {"raster_mask", "video_mask", "video_track_mask"}
            },
            key=str,
        )
        if mask_task_ids:
            await self.db.execute(
                select(Task)
                .where(Task.id.in_(mask_task_ids))
                .order_by(Task.id)
                .with_for_update()
            )
            rows = (
                (
                    await self.db.execute(
                        select(Annotation)
                        .where(Annotation.id.in_(ids))
                        .order_by(Annotation.id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                )
                .scalars()
                .all()
            )
        # 整体校验: 任一被锁 / 已软删 → 422 + 整体回滚 (调用方上游事务承担).
        for r in rows:
            if r.sensor_role is not None:
                raise HTTPException(
                    status_code=422,
                    detail="camera members must be edited through the point-cloud camera member API",
                )
            if not r.is_active:
                raise HTTPException(
                    status_code=422,
                    detail=f"annotation {r.id} is not active",
                )
            if r.is_locked and (
                class_name is not None or attributes is not None or z_order is not None
            ):
                raise HTTPException(
                    status_code=422,
                    detail=f"annotation {r.id} is locked; unlock it first",
                )
        # class_name 走软校验 (复用 _validate_class_name); bulk 时按 tool_unit_id 分桶,
        # 每桶各校验一次, 避免 N 次同 unit 重复查 project.tool_bindings.
        if class_name is not None:
            by_unit: dict[tuple[uuid.UUID | None, str], None] = {}
            for r in rows:
                by_unit[(r.project_id, r.tool_unit_id)] = None
            for (pid, unit), _ in by_unit.items():
                await self._validate_class_name(pid, unit, class_name)
        linked_rows = [row for row in rows if row.scene_track_id is not None]
        linked_track_ids = sorted(
            {
                row.scene_track_id
                for row in linked_rows
                if row.scene_track_id is not None
            },
            key=str,
        )
        linked_tracks: dict[uuid.UUID, SceneTrack] = {}
        if linked_track_ids:
            tracks = list(
                (
                    await self.db.execute(
                        select(SceneTrack)
                        .where(SceneTrack.id.in_(linked_track_ids))
                        .order_by(SceneTrack.id)
                        .with_for_update()
                    )
                ).scalars()
            )
            linked_tracks = {track.id: track for track in tracks}
            if len(linked_tracks) != len(linked_track_ids):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "reason": "track_missing",
                        "message": "one or more annotations reference a missing Scene Track",
                    },
                )
        if class_name is not None:
            selected_counts = Counter(
                row.scene_track_id
                for row in linked_rows
                if row.scene_track_id is not None
            )
            for track_id, selected_count in selected_counts.items():
                active_count = await self.db.scalar(
                    select(func.count())
                    .select_from(Annotation)
                    .where(Annotation.scene_track_id == track_id)
                    .where(Annotation.is_active.is_(True))
                    .where(Annotation.was_cancelled.is_(False))
                    .where(Annotation.sensor_role.is_(None))
                )
                if int(active_count or 0) != selected_count:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "reason": "track_class_change_requires_scope",
                            "message": "select every active Scene Track member to change its class",
                        },
                    )
                linked_tracks[track_id].class_name = class_name
                camera_members = list(
                    (
                        await self.db.execute(
                            select(Annotation)
                            .where(Annotation.scene_track_id == track_id)
                            .where(Annotation.sensor_role.is_not(None))
                            .where(Annotation.was_cancelled.is_(False))
                            .with_for_update()
                        )
                    ).scalars()
                )
                for camera_member in camera_members:
                    camera_member.class_name = class_name
                    camera_member.version = int(camera_member.version or 1) + 1
        for r in rows:
            if class_name is not None:
                r.class_name = class_name
            if attributes is not None:
                # v0.20.10 · 同 update: 批量改属性时同步各行 meta（人工认领）。
                r.attributes_meta = _sync_attributes_meta(
                    r.attributes, r.attributes_meta, attributes
                )
                r.attributes = attributes
            if z_order is not None:
                r.z_order = z_order
            if is_locked is not None:
                r.is_locked = is_locked
            if is_hidden is not None:
                r.is_hidden = is_hidden
            r.version += 1
        for track in linked_tracks.values():
            track.revision = int(track.revision or 1) + 1
        await self.db.flush()
        return rows

    async def _resolve_axis_convention(self, task: Task) -> str | None:
        """v0.14.1 · 取 task 主 dataset_item 所在 dataset 的 axis_convention。
        无主 item / 无 metadata key → None(前端按 iso_8855 identity 处理)。"""
        from app.db.models.dataset import Dataset
        from app.services.scene import resolve_primary_item_id

        primary_item_id = await resolve_primary_item_id(self.db, task)
        if primary_item_id is None:
            return None
        from app.db.models.dataset import DatasetItem

        item = await self.db.get(DatasetItem, primary_item_id)
        if item is None:
            return None
        dataset = await self.db.get(Dataset, item.dataset_id)
        if dataset is None:
            return None
        return (dataset.metadata_ or {}).get("axis_convention")

    async def _frame_pose(self, scene_id, frame_index):
        """v0.15.1 · 取 (scene_id, frame_index) 的 ego pose;任一参数缺/无行 → None。"""
        if scene_id is None or frame_index is None:
            return None
        from app.services import scene_pose as scene_pose_svc

        return await scene_pose_svc.get_frame_pose(
            self.db, scene_id=scene_id, frame_index=frame_index
        )

    async def propagate(
        self,
        *,
        source_annotation_id: uuid.UUID,
        target_task_id: uuid.UUID,
        user_id: uuid.UUID,
        override_psr: dict | None = None,
    ) -> tuple[Annotation, bool]:
        """v0.14.1 · 跨帧目标延续: 把源 annotation 复制到目标 task。

        语义:
        - 仅复制静态几何(PROPAGATABLE_GEOMETRY_TYPES); video_* / point_mask_3d 拒。
        - 跨帧链共享 track_id (ADR-0045): 源无 track_id 时 _new_track_id() 分配并
          写回源, 再复用; 不再写 group_id (跨帧语义已全切 track_id)。
        - box_3d 的 convention_at_create 取**目标** dataset 的 axis_convention:
          DB 内 PSR 永远是 ISO 系字节, 原值复制即对齐世界坐标; convention_at_create
          写目标值仅为让前端 banner 不误报(v0.13.11 安全网)。
        - override_psr 留扩展位, 给定时覆盖 box_3d 的 center/size/rotation
          (并跳过运动补偿——调用方已显式给定目标 PSR)。
        - v0.15.1 · 运动补偿: box_3d 且源/目标帧都有 ego pose 时, 由「世界位置
          不变」反算目标帧 PSR(静止物自动对齐); 无 pose 退化为原样复制。

        返回 (新 annotation, motion_compensated)。
        """
        from fastapi import HTTPException

        src = await self.db.get(Annotation, source_annotation_id)
        if src is None or not src.is_active:
            raise HTTPException(status_code=404, detail="source annotation not found")

        target_task = await self.db.get(Task, target_task_id)
        if target_task is None:
            raise HTTPException(status_code=404, detail="target task not found")

        src_task = await self.db.get(Task, src.task_id)
        if src_task is None:
            raise HTTPException(status_code=404, detail="source task not found")

        # A2 · lock the target task before the propagated annotations are written.
        await record_annotation_actor(self.db, target_task, user_id)
        ctx = await self._resolve_propagate_context(
            src_task=src_task, target_task=target_task
        )
        new_annotation, motion_compensated = await self._propagate_one(
            src=src, ctx=ctx, user_id=user_id, override_psr=override_psr
        )
        await self.db.flush()
        await self._update_task_stats(target_task_id)
        return new_annotation, motion_compensated

    async def _resolve_propagate_context(
        self, *, src_task: Task, target_task: Task
    ) -> _PropagateContext:
        """解析 propagate 的整批恒定上下文: 同 task/project/scene 校验 +
        源/目标 scene_id / frame_index。

        propagate_batch 一次解析供全框复用; 单条 propagate 解析后即用一次。
        box_3d 专用的 axis_convention / pose 延迟到 _propagate_one 首次遇到
        box_3d 时解析(见 _ensure_box3d_context),2D 几何不付这部分往返。
        """
        from fastapi import HTTPException

        if target_task.id == src_task.id:
            raise HTTPException(
                status_code=422, detail="propagate 目标不能是源 task 自身"
            )
        # §5.6 · 严格同 project(跨 project/dataset/scene propagate 语义不成立)。
        if src_task.project_id != target_task.project_id:
            raise HTTPException(status_code=422, detail="跨 project propagate 不被允许")

        # 同 scene 校验: 两侧主 item 必须解析到相同且非空的 scene_id。
        from app.db.models.dataset import DatasetItem
        from app.services.scene import resolve_primary_item_id

        src_item_id = await resolve_primary_item_id(self.db, src_task)
        target_item_id = await resolve_primary_item_id(self.db, target_task)
        src_scene_id = None
        target_scene_id = None
        src_frame_index = None
        target_frame_index = None
        if src_item_id is not None:
            src_item = await self.db.get(DatasetItem, src_item_id)
            if src_item is not None:
                src_scene_id = src_item.scene_id
                src_frame_index = src_item.frame_index
        if target_item_id is not None:
            target_item = await self.db.get(DatasetItem, target_item_id)
            if target_item is not None:
                target_scene_id = target_item.scene_id
                target_frame_index = target_item.frame_index
        if (
            src_scene_id is None
            or target_scene_id is None
            or src_scene_id != target_scene_id
        ):
            raise HTTPException(status_code=422, detail="跨 scene propagate 不被允许")

        return _PropagateContext(
            src_task=src_task,
            target_task=target_task,
            src_scene_id=src_scene_id,
            src_frame_index=src_frame_index,
            target_scene_id=target_scene_id,
            target_frame_index=target_frame_index,
        )

    async def _ensure_box3d_context(self, ctx: _PropagateContext) -> None:
        """box_3d 专用上下文解析: 目标 axis_convention + 源/目标帧 ego pose。

        结果缓存在 ctx 上,整批只在首次遇到 box_3d 时解析一次。
        """
        if ctx._box3d_resolved:
            return
        ctx.axis_convention = await self._resolve_axis_convention(ctx.target_task)
        ctx.pose_src = await self._frame_pose(ctx.src_scene_id, ctx.src_frame_index)
        ctx.pose_dst = await self._frame_pose(
            ctx.target_scene_id, ctx.target_frame_index
        )
        ctx._box3d_resolved = True

    async def _propagate_one(
        self,
        *,
        src: Annotation,
        ctx: _PropagateContext,
        user_id: uuid.UUID,
        override_psr: dict | None = None,
    ) -> tuple[Annotation, bool]:
        """单框 propagate 核心: 校验几何类型、延续 track_id、box_3d 运动补偿、
        建新 annotation。

        使用 ctx 中整批预解析好的 scene/frame/axis/pose,不再逐框解析。
        **不** flush、**不** 更新 task stats —— 由调用方(propagate / propagate_batch)
        在循环外统一收尾,避免逐框 N+1。
        """
        from fastapi import HTTPException

        if src is None or not src.is_active:
            raise HTTPException(status_code=404, detail="source annotation not found")

        geom_type = (src.geometry or {}).get("type")
        if geom_type not in PROPAGATABLE_GEOMETRY_TYPES:
            raise HTTPException(
                status_code=422,
                detail=f"geometry type '{geom_type}' 不支持跨帧 propagate",
            )

        # 共享跨帧标识 track_id: 源无则分配并写回(每框各自一条独立链)。
        # v0.21.2 · ADR-0045 · Phase 6 · track_id 为唯一权威跨帧标识, 不再 dual-write
        # group_id (readers 已全切 track_id); cross_frame_group_seq 停写 (v0.21.3 DROP)。
        track_id = src.track_id
        if track_id is None:
            track_id = _new_track_id()
            src.track_id = track_id
            src.version += 1

        geometry = copy.deepcopy(src.geometry or {})
        motion_compensated = False
        if geom_type == "box_3d":
            await self._ensure_box3d_context(ctx)
            geometry["convention_at_create"] = ctx.axis_convention
            if override_psr:
                for key in ("center", "size", "rotation"):
                    if key in override_psr:
                        geometry[key] = override_psr[key]
            elif all(k in geometry for k in ("center", "size", "rotation")):
                # v0.15.1 · 运动补偿: 世界位置不变, 投回目标帧 ego 系;
                # 任一帧无 pose → compensate_psr 恒等返回(= v0.14.1 原样复制)
                from app.services.ego_transform import compensate_psr

                psr, motion_compensated = compensate_psr(
                    {
                        "center": geometry["center"],
                        "size": geometry["size"],
                        "rotation": geometry["rotation"],
                    },
                    pose_src=ctx.pose_src,
                    pose_dst=ctx.pose_dst,
                )
                if motion_compensated:
                    geometry.update(psr)

        new_annotation = Annotation(
            id=uuid.uuid4(),
            task_id=ctx.target_task.id,
            project_id=ctx.target_task.project_id,
            video_segment_id=src.video_segment_id,
            user_id=user_id,
            source="manual",
            annotation_type=src.annotation_type,
            tool_unit_id=src.tool_unit_id,
            class_name=src.class_name,
            geometry=geometry,
            track_id=track_id,
            attributes=copy.deepcopy(src.attributes or {}),
            temporal_role="derived" if geom_type == "box_3d" else "sample",
        )
        self.db.add(new_annotation)
        if geom_type == "box_3d":
            assert ctx.src_frame_index is not None
            assert ctx.target_frame_index is not None
            assert ctx.target_scene_id is not None
            try:
                track = await ensure_scene_track(
                    self.db,
                    project_id=ctx.target_task.project_id,
                    scene_id=ctx.target_scene_id,
                    track_id=track_id,
                    class_name=src.class_name,
                    frames={ctx.src_frame_index, ctx.target_frame_index},
                    actor_id=user_id,
                    interval_source="derived",
                )
            except SceneTrackIntegrityError as exc:
                _raise_scene_track_conflict(exc)
            src.scene_track_id = track.id
            if src.temporal_role != "keyframe":
                src.temporal_role = "keyframe"
                src.version = int(src.version or 1) + 1
            new_annotation.scene_track_id = track.id
        return new_annotation, motion_compensated

    async def propagate_batch(
        self,
        *,
        source_task_id: uuid.UUID,
        target_task_id: uuid.UUID,
        annotation_ids: list[uuid.UUID] | None,
        user_id: uuid.UUID,
    ) -> tuple[list[tuple[uuid.UUID, Annotation]], bool]:
        """v0.15.1 · 多目标批量 propagate: 一次把源 task 的多个(或全部)box_3d
        运动补偿延续到目标 task,各自延续 group_id 链。

        - annotation_ids=None → 源 task 全部 active box_3d(空 → 422)。
        - 显式 ids → 必须都属于源 task 且 active(否则 404),逐条复用单条
          propagate 的全部校验;任一失败整批回滚(同一事务)。

        返回 ([(src_id, 新 annotation)...], motion_compensated)。
        """
        from fastapi import HTTPException

        if annotation_ids is None:
            rows = (
                (
                    await self.db.execute(
                        select(Annotation)
                        .where(Annotation.task_id == source_task_id)
                        .where(Annotation.is_active.is_(True))
                        .where(Annotation.geometry["type"].astext == "box_3d")
                        .order_by(Annotation.created_at, Annotation.id)
                    )
                )
                .scalars()
                .all()
            )
            if not rows:
                raise HTTPException(
                    status_code=422, detail="源 task 没有可延续的 box_3d 标注"
                )
        else:
            if not annotation_ids:
                raise HTTPException(status_code=422, detail="annotation_ids 不能为空")
            rows = (
                (
                    await self.db.execute(
                        select(Annotation).where(Annotation.id.in_(annotation_ids))
                    )
                )
                .scalars()
                .all()
            )
            by_id = {r.id: r for r in rows}
            missing = [
                str(i)
                for i in annotation_ids
                if i not in by_id
                or not by_id[i].is_active
                or by_id[i].task_id != source_task_id
            ]
            if missing:
                raise HTTPException(
                    status_code=404,
                    detail=f"annotations 不存在/不属于源 task: {', '.join(missing)}",
                )
            rows = [by_id[i] for i in annotation_ids]

        # 整批共享同一 source_task / target_task → scene/frame/axis/pose 全恒定。
        # 循环外解析一次(原先逐条 propagate 会重复解析 → N+1,框数 20-50+ 放大成
        # 上百次 DB 往返)。校验语义不变: 仍走与单条 propagate 同一份 context 解析。
        source_task = await self.db.get(Task, source_task_id)
        if source_task is None:
            raise HTTPException(status_code=404, detail="source task not found")
        target_task = await self.db.get(Task, target_task_id)
        if target_task is None:
            raise HTTPException(status_code=404, detail="target task not found")
        # A2 · lock the target task before the batch of propagated annotations.
        await record_annotation_actor(self.db, target_task, user_id)
        ctx = await self._resolve_propagate_context(
            src_task=source_task, target_task=target_task
        )

        results: list[tuple[uuid.UUID, Annotation]] = []
        motion_compensated = False
        for src in rows:
            new_annotation, compensated = await self._propagate_one(
                src=src, ctx=ctx, user_id=user_id
            )
            motion_compensated = motion_compensated or compensated
            results.append((src.id, new_annotation))
        # 一次 flush + 一次 task stats 更新(原逐框 _update_task_stats 是 N+1 的另一
        # 来源: 每框都做 count 查询 + Task.get)。最终计数/状态与逐框累加等价。
        await self.db.flush()
        await self._update_task_stats(target_task_id)
        return results, motion_compensated

    async def interpolate_range(
        self,
        *,
        track_id: str,
        from_task_id: uuid.UUID,
        to_task_id: uuid.UUID,
        user_id: uuid.UUID,
        assert_task_editable=None,
        assert_task_visible=None,
    ) -> tuple[list[Annotation], bool, list[int]]:
        """v0.15.1 · 关键帧区间插值: 同 track_id 链上帧 i 与帧 k 各有一框,
        给区间 (i,k) 内每个有 task 的中间帧生成插值框(source="interpolated")。

        v0.21.2 · ADR-0045 · 跨帧链按 track_id 查询(原按 group_id); 新插值框
        dual-write track_id + 端点框的 group_id(readers 尚未全切, 过渡期兼容)。

        - 两端框: 各自 task 上该 track 的唯一 active box_3d(0 或 >1 → 422)。
        - 几何: 世界系线性内插中心 + slerp 朝向 + 线性尺寸,投回各帧 ego 系;
          任一帧缺 pose → 纯 ego 系插值(motion_compensated=False)。
        - 幂等: 中间帧已有该 group 的 active 标注 → 跳过(记入 skipped_frames),
          重复触发/人工微调过的帧不重复生成。
        - assert_task_editable: API 层注入的锁态校验(reviewer 例外逻辑在 api 层),
          任一中间帧不可写 → 整批 422/409,不产生部分写入。
        - assert_task_visible: API 层注入的 batch 可见性/分派校验(async)。两端 task
          已在端点校验,但中间帧 task 由 group 链 + scene 反查得到,必须逐帧再校验,
          否则 annotator 凭两端可见即可往不可见/未分派的中间批次写入(权限漂移)。

        返回 (新建 annotations, motion_compensated, skipped_frames)。
        """
        from fastapi import HTTPException

        from app.db.models.dataset import DatasetItem
        from app.services import scene_pose as scene_pose_svc
        from app.services.ego_transform import interpolate_psr
        from app.services.scene import (
            get_scene_frame_task_map,
            resolve_primary_item_id,
        )

        if from_task_id == to_task_id:
            raise HTTPException(status_code=422, detail="插值区间两端不能是同一 task")

        from_task = await self.db.get(Task, from_task_id)
        to_task = await self.db.get(Task, to_task_id)
        if from_task is None or to_task is None:
            raise HTTPException(status_code=404, detail="task not found")
        if from_task.project_id != to_task.project_id:
            raise HTTPException(status_code=422, detail="跨 project 插值不被允许")

        # A2 · interpolation also writes to middle-frame tasks discovered later,
        # so use the nonblocking task lock: a busy endpoint task rolls back to a
        # retryable 409 rather than forming a wait cycle.
        await record_annotation_actors_for_tasks(
            self.db, [from_task.id, to_task.id], user_id, nowait=True
        )

        async def _scene_frame(task: Task) -> tuple[uuid.UUID | None, int | None]:
            item_id = await resolve_primary_item_id(self.db, task)
            if item_id is None:
                return None, None
            item = await self.db.get(DatasetItem, item_id)
            if item is None:
                return None, None
            return item.scene_id, item.frame_index

        from_scene, from_frame = await _scene_frame(from_task)
        to_scene, to_frame = await _scene_frame(to_task)
        if (
            from_scene is None
            or from_scene != to_scene
            or from_frame is None
            or to_frame is None
        ):
            raise HTTPException(status_code=422, detail="插值区间必须在同一 scene 内")
        if abs(to_frame - from_frame) < 2:
            raise HTTPException(
                status_code=422, detail="插值区间无中间帧(两端帧需间隔 ≥ 2)"
            )

        async def _endpoint_box(task_id: uuid.UUID) -> Annotation:
            rows = (
                (
                    await self.db.execute(
                        select(Annotation)
                        .where(Annotation.task_id == task_id)
                        .where(Annotation.track_id == track_id)
                        .where(Annotation.is_active.is_(True))
                        .where(Annotation.geometry["type"].astext == "box_3d")
                    )
                )
                .scalars()
                .all()
            )
            if len(rows) != 1:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"task {task_id} 上 track {track_id} 的 active box_3d "
                        f"数量为 {len(rows)},插值要求恰好 1 个"
                    ),
                )
            return rows[0]

        box_from = await _endpoint_box(from_task_id)
        box_to = await _endpoint_box(to_task_id)

        # 统一成 i < k(几何插值对称,t 取向不影响结果)
        if from_frame > to_frame:
            from_frame, to_frame = to_frame, from_frame
            box_from, box_to = box_to, box_from

        frame_to_task = await get_scene_frame_task_map(self.db, from_scene)
        mid_frames = sorted(f for f in frame_to_task if from_frame < f < to_frame)
        if not mid_frames:
            raise HTTPException(
                status_code=422, detail="插值区间内没有可写的中间帧 task"
            )

        # 整批前置校验: 中间帧 task 可见性(batch 可见/分派)+ 锁态。
        # 可见性必须逐帧校验——两端 task 可见不代表中间帧批次对该用户可见/已分派。
        mid_tasks: dict[int, Task] = {}
        for f in mid_frames:
            t = await self.db.get(Task, frame_to_task[f])
            if t is None:
                continue
            if assert_task_visible is not None:
                await assert_task_visible(t)
            if assert_task_editable is not None:
                assert_task_editable(t)
            mid_tasks[f] = t

        trajectory = {
            p.frame_index: p
            for p in await scene_pose_svc.get_trajectory(self.db, from_scene)
        }
        psr_a = {
            "center": box_from.geometry["center"],
            "size": box_from.geometry["size"],
            "rotation": box_from.geometry["rotation"],
        }
        psr_b = {
            "center": box_to.geometry["center"],
            "size": box_to.geometry["size"],
            "rotation": box_to.geometry["rotation"],
        }
        convention = await self._resolve_axis_convention(from_task)

        created: list[Annotation] = []
        skipped_frames: list[int] = []
        motion_compensated = True
        for f, mid_task in mid_tasks.items():
            # 幂等: 该帧已有同 track 的 active 标注 → 跳过
            existing = await self.db.scalar(
                select(func.count())
                .select_from(Annotation)
                .where(Annotation.task_id == mid_task.id)
                .where(Annotation.track_id == track_id)
                .where(Annotation.is_active.is_(True))
            )
            if existing:
                skipped_frames.append(f)
                continue

            # A2 · lock this mid task (NOWAIT) before writing its interpolation.
            await record_annotation_actor(self.db, mid_task, user_id, nowait=True)
            t = (f - from_frame) / (to_frame - from_frame)
            psr, compensated = interpolate_psr(
                psr_a,
                psr_b,
                t,
                pose_a=trajectory.get(from_frame),
                pose_b=trajectory.get(to_frame),
                pose_mid=trajectory.get(f),
            )
            motion_compensated = motion_compensated and compensated

            geometry = copy.deepcopy(box_from.geometry or {})
            geometry.update(psr)
            geometry["convention_at_create"] = convention
            ann = Annotation(
                id=uuid.uuid4(),
                task_id=mid_task.id,
                project_id=mid_task.project_id,
                user_id=user_id,
                source="interpolated",
                annotation_type=box_from.annotation_type,
                tool_unit_id=box_from.tool_unit_id,
                class_name=box_from.class_name,
                geometry=geometry,
                track_id=track_id,
                attributes=copy.deepcopy(box_from.attributes or {}),
                temporal_role="derived",
            )
            self.db.add(ann)
            created.append(ann)

        try:
            track = await ensure_scene_track(
                self.db,
                project_id=from_task.project_id,
                scene_id=from_scene,
                track_id=track_id,
                class_name=box_from.class_name,
                frames=set(range(from_frame, to_frame + 1)),
                actor_id=user_id,
                interval_source="derived",
            )
        except SceneTrackIntegrityError as exc:
            _raise_scene_track_conflict(exc)
        box_from.scene_track_id = track.id
        box_to.scene_track_id = track.id
        if box_from.temporal_role != "keyframe":
            box_from.temporal_role = "keyframe"
            box_from.version = int(box_from.version or 1) + 1
        if box_to.temporal_role != "keyframe":
            box_to.temporal_role = "keyframe"
            box_to.version = int(box_to.version or 1) + 1
        for ann in created:
            ann.scene_track_id = track.id

        await self.db.flush()
        for ann in created:
            await self._update_task_stats(ann.task_id)
        return created, motion_compensated and bool(created), skipped_frames

    async def update(
        self,
        annotation_id: uuid.UUID,
        geometry: dict | None = None,
        class_name: str | None = None,
        confidence: float | None = None,
        attributes: dict | None = None,
        z_order: int | None = None,
        is_locked: bool | None = None,
        is_hidden: bool | None = None,
        mask_payload_prepared: bool = False,
    ) -> Annotation | None:
        """Surgical update of mutable fields. Increments version for optimistic concurrency."""
        annotation = await self.db.get(Annotation, annotation_id)
        if not annotation or not annotation.is_active:
            return None
        if annotation.sensor_role is not None:
            raise HTTPException(
                status_code=422,
                detail="camera members must be edited through the point-cloud camera member API",
            )
        track_reclassified = False
        if class_name is not None:
            # v0.14.17 · 与 create / accept_prediction 对齐: PATCH 改类也走软校验,
            # 堵住"采纳后 PATCH 改成项目标签集外的任意非法值"的数据质量缺口.
            await self._validate_class_name(
                annotation.project_id, annotation.tool_unit_id, class_name
            )
            try:
                track_reclassified = await reclassify_single_member_scene_track(
                    self.db,
                    annotation=annotation,
                    class_name=class_name,
                )
            except SceneTrackIntegrityError as exc:
                _raise_scene_track_conflict(exc)
        if geometry is not None:
            next_type = str(geometry.get("type") or "")
            validate_geometry_type_transition(
                tool_unit_id=annotation.tool_unit_id,
                previous_geometry=annotation.geometry,
                next_geometry=geometry,
            )
            if not mask_payload_prepared:
                task = await self.db.get(Task, annotation.task_id)
                await prepare_mask_geometry_for_annotation_write(
                    self.db, task, geometry
                )
            try:
                geometry, track_id = prepare_compact_track_identity(
                    geometry,
                    annotation.track_id,
                    reject_identity_change=True,
                )
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            annotation.geometry = geometry
            annotation.annotation_type = next_type
            annotation.track_id = track_id
        if class_name is not None:
            annotation.class_name = class_name
        if confidence is not None:
            annotation.confidence = confidence
        if attributes is not None:
            # v0.20.10 · 先据旧值算 meta 同步（人工改动即认领），再替换 attributes。
            annotation.attributes_meta = _sync_attributes_meta(
                annotation.attributes, annotation.attributes_meta, attributes
            )
            annotation.attributes = attributes
        # v0.10.5 M4-β · shape 状态位字段级 PATCH（I15）
        if z_order is not None:
            annotation.z_order = z_order
        if is_locked is not None:
            annotation.is_locked = is_locked
        if is_hidden is not None:
            annotation.is_hidden = is_hidden
        annotation.version += 1
        newly_bound = False
        if (annotation.geometry or {}).get("type") == "box_3d":
            if annotation.track_id is None:
                annotation.track_id = _new_track_id()
            if annotation.scene_track_id is None:
                task = await self.db.get(Task, annotation.task_id)
                if task is not None:
                    binding = await bind_annotation_to_scene_track(
                        self.db,
                        annotation=annotation,
                        task=task,
                        temporal_role=(
                            "keyframe"
                            if geometry is not None or class_name is not None
                            else annotation.temporal_role
                        ),
                        interval_source="manual",
                        actor_id=None,
                    )
                    newly_bound = binding is not None
        try:
            if not track_reclassified and not newly_bound:
                await touch_annotation_scene_track(
                    self.db,
                    annotation,
                    make_keyframe=geometry is not None,
                )
            elif track_reclassified and geometry is not None:
                annotation.temporal_role = "keyframe"
        except SceneTrackIntegrityError as exc:
            _raise_scene_track_conflict(exc)
        await self.db.flush()
        return annotation

    async def convert_video_track_to_bboxes(
        self,
        *,
        task: Task,
        annotation: Annotation,
        user_id: uuid.UUID,
        operation: str,
        scope: str,
        frame_index: int | None = None,
        frame_mode: str = "keyframes",
        frame_count: int | None = None,
        max_created: int = VIDEO_BBOX_CONVERSION_LIMIT,
    ) -> tuple[Annotation | None, list[Annotation], bool, list[int]]:
        # A2 · lock the task before any annotation row is created/updated.
        await record_annotation_actor(self.db, task, user_id)
        geometry = annotation.geometry or {}
        if geometry.get("type") != "video_track_bbox":
            raise ValueError("annotation must be a video_track_bbox")

        frames: list[dict]
        removed_frame_indexes: list[int] = []
        deleted_source = False

        if scope == "frame":
            if frame_index is None:
                raise ValueError("frame_index is required for frame scope")
            keyframes = sorted_keyframes(geometry)
            if operation == "split":
                exact = next(
                    (
                        kf
                        for kf in keyframes
                        if int(kf.get("frame_index", 0)) == frame_index
                    ),
                    None,
                )
                if not exact or frame_is_outside(geometry, frame_index):
                    raise ValueError("frame split requires an exact visible keyframe")
                frames = [
                    {
                        "frame_index": frame_index,
                        "bbox": exact.get("bbox") or {},
                        "source": exact.get("source", "manual"),
                    }
                ]
                removed_frame_indexes = [frame_index]
            else:
                resolved = resolve_track_at_frame(geometry, frame_index)
                if not resolved:
                    raise ValueError("track has no bbox at the requested frame")
                frames = [resolved]
        elif scope == "track":
            frames = resolved_track_frames(
                geometry,
                frame_mode=frame_mode,
                frame_count=frame_count,
            )
            if operation == "split":
                removed_frame_indexes = [int(frame["frame_index"]) for frame in frames]
        else:
            raise ValueError("scope must be one of: frame, track")

        if not frames:
            raise ValueError("track conversion produced no frames")
        if len(frames) > max_created:
            raise ValueError(
                f"track conversion would create more than {max_created} bboxes"
            )

        created: list[Annotation] = []
        for frame in frames:
            bbox = frame.get("bbox") or {}
            created_ann = Annotation(
                id=uuid.uuid4(),
                task_id=task.id,
                project_id=task.project_id,
                video_segment_id=annotation.video_segment_id,
                user_id=user_id,
                source=annotation.source,
                annotation_type="video_bbox",
                class_name=annotation.class_name,
                geometry={
                    "type": "video_bbox",
                    "frame_index": int(frame.get("frame_index", 0)),
                    "x": bbox.get("x", 0),
                    "y": bbox.get("y", 0),
                    "w": bbox.get("w", 0),
                    "h": bbox.get("h", 0),
                },
                confidence=annotation.confidence,
                parent_annotation_id=annotation.id,
                attributes=dict(annotation.attributes or {}),
            )
            self.db.add(created_ann)
            created.append(created_ann)

        if operation == "split":
            if scope == "frame":
                remaining = [
                    kf
                    for kf in sorted_keyframes(geometry)
                    if int(kf.get("frame_index", 0)) != frame_index
                ]
                if remaining:
                    annotation.geometry = {**geometry, "keyframes": remaining}
                    annotation.version += 1
                else:
                    annotation.is_active = False
                    deleted_source = True
            else:
                annotation.is_active = False
                deleted_source = True

        await self.db.flush()
        await self._update_task_stats(task.id)
        source = None if deleted_source else annotation
        return source, created, deleted_source, removed_frame_indexes

    async def compose_video_tracks(
        self,
        *,
        task: Task,
        annotations: list[Annotation],
        user_id: uuid.UUID,
        operation: str,
        frame_index: int | None = None,
        delete_sources: bool = True,
        gap_mode: str = "interpolate",
    ) -> tuple[list[Annotation], list[Annotation], list[uuid.UUID]]:
        # A2 · lock the task before any annotation row is created/updated.
        await record_annotation_actor(self.db, task, user_id)
        if operation == "aggregate_bboxes":
            updated, created, deleted = await self._aggregate_video_bboxes(
                task=task,
                annotations=annotations,
                user_id=user_id,
                delete_sources=delete_sources,
            )
        elif operation == "split_track":
            updated, created, deleted = await self._split_video_track(
                task=task,
                annotations=annotations,
                user_id=user_id,
                frame_index=frame_index,
            )
        elif operation == "merge_tracks":
            updated, created, deleted = await self._merge_video_tracks(
                task=task,
                annotations=annotations,
            )
        elif operation == "join_tracks":
            updated, created, deleted = await self._join_video_tracks(
                task=task,
                annotations=annotations,
                gap_mode=gap_mode,
            )
        else:
            raise ValueError("unsupported composition operation")

        await self.db.flush()
        await self._update_task_stats(task.id)
        return updated, created, deleted

    async def _aggregate_video_bboxes(
        self,
        *,
        task: Task,
        annotations: list[Annotation],
        user_id: uuid.UUID,
        delete_sources: bool,
    ) -> tuple[list[Annotation], list[Annotation], list[uuid.UUID]]:
        if not annotations:
            raise ValueError("annotation_ids is required")
        if any((ann.geometry or {}).get("type") != "video_bbox" for ann in annotations):
            raise ValueError("aggregate_bboxes only accepts video_bbox annotations")
        class_names = {ann.class_name for ann in annotations}
        if len(class_names) != 1:
            raise ValueError("video_bbox annotations must share one class")

        frames: dict[int, Annotation] = {}
        for ann in annotations:
            frame = int((ann.geometry or {}).get("frame_index", 0))
            if frame in frames:
                raise ValueError("video_bbox annotations must not share a frame_index")
            frames[frame] = ann

        ordered = [frames[frame] for frame in sorted(frames)]
        keyframes = [
            _composition_keyframe(
                int(ann.geometry.get("frame_index", 0)),
                ann.geometry,
                source="manual",
            )
            for ann in ordered
        ]
        created_track_id = _new_track_id()
        created = Annotation(
            id=uuid.uuid4(),
            task_id=task.id,
            project_id=task.project_id,
            video_segment_id=ordered[0].video_segment_id,
            user_id=user_id,
            source=ordered[0].source,
            annotation_type="video_track_bbox",
            class_name=ordered[0].class_name,
            geometry={
                "type": "video_track_bbox",
                "track_id": created_track_id,
                "keyframes": keyframes,
                "outside": [],
            },
            track_id=created_track_id,
            confidence=ordered[0].confidence,
            attributes=dict(ordered[0].attributes or {}),
        )
        self.db.add(created)

        deleted_ids: list[uuid.UUID] = []
        if delete_sources:
            for ann in ordered:
                ann.is_active = False
                deleted_ids.append(ann.id)
        return [], [created], deleted_ids

    async def _split_video_track(
        self,
        *,
        task: Task,
        annotations: list[Annotation],
        user_id: uuid.UUID,
        frame_index: int | None,
    ) -> tuple[list[Annotation], list[Annotation], list[uuid.UUID]]:
        if frame_index is None:
            raise ValueError("frame_index is required for split_track")
        if len(annotations) != 1:
            raise ValueError("split_track requires exactly one annotation")
        source = annotations[0]
        geometry = source.geometry or {}
        if geometry.get("type") != "video_track_bbox":
            raise ValueError("split_track only accepts a video_track_bbox annotation")
        if not resolve_track_at_frame(geometry, frame_index):
            raise ValueError("split_track requires a visible frame")

        keyframes = sorted_keyframes(geometry)
        before_keyframes = [
            dict(kf) for kf in keyframes if int(kf.get("frame_index", 0)) <= frame_index
        ]
        if not any(
            int(kf.get("frame_index", 0)) == frame_index for kf in before_keyframes
        ):
            resolved = resolve_track_at_frame(geometry, frame_index)
            if not resolved:
                raise ValueError("split_track requires a visible frame")
            before_keyframes.append(
                _composition_keyframe(frame_index, resolved.get("bbox") or {})
            )
        before_keyframes.sort(key=lambda kf: int(kf.get("frame_index", 0)))

        next_frame = frame_index + 1
        next_resolved = resolve_track_at_frame(geometry, next_frame)
        after_keyframes = [
            dict(kf) for kf in keyframes if int(kf.get("frame_index", 0)) > frame_index
        ]
        if next_resolved and not any(
            int(kf.get("frame_index", 0)) == next_frame for kf in after_keyframes
        ):
            after_keyframes.insert(
                0, _composition_keyframe(next_frame, next_resolved.get("bbox") or {})
            )
        after_keyframes.sort(key=lambda kf: int(kf.get("frame_index", 0)))
        if not _track_visible_keyframes(
            {"type": "video_track_bbox", "keyframes": after_keyframes}
        ):
            raise ValueError("split_track requires a visible tail segment")

        source.geometry = {
            **geometry,
            "keyframes": before_keyframes,
            "outside": _clip_outside_ranges(geometry, start=None, end=frame_index),
        }
        source.version += 1

        tail_track_id = _new_track_id()
        tail = Annotation(
            id=uuid.uuid4(),
            task_id=task.id,
            project_id=task.project_id,
            video_segment_id=source.video_segment_id,
            user_id=user_id,
            source=source.source,
            annotation_type="video_track_bbox",
            class_name=source.class_name,
            geometry={
                "type": "video_track_bbox",
                "track_id": tail_track_id,
                "semantic_label": geometry.get("semantic_label"),
                "keyframes": after_keyframes,
                "outside": _clip_outside_ranges(geometry, start=next_frame, end=None),
            },
            track_id=tail_track_id,
            confidence=source.confidence,
            parent_annotation_id=source.id,
            attributes=dict(source.attributes or {}),
        )
        self.db.add(tail)
        return [source], [tail], []

    async def _merge_video_tracks(
        self,
        *,
        task: Task,
        annotations: list[Annotation],
    ) -> tuple[list[Annotation], list[Annotation], list[uuid.UUID]]:
        # merge_tracks 默认把 gap 区间标 outside 后合并.
        return self._combine_two_video_tracks(
            annotations=annotations,
            operation="merge_tracks",
            fill_gap_outside=True,
        )

    async def _join_video_tracks(
        self,
        *,
        task: Task,
        annotations: list[Annotation],
        gap_mode: str,
    ) -> tuple[list[Annotation], list[Annotation], list[uuid.UUID]]:
        # v0.10.30 · D-2.5 · 两条帧号不重叠的 track 之间补 gap 后合并.
        # gap_mode="outside" 与 merge_tracks 行为一致 (gap 标 outside);
        # gap_mode="interpolate" 不写 gap outside, gap 端点间靠现有线性插值连接.
        return self._combine_two_video_tracks(
            annotations=annotations,
            operation="join_tracks",
            fill_gap_outside=(gap_mode == "outside"),
        )

    def _combine_two_video_tracks(
        self,
        *,
        annotations: list[Annotation],
        operation: str,
        fill_gap_outside: bool,
    ) -> tuple[list[Annotation], list[Annotation], list[uuid.UUID]]:
        """merge / join 共用的两条 track 合并落库路径.

        校验恰好两条同 class 的 video_track 且帧号区间不重叠, 合并 keyframes
        (frame_index 唯一), 透传 survivor 既有 geometry (含 semantic_label) 与两条
        track 的 outside; ``fill_gap_outside`` 决定是否把 gap 区间额外标 outside.
        落库约定: survivor 保留并 version+1, removed 置 is_active=False.
        """
        if len(annotations) != 2:
            raise ValueError(f"{operation} requires exactly two annotations")
        if any(
            (ann.geometry or {}).get("type") != "video_track_bbox"
            for ann in annotations
        ):
            raise ValueError(f"{operation} only accepts video_track_bbox annotations")
        if annotations[0].class_name != annotations[1].class_name:
            raise ValueError(f"{operation} requires tracks with the same class")

        ranges: list[tuple[int, int, Annotation]] = []
        for ann in annotations:
            visible = _track_visible_keyframes(ann.geometry or {})
            if not visible:
                raise ValueError(f"{operation} requires visible keyframes")
            frames = [int(kf.get("frame_index", 0)) for kf in visible]
            ranges.append((min(frames), max(frames), ann))
        ranges.sort(key=lambda item: item[0])
        first_start, first_end, first = ranges[0]
        second_start, second_end, second = ranges[1]
        if first_end >= second_start:
            raise ValueError(f"{operation} requires non-overlapping tracks")

        survivor = annotations[0]
        removed = annotations[1]
        keyframes = [
            dict(kf)
            for ann in annotations
            for kf in sorted_keyframes(ann.geometry or {})
        ]
        frame_counts: dict[int, int] = {}
        for kf in keyframes:
            frame = int(kf.get("frame_index", 0))
            frame_counts[frame] = frame_counts.get(frame, 0) + 1
        if any(count > 1 for count in frame_counts.values()):
            raise ValueError(f"{operation} requires unique keyframe frame_index values")
        keyframes.sort(key=lambda kf: int(kf.get("frame_index", 0)))

        outside = [
            range_
            for ann in annotations
            for range_ in ((ann.geometry or {}).get("outside") or [])
        ]
        if fill_gap_outside and first_end + 1 <= second_start - 1:
            outside.append(
                {
                    "from": first_end + 1,
                    "to": second_start - 1,
                    "source": "manual",
                }
            )

        survivor.geometry = {
            **(survivor.geometry or {}),
            "type": "video_track_bbox",
            "track_id": (survivor.geometry or {}).get("track_id") or _new_track_id(),
            "keyframes": keyframes,
            "outside": normalize_outside_ranges(outside),
        }
        survivor.track_id = survivor.geometry["track_id"]
        survivor.version += 1
        removed.is_active = False
        return [survivor], [], [removed.id]

    async def save_draft(
        self, task_id: uuid.UUID, user_id: uuid.UUID, result: dict
    ) -> AnnotationDraft:
        existing = await self.db.execute(
            select(AnnotationDraft).where(
                AnnotationDraft.task_id == task_id,
                AnnotationDraft.user_id == user_id,
            )
        )
        draft = existing.scalar_one_or_none()
        if draft:
            draft.result = result
        else:
            draft = AnnotationDraft(
                id=uuid.uuid4(),
                task_id=task_id,
                user_id=user_id,
                result=result,
            )
            self.db.add(draft)
        await self.db.flush()
        return draft

    async def get_draft(
        self, task_id: uuid.UUID, user_id: uuid.UUID
    ) -> AnnotationDraft | None:
        result = await self.db.execute(
            select(AnnotationDraft).where(
                AnnotationDraft.task_id == task_id,
                AnnotationDraft.user_id == user_id,
            )
        )
        return result.scalar_one_or_none()

    async def _update_task_stats(
        self,
        task_id: uuid.UUID,
        trigger_batch_transitions: bool = True,
    ) -> None:
        """更新 task 标注计数、is_labeled 与 pending↔in_progress 状态翻转。

        trigger_batch_transitions=True（默认）: 状态翻转时联动 batch 自动流转 +
        计数重算（现有行为保持不变）。
        trigger_batch_transitions=False: 只更新 task 本身，跳过 batch 流转
        （用于批量导入，避免导入过程意外推进整个 batch 状态）。
        """
        count_result = await self.db.execute(
            select(func.count()).where(
                Annotation.task_id == task_id,
                Annotation.is_active.is_(True),
                Annotation.was_cancelled.is_(False),
                Annotation.sensor_role.is_(None),
            )
        )
        count = count_result.scalar() or 0

        task = await self.db.get(Task, task_id)
        status_changed = False
        if task:
            task.total_annotations = count
            task.is_labeled = count > 0
            # B-20：首次产生标注 → 把 task 从 pending 转 in_progress；标注全删 → 回 pending。
            # 让 batch.in_progress_tasks / dashboard 进度条与"已动工"状态对齐。
            if count > 0 and task.status == "pending":
                task.status = "in_progress"
                status_changed = True
            elif count == 0 and task.status == "in_progress":
                task.status = "pending"
                status_changed = True
        await self.db.flush()

        if trigger_batch_transitions and status_changed and task and task.batch_id:
            # 在函数内 import 避免 services 层循环依赖。
            from app.services.batch import BatchService

            batch_svc = BatchService(self.db)
            await batch_svc.check_auto_transitions(task.batch_id)
            await batch_svc.recalculate_counters(task.batch_id)
