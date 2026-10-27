from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from copy import deepcopy
from datetime import datetime
from dataclasses import replace
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import inspect, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from aap_labeling.annotation_write_contract import (
    ANNOTATION_WRITE_PROTOCOL,
    BindSceneTrackMember,
    CreateAnnotation,
    DeleteAnnotation,
    ReadCreateReceipt,
    UpdateAnnotation,
    WriteCreateReceipt,
)
from aap_labeling.annotation_bulk_contract import (
    ANNOTATION_BULK_PROTOCOL,
    AnnotationBulkPatch,
    UpdateAnnotations,
)
from aap_labeling.cross_frame_write_policy import (
    PROPAGATABLE_GEOMETRY_TYPES as PROPAGATABLE_GEOMETRY_TYPES,
)
from aap_labeling.annotation_prediction_contract import (
    ApplySecondaryAttributes,
    CreateAcceptedAnnotation,
    MarkSecondaryChild,
)

from app.db.models.annotation import Annotation
from app.services.project_access import get_workspace_project
from app.db.models.scene_track import SceneTrack
from app.db.models.prediction import (
    INTERACTIVE_ACCEPT_PREDICTION_SOURCE,
)
from app.db.models.task import Task
from app.db.models.task_lock import AnnotationDraft

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
)
from app.services.annotation_track_identity import prepare_compact_track_identity
from app.services.labeling_write_scope import LabelingWriteScope
from app.services.labeling_write_guard import lock_labeling_write
from app.services.secondary_inference_result import (
    SecondaryInferenceResult,
)
from aap_inference_contract.execution import thaw_json
from app.services.raster_mask_storage import (
    _VerifiedMaskContent,
    RasterMaskContractError,
    prepare_mask_geometry_for_annotation_write,
    prepare_mask_payload_for_write,
    preflight_mask_payload_for_write,
)
from app.services.scene_track_domain import (
    SceneTrackIntegrityError,
    touch_annotation_scene_track,
)
from app.services.labeling_annotation_maintenance import (
    LabelingAnnotationMaintenanceService,
    _orphan_user_attribute_keys as _orphan_user_attribute_keys,
    _prune_orphan_user_attributes as _prune_orphan_user_attributes,
)
from app.services.labeling_annotation_data import annotation_write_command
from app.services.labeling_annotation_bulk_data import annotation_bulk_command
from app.services.labeling_video_bbox_editing import LabelingVideoBBoxEditingService
from app.services.labeling_cross_frame_writes import LabelingCrossFrameWriteService
from app.services.labeling_annotation_prediction import (
    annotation_prediction_command,
    annotation_prediction_context,
)
from app.services.labeling_core_read_relations import (
    annotation_row,
    get_core_row,
    lock_task_for_core_read,
    prediction_meta_row,
    prediction_row,
    task_row,
)

logger = logging.getLogger("app.services.annotation")

VIDEO_BBOX_CONVERSION_LIMIT = 5000


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
    """Preserve unchanged AI attributes and existing server-owned video lineage.

    Human edits remove per-attribute AI provenance. Reserved keys from the
    request never create or replace server-owned metadata.
    """
    from app.services.video_prediction_reads import LINEAGE_KEY

    old_attrs = old_attrs or {}
    old_meta = old_meta or {}
    new_meta: dict = {}
    if LINEAGE_KEY in old_meta:
        new_meta[LINEAGE_KEY] = deepcopy(old_meta[LINEAGE_KEY])
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


class AnnotationService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _load_after_write(self, annotation_id: uuid.UUID) -> Annotation:
        return (
            await self.db.execute(
                select(annotation_row)
                .where(annotation_row.id == annotation_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()

    async def _refresh_loaded_annotation_fields(
        self, annotation_ids: set[uuid.UUID], fields: tuple[str, ...]
    ) -> None:
        loaded = {
            item.id: item
            for item in self.db.sync_session.identity_map.values()
            if isinstance(item, Annotation) and item.id in annotation_ids
        }
        if not loaded:
            return
        columns = [
            annotation_row.id,
            *(getattr(annotation_row, field) for field in fields),
        ]
        with self.db.no_autoflush:
            rows = (
                await self.db.execute(
                    select(*columns).where(annotation_row.id.in_(loaded))
                )
            ).mappings()
            for row in rows:
                item = loaded[row["id"]]
                state = inspect(item)
                for field in fields:
                    value = row[field]
                    if field not in state.dict or state.dict[field] != value:
                        set_committed_value(item, field, value)

    async def _refresh_loaded_scene_track_fields(
        self, scene_track_id: uuid.UUID, fields: tuple[str, ...]
    ) -> None:
        loaded = next(
            (
                item
                for item in self.db.sync_session.identity_map.values()
                if isinstance(item, SceneTrack) and item.id == scene_track_id
            ),
            None,
        )
        if loaded is None:
            return
        from app.services.labeling_scene_read import read_scene_data

        with self.db.no_autoflush:
            data = await read_scene_data(
                self.db, "load_scene_track_cache", scene_track_id=scene_track_id
            )
        row = data.track_cache
        if row is not None:
            state = inspect(loaded)
            for field in fields:
                value = row[field]
                if field not in state.dict or state.dict[field] != value:
                    set_committed_value(loaded, field, value)

    async def _refresh_loaded_scene_track_cameras(
        self, scene_track_id: uuid.UUID
    ) -> None:
        loaded = {
            item.id: item
            for item in self.db.sync_session.identity_map.values()
            if isinstance(item, Annotation)
            and inspect(item).dict.get("scene_track_id", scene_track_id)
            == scene_track_id
            and inspect(item).dict.get("sensor_role", "camera") is not None
        }
        if not loaded:
            return
        from app.services.labeling_scene_read import read_scene_data

        with self.db.no_autoflush:
            data = await read_scene_data(
                self.db,
                "load_scene_camera_cache",
                scene_track_id=scene_track_id,
                annotation_ids=list(loaded),
            )
        for row in data.camera_members:
            item = loaded[row.id]
            state = inspect(item)
            for field in ("class_name", "version", "updated_at"):
                value = getattr(row, field)
                if field not in state.dict or state.dict[field] != value:
                    set_committed_value(item, field, value)

    async def _refresh_deleted_annotation_cache(
        self,
        annotation: Annotation,
        *,
        deleted_camera_members: int,
        deleted_children: int,
    ) -> None:
        loaded = {
            item.id: item
            for item in self.db.sync_session.identity_map.values()
            if isinstance(item, Annotation)
        }
        if not loaded:
            return
        targets = [annotation_row.id == annotation.id]
        if deleted_camera_members and annotation.scene_track_id is not None:
            targets.append(
                (annotation_row.task_id == annotation.task_id)
                & (annotation_row.scene_track_id == annotation.scene_track_id)
                & annotation_row.sensor_role.is_not(None)
                & annotation_row.was_cancelled.is_(False)
            )
        if deleted_children:
            targets.append(annotation_row.parent_annotation_id == annotation.id)
        with self.db.no_autoflush:
            rows = (
                await self.db.execute(
                    select(
                        annotation_row.id,
                        annotation_row.is_active,
                        annotation_row.version,
                        annotation_row.updated_at,
                    ).where(or_(*targets))
                )
            ).mappings()
            for row in rows:
                item = loaded.get(row["id"])
                if item is None:
                    continue
                state = inspect(item)
                for field in ("is_active", "version", "updated_at"):
                    value = row[field]
                    if field not in state.dict or state.dict[field] != value:
                        set_committed_value(item, field, value)

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
        from aap_workspace.configuration_policy import lookup_classes_for_tool_unit
        from fastapi import HTTPException

        project = await get_workspace_project(self.db, project_id=project_id)
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
        await lock_task_for_core_read(self.db, task_id)
        parent = await self.db.scalar(
            select(annotation_row)
            .where(annotation_row.id == parent_annotation_id)
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
        verified_mask_content: _VerifiedMaskContent | None = None,
        require_verified_mask_content: bool = False,
        native_original_receipt=None,
    ) -> Annotation:
        task = await get_core_row(self.db, task_row, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        if not require_verified_mask_content:
            verified_mask_content = (
                verified_mask_content
                or await preflight_mask_payload_for_write(
                    self.db,
                    task,
                    geometry,
                    native_original_receipt=native_original_receipt,
                )
            )
        # A2 · record before any Annotation/SceneTrack lock: this takes the task
        # row lock first, keeps the global Task -> Annotation order, and rolls
        # back with the mutation if the mutation fails.
        await record_annotation_actor(self.db, task, user_id)

        if task.project_id:
            await self._validate_class_name(task.project_id, tool_unit_id, class_name)

        if parent_annotation_id is not None:
            await self._validate_parent_annotation(
                task_id, parent_annotation_id, video_segment_id
            )

        await prepare_mask_geometry_for_annotation_write(
            self.db,
            task,
            geometry,
            verified_content=verified_mask_content,
            require_verified_content=(
                require_verified_mask_content or verified_mask_content is not None
            ),
            native_original_receipt=native_original_receipt,
        )
        geometry, track_id = prepare_compact_track_identity(geometry)
        if geometry.get("type") == "box_3d" and track_id is None:
            track_id = _new_track_id()
        annotation_id = uuid.uuid4()
        project_id = task.project_id
        resolved_annotation_type = str(geometry.get("type") or annotation_type)

        await annotation_write_command(
            self.db,
            CreateAnnotation(
                schema=ANNOTATION_WRITE_PROTOCOL,
                operation="create",
                annotation_id=annotation_id,
                task_id=task_id,
                project_id=project_id,
                actor_id=user_id,
                annotation_type=resolved_annotation_type,
                tool_unit_id=tool_unit_id,
                class_name=class_name,
                geometry=geometry,
                confidence=confidence,
                parent_prediction_id=parent_prediction_id,
                parent_annotation_id=parent_annotation_id,
                video_segment_id=video_segment_id,
                lead_time=lead_time,
                attributes=attributes or {},
                track_id=track_id,
            ),
        )
        if geometry.get("type") == "box_3d":
            from app.services.labeling_scene import resolve_task_scene_frames

            scene_frame = (await resolve_task_scene_frames(self.db, [task_id]))[task_id]
            if scene_frame.scene_id is not None and scene_frame.frame_index is not None:
                await annotation_write_command(
                    self.db,
                    BindSceneTrackMember(
                        schema=ANNOTATION_WRITE_PROTOCOL,
                        operation="bind_scene_track_member",
                        annotation_id=annotation_id,
                        task_id=task_id,
                        project_id=project_id,
                        actor_id=user_id,
                        expected_version=1,
                        scene_id=scene_frame.scene_id,
                        frame_index=scene_frame.frame_index,
                        temporal_role="keyframe",
                    ),
                )
        await self.db.flush()

        await self._update_task_stats(task_id)
        annotation = await self._load_after_write(annotation_id)
        if annotation.scene_track_id is not None:
            await self._refresh_loaded_scene_track_fields(
                annotation.scene_track_id, ("revision", "updated_at")
            )
        return annotation

    async def annotation_create_receipt_response(
        self,
        *,
        task_id: uuid.UUID,
        actor_id: uuid.UUID,
        idempotency_key: str,
        request_digest: str,
    ) -> dict | None:
        """Load a frozen create response or reject reuse of another operation key."""
        project_id = await self.db.scalar(
            select(task_row.project_id).where(task_row.id == task_id)
        )
        if project_id is None:
            return None
        result = await annotation_write_command(
            self.db,
            ReadCreateReceipt(
                schema=ANNOTATION_WRITE_PROTOCOL,
                operation="read_create_receipt",
                task_id=task_id,
                project_id=project_id,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
            ),
        )
        assert result.receipt is not None
        return result.receipt.response_json if result.receipt.found else None

    async def record_annotation_create_receipt(
        self,
        *,
        task_id: uuid.UUID,
        actor_id: uuid.UUID,
        idempotency_key: str,
        request_digest: str,
        response_json: dict,
    ) -> None:
        """Stage the historical create response in the labeling transaction."""
        project_id = await self.db.scalar(
            select(task_row.project_id).where(task_row.id == task_id)
        )
        if project_id is None:
            raise HTTPException(status_code=404, detail="Task not found")
        await annotation_write_command(
            self.db,
            WriteCreateReceipt(
                schema=ANNOTATION_WRITE_PROTOCOL,
                operation="write_create_receipt",
                operation_id=uuid.uuid4(),
                task_id=task_id,
                project_id=project_id,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                response_json=response_json,
            ),
        )

    async def _accept_prediction(
        self,
        prediction_id: uuid.UUID,
        shape_index: int | None = None,
        override_class_name: str | None = None,
        attribute_overrides: dict | None = None,
        video_segment_id: uuid.UUID | None = None,
        *,
        write_scope: LabelingWriteScope,
        reference_receipt=None,
        repair_original_receipt=None,
        video_acceptance_receipt=None,
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
        user_id = write_scope.actor_id
        if user_id is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        # v0.10.25 · predictions 复合 PK (id, created_at) 后不能用 db.get(单值)，改按 id 查。
        prediction = (
            await self.db.execute(
                select(prediction_row).where(prediction_row.id == prediction_id)
            )
        ).scalar_one_or_none()
        if (
            prediction is None
            or prediction.task_id != write_scope.task_id
            or prediction.project_id != write_scope.project_id
        ):
            return None
        if prediction.source == INTERACTIVE_ACCEPT_PREDICTION_SOURCE:
            return None

        from app.services.reference_access import reject_reference_scope

        selection = None
        if video_acceptance_receipt is not None:
            from app.services.video_prediction_acceptance import (
                selection_value,
                validate_acceptance as validate_video_acceptance,
            )
            from app.services.content_snapshot import ContentError

            if reference_receipt is not None or repair_original_receipt is not None:
                raise ContentError("prediction_video_acceptance_receipt_invalid")
            selection = selection_value(
                shape_index, override_class_name, attribute_overrides, video_segment_id
            )
            await validate_video_acceptance(
                self.db,
                prediction,
                user_id,
                video_acceptance_receipt,
                selection=selection,
            )
        elif repair_original_receipt is not None:
            from app.services.managed_repair_acceptance import (
                selection_value,
                validate_acceptance as validate_repair_acceptance,
            )
            from app.services.content_snapshot import ContentError

            if reference_receipt is not None:
                raise ContentError("repair_original_receipt_invalid")
            selection = selection_value(
                shape_index, override_class_name, attribute_overrides, video_segment_id
            )
            await validate_repair_acceptance(
                self.db,
                prediction,
                user_id,
                repair_original_receipt,
                selection=selection,
            )
        elif reference_receipt is None:
            await reject_reference_scope(self.db, task_ids=[prediction.task_id])
        else:
            from app.services.prediction_acceptance import validate_acceptance

            await validate_acceptance(self.db, prediction, user_id, reference_receipt)

        pred_meta = (
            await self.db.execute(
                select(prediction_meta_row).where(
                    prediction_meta_row.prediction_id == prediction_id
                )
            )
        ).scalar_one_or_none()
        meta_extra = (
            pred_meta.extra if pred_meta and isinstance(pred_meta.extra, dict) else {}
        )
        accepted_input_id = None
        accepted_input_sha256 = None
        accepted_video_input = None
        if "accepted_input" in meta_extra:
            from aap_assets_inputs import contract as accepted_inputs_contract
            from aap_assets_inputs.contract import InputFailure

            raw_input = meta_extra.get("accepted_input")
            try:
                if not isinstance(raw_input, dict):
                    raise ValueError("accepted input metadata is malformed")
                input_id = uuid.UUID(raw_input["input_id"])
                input_job_id = uuid.UUID(raw_input["job_id"])
                input_task_id = uuid.UUID(raw_input["task_id"])
                sha256 = raw_input["sha256"]
                byte_size = raw_input["byte_size"]
                if (
                    input_task_id != prediction.task_id
                    or not isinstance(sha256, str)
                    or re.fullmatch(r"[0-9a-f]{64}", sha256) is None
                    or type(byte_size) is not int
                    or byte_size <= 0
                ):
                    raise ValueError("accepted input metadata is malformed")
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                raise HTTPException(
                    status_code=409,
                    detail={"reason": "accepted_input_unavailable"},
                ) from exc
            try:
                from app.services.accepted_inputs import input_gateway

                async with input_gateway() as gateway:
                    record = await gateway.verify_source(
                        accepted_inputs_contract.InputRequest(
                            input_id=input_id,
                            job_id=input_job_id,
                            task_id=input_task_id,
                        )
                    )
            except InputFailure as exc:
                if exc.code in {
                    "input_busy",
                    "source_busy",
                    "reference_busy",
                    "job_busy",
                }:
                    reason = "input_busy"
                elif exc.code == "source_changed":
                    reason = "prediction_source_changed"
                else:
                    reason = "accepted_input_unavailable"
                raise HTTPException(
                    status_code=409,
                    detail={"reason": reason, "code": exc.code},
                ) from exc
            if (
                record.input_id != input_id
                or record.job_id != input_job_id
                or record.task_id != prediction.task_id
                or record.project_id != prediction.project_id
                or record.sha256 != sha256
                or record.byte_size != byte_size
            ):
                raise HTTPException(
                    status_code=409,
                    detail={"reason": "accepted_input_unavailable"},
                )
            accepted_input_id = input_id
            accepted_input_sha256 = sha256
            write_scope = replace(write_scope, accepted_input_id=accepted_input_id)
        if "accepted_video_input" in meta_extra:
            from aap_assets_video_inputs import execution as video_execution
            from aap_assets_video_inputs.evidence import (
                VideoEvidenceFailure,
                VideoInputEvidence,
                parse_prediction_evidence,
                ready_evidence,
            )

            if accepted_input_id is not None:
                raise HTTPException(
                    status_code=409,
                    detail={"reason": "accepted_video_input_unavailable"},
                )
            try:
                accepted_video_input = parse_prediction_evidence(
                    meta_extra["accepted_video_input"]
                )
                raw_execution_job_id = meta_extra["accepted_video_execution_job_id"]
                execution_job_id = uuid.UUID(raw_execution_job_id)
                if (
                    type(accepted_video_input) is not VideoInputEvidence
                    or accepted_video_input.task_id != prediction.task_id
                    or accepted_video_input.project_id != prediction.project_id
                    or raw_execution_job_id != str(execution_job_id)
                ):
                    raise ValueError(
                        "accepted video metadata does not match prediction"
                    )
            except (KeyError, TypeError, ValueError, VideoEvidenceFailure) as exc:
                raise HTTPException(
                    status_code=409,
                    detail={"reason": "accepted_video_input_unavailable"},
                ) from exc
            from app.services.accepted_video_inputs import video_input_gateway

            video_request = video_execution.VideoFrameRequest(
                actor_id=user_id,
                job_id=accepted_video_input.job_id,
                task_id=accepted_video_input.task_id,
                input_id=accepted_video_input.input_id,
                plan_digest=accepted_video_input.plan_digest,
                ordinal=accepted_video_input.entry.ordinal,
            )
            try:
                async with video_input_gateway() as gateway:
                    verified = await gateway.verify(
                        video_execution.LockVideoInputRequest(
                            **video_request.model_dump(exclude={"ordinal"})
                        )
                    )
                    reference = await gateway.ensure_reference(video_request)
                    frame = await gateway.frame(video_request)
            except video_execution.VideoInputFailure as exc:
                if exc.code in {
                    "input_busy",
                    "source_busy",
                    "reference_busy",
                    "manifest_busy",
                    "frame_busy",
                    "job_busy",
                }:
                    reason = "input_busy"
                elif exc.code in {"source_changed", "task_scope_changed"}:
                    reason = "prediction_source_changed"
                else:
                    reason = "accepted_video_input_unavailable"
                raise HTTPException(
                    status_code=409,
                    detail={"reason": reason, "code": exc.code},
                ) from exc
            if (
                verified.plan != frame.plan
                or reference.plan != frame.plan
                or reference.entry != frame.entry
                or reference.blob != frame.blob
                or ready_evidence(frame.plan, frame) != accepted_video_input
            ):
                raise HTTPException(
                    status_code=409,
                    detail={"reason": "accepted_video_input_unavailable"},
                )
        # Convert and validate every selected geometry before constructing or adding
        # any Annotation row.  This closes the accept path that previously bypassed
        # the raster create gate and linked uploads immediately before flush.
        from app.services.prediction import to_internal_shape

        raw_shapes = list(prediction.result or [])
        rejected = set(prediction.rejected_shape_indexes or [])
        if shape_index is not None:
            if not (0 <= shape_index < len(raw_shapes)):
                return None
            if shape_index in rejected:
                raise HTTPException(409, detail="Prediction shape was rejected")
            indexed = [(shape_index, to_internal_shape(raw_shapes[shape_index]))]
        else:
            indexed = [
                (index, to_internal_shape(raw_shape))
                for index, raw_shape in enumerate(raw_shapes)
                if index not in rejected
            ]
            if raw_shapes and not indexed:
                raise HTTPException(409, detail="Prediction has no available shapes")
        if video_acceptance_receipt is not None:
            from app.services.video_prediction_acceptance import selected_shape_indexes

            selected = set(selected_shape_indexes(video_acceptance_receipt))
            indexed = [(index, shape) for index, shape in indexed if index in selected]
        preflight_task = await get_core_row(
            self.db, task_row, prediction.task_id, populate_existing=True
        )
        verified_mask_content = await preflight_mask_payload_for_write(
            self.db,
            preflight_task,
            [shape.get("geometry", {}) for _, shape in indexed],
            repair_original_receipt=repair_original_receipt,
            video_acceptance_receipt=video_acceptance_receipt,
        )
        task, _actor, _access = await lock_labeling_write(
            self.db,
            write_scope,
            operations=("candidate_accept", "interactive_mask_accept"),
        )
        if task is not None:
            # A2 · task row lock before the accepted annotations are written.
            await record_annotation_actor(self.db, task, user_id)
        await prepare_mask_payload_for_write(
            self.db,
            task,
            [shape.get("geometry", {}) for _, shape in indexed],
            verified_content=verified_mask_content,
            require_verified_content=True,
            repair_original_receipt=repair_original_receipt,
            video_acceptance_receipt=video_acceptance_receipt,
        )

        # v0.20.10 · 属性级溯源: 从 PredictionMeta.extra.pipeline 建「AI 富集属性键 → model_ref」
        # 映射。pipeline 是 stage 级 (非 per-key), 但 enriched key = f"{label}_{k}" if label else k
        # (与 application/preannotation_execution.py:_run_task_pipeline 一致), 故能精确反推每个键出自哪个 stage 的 backend/model。
        # PredictionMeta 与 prediction 1:1 (prediction_id unique)。confidence 不在 extra 里, 不编造。
        ai_key_model: dict[str, dict] = {}
        pipeline_meta = (pred_meta.extra or {}).get("pipeline") if pred_meta else None
        controlled_binding = bool(
            pred_meta and (pred_meta.extra or {}).get("trained_model")
        )
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

        # B-11 · DINO 写入的 class_name 是项目类别的英文 alias; 采纳时反查
        # 对应 tool_unit 的 classes 把 alias 映射回原类别名 (中文 / 业务名).
        # v0.10.17 · 强隔离: 只查 prediction.tool_unit_id 对应 unit 的 classes,
        # 不再跨 unit 走派生 classes_config (不同 unit 同名类可有不同 alias).
        project = await get_workspace_project(self.db, project_id=prediction.project_id)
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
                else raw_class
                if controlled_binding
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
            annotation_id = uuid.uuid4()
            confidence = shape.get("confidence")
            annotation_type = shape.get("type", "bbox")
            if video_acceptance_receipt is not None:
                from app.services.video_prediction_reads import (
                    LINEAGE_KEY,
                    annotation_lineage,
                )

                attributes_meta[LINEAGE_KEY] = annotation_lineage(
                    self.db,
                    SimpleNamespace(
                        task_id=task.id,
                        project_id=task.project_id,
                        parent_prediction_id=prediction_id,
                        source="prediction_based",
                        geometry=geometry,
                    ),
                    prediction,
                    video_acceptance_receipt,
                    idx,
                )
            await annotation_prediction_command(
                self.db,
                CreateAcceptedAnnotation(
                    **annotation_prediction_context(task, user_id),
                    operation="create_accepted",
                    annotation_id=annotation_id,
                    prediction_id=prediction_id,
                    prediction_created_at=prediction.created_at,
                    video_segment_id=video_segment_id,
                    annotation_type="bbox"
                    if annotation_type is None
                    else annotation_type,
                    tool_unit_id=prediction_unit,
                    class_name=mapped_class,
                    geometry=geometry,
                    track_id=track_id,
                    confidence=float(confidence) if confidence is not None else None,
                    attributes=attributes,
                    attributes_meta=attributes_meta,
                    temporal_role="keyframe"
                    if geometry.get("type") == "box_3d"
                    else "sample",
                ),
            )
            if geometry.get("type") == "box_3d":
                from app.services.labeling_scene import resolve_task_scene_frames

                scene_frame = (await resolve_task_scene_frames(self.db, [task.id]))[
                    task.id
                ]
                if (
                    scene_frame.scene_id is not None
                    and scene_frame.frame_index is not None
                ):
                    await annotation_write_command(
                        self.db,
                        BindSceneTrackMember(
                            schema=ANNOTATION_WRITE_PROTOCOL,
                            operation="bind_scene_track_member",
                            annotation_id=annotation_id,
                            task_id=task.id,
                            project_id=task.project_id,
                            actor_id=user_id,
                            expected_version=1,
                            scene_id=scene_frame.scene_id,
                            frame_index=scene_frame.frame_index,
                            temporal_role="keyframe",
                        ),
                    )
            annotation = await self._load_after_write(annotation_id)
            anns.append(annotation)

        await self.db.flush()
        if accepted_input_id is not None:
            from app.services.accepted_inputs import attach_annotation_input_source

            assert accepted_input_sha256 is not None
            for annotation in anns:
                await attach_annotation_input_source(
                    self.db,
                    annotation_id=annotation.id,
                    input_id=accepted_input_id,
                    sha256=accepted_input_sha256,
                )
        if accepted_video_input is not None:
            from aap_assets_video_inputs.evidence import VideoEvidenceFailure
            from app.services.accepted_video_inputs import (
                attach_annotation_video_input_source,
            )

            for annotation in anns:
                try:
                    await attach_annotation_video_input_source(
                        self.db,
                        annotation_id=annotation.id,
                        actor_id=user_id,
                        evidence=accepted_video_input,
                    )
                except VideoEvidenceFailure as exc:
                    if exc.code in {
                        "input_busy",
                        "source_busy",
                        "reference_busy",
                        "manifest_busy",
                        "frame_busy",
                        "job_busy",
                    }:
                        reason = "input_busy"
                    elif exc.code in {
                        "source_changed",
                        "task_scope_changed",
                        "input_scope_mismatch",
                    }:
                        reason = "prediction_source_changed"
                    else:
                        reason = "accepted_video_input_unavailable"
                    raise HTTPException(
                        status_code=409,
                        detail={"reason": reason, "code": exc.code},
                    ) from exc
        await self._update_task_stats(prediction.task_id)
        if repair_original_receipt is not None:
            from app.services.managed_mask_repair import recheck_credentials

            await recheck_credentials(self.db, repair_original_receipt)
        if video_acceptance_receipt is not None:
            from app.services.video_prediction_acceptance import recheck_credentials

            await recheck_credentials(self.db, video_acceptance_receipt)
        return anns

    async def _apply_secondary_inference(
        self,
        *,
        task: Task,
        annotation: Annotation,
        result: SecondaryInferenceResult,
        write_scope: LabelingWriteScope,
    ) -> tuple[Annotation, list[Annotation]]:
        """Apply immutable secondary-inference facts in the caller's transaction."""
        actor_id = write_scope.actor_id
        if actor_id is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        if (
            write_scope.task_id != task.id
            or write_scope.project_id != task.project_id
            or write_scope.source_annotation_id != annotation.id
        ):
            raise HTTPException(status_code=404, detail="annotation not found")
        prepared_shapes = []
        if result.write_target != "attributes":
            from app.services.prediction import to_internal_shape

            for frozen_shape in result.geometry_shapes:
                shape = to_internal_shape(thaw_json(frozen_shape))
                geometry = shape.get("geometry")
                if geometry:
                    try:
                        verified_content = await preflight_mask_payload_for_write(
                            self.db, task, geometry
                        )
                    except RasterMaskContractError as exc:
                        raise HTTPException(
                            status_code=exc.status_code, detail=exc.detail
                        ) from exc
                    prepared_shapes.append((shape, geometry, verified_content))
        task, _actor, _access = await lock_labeling_write(
            self.db, write_scope, operations=("secondary_inference",)
        )
        annotation = await get_core_row(
            self.db, annotation_row, write_scope.source_annotation_id
        )
        await record_annotation_actor(self.db, task, actor_id)

        if result.write_target == "attributes":
            current_attributes = dict(annotation.attributes or {})
            current_meta = dict(annotation.attributes_meta or {})
            model_ref = dict(result.model_ref)
            changed = False
            for key, frozen_value in result.attributes:
                if (
                    key in current_attributes
                    and (current_meta.get(key) or {}).get("origin") != "ai"
                ):
                    continue
                current_attributes[key] = thaw_json(frozen_value)
                current_meta[key] = {"origin": "ai", "model_ref": model_ref}
                changed = True
            if changed:
                written = await annotation_prediction_command(
                    self.db,
                    ApplySecondaryAttributes(
                        **annotation_prediction_context(task, actor_id),
                        operation="apply_secondary_attributes",
                        annotation_id=annotation.id,
                        expected_version=annotation.version,
                        attributes=current_attributes,
                        attributes_meta=current_meta,
                    ),
                )
                set_committed_value(annotation, "attributes", current_attributes)
                set_committed_value(annotation, "attributes_meta", current_meta)
                set_committed_value(annotation, "version", written.version)
                set_committed_value(annotation, "updated_at", written.updated_at)
                await touch_annotation_scene_track(
                    self.db, annotation, make_keyframe=False
                )
                await self.db.flush()
            return annotation, []

        children: list[Annotation] = []
        for shape, geometry, verified_content in prepared_shapes:
            class_name = shape.get("class_name") or annotation.class_name
            try:
                child = await self.create(
                    task_id=task.id,
                    user_id=actor_id,
                    annotation_type=shape.get("type", "bbox"),
                    class_name=class_name,
                    geometry=geometry,
                    confidence=shape.get("confidence"),
                    parent_annotation_id=annotation.id,
                    tool_unit_id=annotation.tool_unit_id,
                    video_segment_id=annotation.video_segment_id,
                    verified_mask_content=verified_content,
                )
            except HTTPException as exc:
                detail = str(getattr(exc, "detail", ""))
                if exc.status_code != 422 or "类别集合内" not in detail:
                    raise
                logger.info(
                    "secondary_inference: 子检测类名 %r 不在项目标签集, 回落 __unknown",
                    class_name,
                )
                child = await self.create(
                    task_id=task.id,
                    user_id=actor_id,
                    annotation_type=shape.get("type", "bbox"),
                    class_name="__unknown",
                    geometry=geometry,
                    confidence=shape.get("confidence"),
                    parent_annotation_id=annotation.id,
                    tool_unit_id=annotation.tool_unit_id,
                    video_segment_id=annotation.video_segment_id,
                    verified_mask_content=verified_content,
                )
            written = await annotation_prediction_command(
                self.db,
                MarkSecondaryChild(
                    **annotation_prediction_context(task, actor_id),
                    operation="mark_secondary_child",
                    annotation_id=child.id,
                    expected_version=child.version,
                    parent_annotation_id=annotation.id,
                ),
            )
            set_committed_value(child, "source", written.source)
            # The old source-only ORM UPDATE did not refresh Mask-trigger versions.
            set_committed_value(child, "updated_at", written.updated_at)
            children.append(child)
        if children:
            await self.db.flush()
        return annotation, children

    async def _reject_prediction(
        self,
        prediction_id: uuid.UUID,
        *,
        shape_index: int | None,
        write_scope: LabelingWriteScope,
    ) -> None:
        """Persist candidate rejection state for one task-scoped AI prediction."""
        actor_id = write_scope.actor_id
        if actor_id is None:
            raise HTTPException(status_code=401, detail="Authentication required")

        prediction = (
            await self.db.execute(
                select(prediction_row).where(prediction_row.id == prediction_id)
            )
        ).scalar_one_or_none()
        if (
            prediction is None
            or prediction.task_id != write_scope.task_id
            or prediction.project_id != write_scope.project_id
        ):
            raise HTTPException(status_code=404, detail="Prediction not found")
        await lock_labeling_write(
            self.db, write_scope, operations=("candidate_reject",)
        )

        total_shapes = len(prediction.result or [])
        if shape_index is not None and shape_index >= total_shapes:
            raise HTTPException(
                status_code=400,
                detail=f"shape_index {shape_index} out of range (0..{total_shapes - 1})",
            )

        from app.services.labeling_prediction_records import reject_prediction_candidate

        await reject_prediction_candidate(
            self.db,
            prediction_id=prediction.id,
            prediction_created_at=prediction.created_at,
            task_id=write_scope.task_id,
            project_id=write_scope.project_id,
            shape_index=shape_index,
        )

        from app.services.task_lock import TaskLockService

        await TaskLockService(self.db).heartbeat(write_scope.task_id, actor_id)

    async def list_by_task(
        self,
        task_id: uuid.UUID,
        include_cancelled: bool = False,
        video_segment_id: uuid.UUID | None = None,
    ) -> list[Annotation]:
        q = select(annotation_row).where(
            annotation_row.task_id == task_id,
            annotation_row.is_active.is_(True),
            annotation_row.sensor_role.is_(None),
        )
        if not include_cancelled:
            q = q.where(annotation_row.was_cancelled.is_(False))
        if video_segment_id is not None:
            q = q.where(annotation_row.video_segment_id == video_segment_id)
        q = q.order_by(annotation_row.created_at)
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
        q = select(annotation_row).where(
            annotation_row.task_id.in_(task_ids),
            annotation_row.is_active.is_(True),
            annotation_row.was_cancelled.is_(False),
            annotation_row.sensor_role.is_(None),
        )
        if track_id is not None:
            q = q.where(annotation_row.track_id == track_id)
        q = q.order_by(annotation_row.task_id, annotation_row.created_at)
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
        q = select(annotation_row).where(
            annotation_row.task_id == task_id,
            annotation_row.is_active.is_(True),
            annotation_row.sensor_role.is_(None),
        )
        if not include_cancelled:
            q = q.where(annotation_row.was_cancelled.is_(False))
        if video_segment_id is not None:
            q = q.where(annotation_row.video_segment_id == video_segment_id)
        if cursor is not None:
            cur_ts, cur_id = cursor
            q = q.where(
                (annotation_row.created_at < cur_ts)
                | ((annotation_row.created_at == cur_ts) & (annotation_row.id < cur_id))
            )
        q = q.order_by(
            annotation_row.created_at.desc(), annotation_row.id.desc()
        ).limit(limit)
        rows = list((await self.db.execute(q)).scalars().all())
        next_cursor: tuple[datetime, uuid.UUID] | None = None
        if len(rows) == limit and rows:
            tail = rows[-1]
            next_cursor = (tail.created_at, tail.id)
        return rows, next_cursor

    async def delete(
        self, annotation_id: uuid.UUID, *, actor_id: uuid.UUID | None = None
    ) -> bool:
        annotation = await get_core_row(self.db, annotation_row, annotation_id)
        if not annotation:
            return False
        project_id = annotation.project_id
        if project_id is None:
            project_id = await self.db.scalar(
                select(task_row.project_id).where(task_row.id == annotation.task_id)
            )
        if project_id is None:
            return False
        result = await annotation_write_command(
            self.db,
            DeleteAnnotation(
                schema=ANNOTATION_WRITE_PROTOCOL,
                operation="delete",
                annotation_id=annotation.id,
                task_id=annotation.task_id,
                project_id=project_id,
                actor_id=actor_id,
                expected_version=int(annotation.version or 1),
            ),
        )
        assert result.mutation is not None
        if not result.mutation.changed:
            return False
        await self._refresh_deleted_annotation_cache(
            annotation,
            deleted_camera_members=result.mutation.deleted_camera_members,
            deleted_children=result.mutation.deleted_children,
        )
        if annotation.scene_track_id is not None:
            await self._refresh_loaded_scene_track_fields(
                annotation.scene_track_id, ("revision", "updated_at")
            )
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
        actor_id: uuid.UUID | None = None,
        task_id: uuid.UUID | None = None,
        project_id: uuid.UUID | None = None,
    ) -> list[Annotation]:
        """I12 · 批量 patch N 个标注. 任一标注被锁/已软删则整体 422.

        调用方负责在外层加 task_access.assert_task_editable + assert_project_visible.
        """
        from fastapi import HTTPException

        if not ids:
            return []
        rows = (
            (
                await self.db.execute(
                    select(annotation_row).where(annotation_row.id.in_(ids))
                )
            )
            .scalars()
            .all()
        )
        if len(rows) != len(ids):
            missing = set(ids) - {r.id for r in rows}
            raise HTTPException(
                status_code=404,
                detail=f"annotations not found: {sorted(str(m) for m in missing)}",
            )
        result_order = [row.id for row in rows]
        if any(
            (row.geometry or {}).get("type")
            in {"raster_mask", "video_mask", "video_track_mask"}
            for row in rows
        ):
            result_order.sort()
        if (task_id is None) != (project_id is None):
            raise ValueError("task_id and project_id must be supplied together")
        patch = {
            key: value
            for key, value in {
                "class_name": class_name,
                "attributes": attributes,
                "z_order": z_order,
                "is_locked": is_locked,
                "is_hidden": is_hidden,
            }.items()
            if value is not None
        }
        result = await annotation_bulk_command(
            self.db,
            UpdateAnnotations(
                schema=ANNOTATION_BULK_PROTOCOL,
                operation="update",
                actor_id=actor_id,
                task_id=task_id,
                project_id=project_id,
                ids=ids,
                patch=AnnotationBulkPatch(**patch),
            ),
        )

        write_fields = set(patch)
        if "attributes" in patch:
            write_fields.add("attributes_meta")
        loaded = {row.id: row for row in rows}
        for snapshot in result.annotations:
            annotation = loaded.get(snapshot.id)
            if annotation is None:
                continue
            fields = {
                "class_name": snapshot.class_name,
                "attributes": snapshot.attributes,
                "attributes_meta": snapshot.attributes_meta,
                "z_order": snapshot.z_order,
                "is_locked": snapshot.is_locked,
                "is_hidden": snapshot.is_hidden,
            }
            state = inspect(annotation)
            for field, value in fields.items():
                if (
                    field in write_fields
                    or not state.attrs[field].history.has_changes()
                ):
                    set_committed_value(annotation, field, value)
            set_committed_value(annotation, "version", snapshot.version)
            set_committed_value(annotation, "updated_at", snapshot.updated_at)

        loaded_by_id = {
            item.id: item
            for item in self.db.identity_map.values()
            if isinstance(item, Annotation)
        }
        for snapshot in result.camera_members:
            annotation = loaded_by_id.get(snapshot.id)
            if annotation is None:
                continue
            state = inspect(annotation)
            if (
                "class_name" in patch
                or not state.attrs.class_name.history.has_changes()
            ):
                set_committed_value(annotation, "class_name", snapshot.class_name)
            set_committed_value(annotation, "version", snapshot.version)
            set_committed_value(annotation, "updated_at", snapshot.updated_at)

        loaded_tracks = {
            item.id: item
            for item in self.db.identity_map.values()
            if isinstance(item, SceneTrack)
        }
        for snapshot in result.tracks:
            track = loaded_tracks.get(snapshot.id)
            if track is None:
                continue
            state = inspect(track)
            if (
                "class_name" in patch
                or not state.attrs.class_name.history.has_changes()
            ):
                set_committed_value(track, "class_name", snapshot.class_name)
            set_committed_value(track, "revision", snapshot.revision)
            set_committed_value(track, "updated_at", snapshot.updated_at)
        await self.db.flush()
        return [loaded[annotation_id] for annotation_id in result_order]

    async def _resolve_axis_convention(self, task: Task) -> str | None:
        return await LabelingCrossFrameWriteService(self.db)._resolve_axis_convention(
            task,
        )

    async def _frame_pose(self, scene_id, frame_index):
        return await LabelingCrossFrameWriteService(self.db)._frame_pose(
            scene_id,
            frame_index,
        )

    async def _propagate(
        self,
        *,
        source_annotation_id: uuid.UUID,
        target_task_id: uuid.UUID,
        user_id: uuid.UUID,
        override_psr: dict | None = None,
    ) -> tuple[Annotation, bool]:
        return await LabelingCrossFrameWriteService(self.db)._propagate(
            source_annotation_id=source_annotation_id,
            target_task_id=target_task_id,
            user_id=user_id,
            override_psr=override_psr,
        )

    async def _propagate_batch(
        self,
        *,
        source_task_id: uuid.UUID,
        target_task_id: uuid.UUID,
        annotation_ids: list[uuid.UUID] | None,
        user_id: uuid.UUID,
    ) -> tuple[list[tuple[uuid.UUID, Annotation]], bool]:
        return await LabelingCrossFrameWriteService(self.db)._propagate_batch(
            source_task_id=source_task_id,
            target_task_id=target_task_id,
            annotation_ids=annotation_ids,
            user_id=user_id,
        )

    async def _interpolate_range(
        self,
        *,
        track_id: str,
        from_task_id: uuid.UUID,
        to_task_id: uuid.UUID,
        user_id: uuid.UUID,
        assert_task_editable=None,
        assert_task_visible=None,
        lock_task_for_write=None,
    ) -> tuple[list[Annotation], bool, list[int]]:
        return await LabelingCrossFrameWriteService(self.db)._interpolate_range(
            track_id=track_id,
            from_task_id=from_task_id,
            to_task_id=to_task_id,
            user_id=user_id,
            assert_task_editable=assert_task_editable,
            assert_task_visible=assert_task_visible,
            lock_task_for_write=lock_task_for_write,
        )

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
        actor_id: uuid.UUID | None = None,
    ) -> Annotation | None:
        """Surgical update of mutable fields. Increments version for optimistic concurrency."""
        annotation = await get_core_row(self.db, annotation_row, annotation_id)
        if not annotation or not annotation.is_active:
            return None
        if annotation.sensor_role is not None:
            raise HTTPException(
                status_code=422,
                detail="camera members must be edited through the point-cloud camera member API",
            )
        verified_mask_content = None
        if geometry is not None and not mask_payload_prepared:
            task = await get_core_row(self.db, task_row, annotation.task_id)
            verified_mask_content = await preflight_mask_payload_for_write(
                self.db, task, geometry
            )
        if class_name is not None:
            # v0.14.17 · 与 create / accept_prediction 对齐: PATCH 改类也走软校验,
            # 堵住"采纳后 PATCH 改成项目标签集外的任意非法值"的数据质量缺口.
            await self._validate_class_name(
                annotation.project_id, annotation.tool_unit_id, class_name
            )
        if geometry is not None:
            validate_geometry_type_transition(
                tool_unit_id=annotation.tool_unit_id,
                previous_geometry=annotation.geometry,
                next_geometry=geometry,
            )
            if not mask_payload_prepared:
                await prepare_mask_geometry_for_annotation_write(
                    self.db,
                    task,
                    geometry,
                    verified_content=verified_mask_content,
                    require_verified_content=verified_mask_content is not None,
                )
            try:
                geometry, next_track_id = prepare_compact_track_identity(
                    geometry,
                    annotation.track_id,
                    reject_identity_change=True,
                )
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        else:
            next_track_id = annotation.track_id
        next_geometry = geometry if geometry is not None else annotation.geometry
        if (next_geometry or {}).get("type") == "box_3d" and next_track_id is None:
            next_track_id = _new_track_id()

        project_id = annotation.project_id
        if project_id is None:
            project_id = await self.db.scalar(
                select(task_row.project_id).where(task_row.id == annotation.task_id)
            )
        if project_id is None:
            raise HTTPException(status_code=404, detail="Task not found")
        values: dict[str, object] = {
            "schema": ANNOTATION_WRITE_PROTOCOL,
            "operation": "update",
            "annotation_id": annotation.id,
            "task_id": annotation.task_id,
            "project_id": project_id,
            "actor_id": actor_id,
            "expected_version": int(annotation.version or 1),
            "track_id": next_track_id,
        }
        if geometry is not None:
            values["geometry"] = geometry
        if class_name is not None:
            values["class_name"] = class_name
        if confidence is not None:
            values["confidence"] = confidence
        if attributes is not None:
            # v0.20.10 · 先据旧值算 meta 同步（人工改动即认领），再替换 attributes。
            values["attributes_meta"] = _sync_attributes_meta(
                annotation.attributes, annotation.attributes_meta, attributes
            )
            values["attributes"] = attributes
        # v0.10.5 M4-β · shape 状态位字段级 PATCH（I15）
        if z_order is not None:
            values["z_order"] = z_order
        if is_locked is not None:
            values["is_locked"] = is_locked
        if is_hidden is not None:
            values["is_hidden"] = is_hidden
        scene_track_id = annotation.scene_track_id
        track_reclassified = (
            scene_track_id is not None
            and class_name is not None
            and class_name != annotation.class_name
        )
        result = await annotation_write_command(self.db, UpdateAnnotation(**values))
        assert result.mutation is not None

        if scene_track_id is not None:
            await self._refresh_loaded_scene_track_fields(
                scene_track_id,
                ("class_name", "revision", "updated_at")
                if track_reclassified
                else ("revision", "updated_at"),
            )
            if track_reclassified:
                await self._refresh_loaded_scene_track_cameras(scene_track_id)

        scene_track_bound = False
        if scene_track_id is None and (next_geometry or {}).get("type") == "box_3d":
            from app.services.labeling_scene import resolve_task_scene_frames

            scene_frame = (
                await resolve_task_scene_frames(self.db, [annotation.task_id])
            )[annotation.task_id]
            if scene_frame.scene_id is not None and scene_frame.frame_index is not None:
                await annotation_write_command(
                    self.db,
                    BindSceneTrackMember(
                        schema=ANNOTATION_WRITE_PROTOCOL,
                        operation="bind_scene_track_member",
                        annotation_id=annotation.id,
                        task_id=annotation.task_id,
                        project_id=project_id,
                        actor_id=None,
                        expected_version=result.mutation.version,
                        scene_id=scene_frame.scene_id,
                        frame_index=scene_frame.frame_index,
                        temporal_role=(
                            "keyframe"
                            if geometry is not None or class_name is not None
                            else annotation.temporal_role
                        ),
                    ),
                )
                scene_track_bound = True
        cache_fields = {"version", "updated_at"}
        if geometry is not None:
            cache_fields.update(("geometry", "annotation_type", "track_id"))
        elif (annotation.geometry or {}).get(
            "type"
        ) == "box_3d" and annotation.track_id is None:
            cache_fields.add("track_id")
        if class_name is not None:
            cache_fields.add("class_name")
        if confidence is not None:
            cache_fields.add("confidence")
        if attributes is not None:
            cache_fields.update(("attributes", "attributes_meta"))
        if z_order is not None:
            cache_fields.add("z_order")
        if is_locked is not None:
            cache_fields.add("is_locked")
        if is_hidden is not None:
            cache_fields.add("is_hidden")
        if (scene_track_id is not None and geometry is not None) or scene_track_bound:
            cache_fields.add("temporal_role")
        if scene_track_bound:
            cache_fields.add("scene_track_id")
        await self._refresh_loaded_annotation_fields(
            {annotation.id}, tuple(sorted(cache_fields))
        )
        if scene_track_bound and annotation.scene_track_id is not None:
            await self._refresh_loaded_scene_track_fields(
                annotation.scene_track_id, ("revision", "updated_at")
            )
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
        return await LabelingVideoBBoxEditingService(
            self.db
        ).convert_video_track_to_bboxes(
            task=task,
            annotation=annotation,
            user_id=user_id,
            operation=operation,
            scope=scope,
            frame_index=frame_index,
            frame_mode=frame_mode,
            frame_count=frame_count,
            max_created=max_created,
        )

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
        return await LabelingVideoBBoxEditingService(self.db).compose_video_tracks(
            task=task,
            annotations=annotations,
            user_id=user_id,
            operation=operation,
            frame_index=frame_index,
            delete_sources=delete_sources,
            gap_mode=gap_mode,
        )

    async def save_draft(
        self, task_id: uuid.UUID, user_id: uuid.UUID, result: dict
    ) -> AnnotationDraft:
        return await LabelingAnnotationMaintenanceService(self.db).save_draft(
            task_id, user_id, result
        )

    async def get_draft(
        self, task_id: uuid.UUID, user_id: uuid.UUID
    ) -> AnnotationDraft | None:
        return await LabelingAnnotationMaintenanceService(self.db).get_draft(
            task_id, user_id
        )

    async def cleanup_schema_orphan_annotations(
        self,
        *,
        project_id: uuid.UUID,
        class_names: set[str],
        attribute_keys: set[str],
        actor_id: uuid.UUID,
        dry_run: bool,
    ) -> dict[str, int | dict[str, int]]:
        return await LabelingAnnotationMaintenanceService(
            self.db
        ).cleanup_schema_orphan_annotations(
            project_id=project_id,
            class_names=class_names,
            attribute_keys=attribute_keys,
            actor_id=actor_id,
            dry_run=dry_run,
        )

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
        from app.services.labeling_annotation_statistics import (
            update_annotation_statistics,
        )

        await update_annotation_statistics(
            self.db,
            task_id,
            trigger_batch_transitions=trigger_batch_transitions,
        )
