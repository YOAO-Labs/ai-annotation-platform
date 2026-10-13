"""v0.10.54 · annotations[] 导入服务 (ADR-0028).

支持 AAP JSON v1.0 envelope 里的 annotations[] 数组导入。与 predictions_import.py
不同的写入路径: 直接落 Annotation 表（不走 PredictionService），geometry 透传（内部
格式直存，无需 LS shape 转换），user_id 归操作者，溯源写 attributes._imported。

详见 docs/adr/0028-annotations-import-semantics.md。
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.annotation import Annotation
from app.db.models.dataset import Scene
from app.db.models.project import Project
from app.db.models.scene_track import SceneTrack, SceneTrackInterval
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.schemas.aap_json import (
    AAPImportErrorEntry,
    AAPImportResult,
    AAPJsonV1Envelope,
    check_schema_major,
)
from app.schemas._jsonb_types import Geometry
from app.services.annotation_track_identity import prepare_compact_track_identity
from app.services.annotation_propagation import _new_track_id
from app.services.scene_track_domain import (
    SceneTrackIntegrityError,
    bind_annotation_to_scene_track,
)
from app.services.scene import get_scene_frame_task_map
from app.services.raster_mask_storage import (
    assert_raster_mask_write_enabled,
    resolve_mask_reference_objects,
    store_mask_reference_objects,
    validate_mask_geometry_for_task,
)
from app.services.task_matcher import resolve_task
from app.utils.raster_mask_rle import coco_rle_area

logger = logging.getLogger(__name__)

# source 允许集合（D2）
_ALLOWED_SOURCES = {"manual", "prediction_based", "interpolated"}

# geometry.type → tool_unit_id 派生映射（参照 predictions_import / prediction.py）
_GEOMETRY_TO_TOOL_UNIT: dict[str, str] = {
    "polyline": "polyline",
    "rotated_bbox": "rotated_bbox",
    "keypoint": "keypoint",
    "polygon": "region",
    "multi_polygon": "region",
    "raster_mask": "region",
    "video_mask": "region",
    "video_track_mask": "region",
    "video_polygon": "region",
    "video_polyline": "polyline",
    "video_rotated_bbox": "rotated_bbox",
    "video_keypoint": "keypoint",
}

_GEOMETRY_ADAPTER = TypeAdapter(Geometry)


def _validate_aap_mask_objects(
    geometry: Any,
    mask_objects: dict[str, dict[str, Any]],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    return resolve_mask_reference_objects(geometry, mask_objects)


def _derive_tool_unit(geometry_type: str | None) -> str:
    """按 geometry.type 派生 tool_unit_id; 其余 kind（含 bbox）→ bbox."""
    if geometry_type is None:
        return "bbox"
    return _GEOMETRY_TO_TOOL_UNIT.get(geometry_type, "bbox")


# ── overwrite 语义: 清理该 task 下 _imported==true 的 annotations ──────


async def _purge_imported_annotations(db: AsyncSession, task_id: uuid.UUID) -> None:
    """overwrite=true 时: 删除该 task 下 attributes._imported == 'true' 的标注行。

    只删导入子集，绝不碰人工标注。
    """
    scene_track_ids = list(
        (
            await db.execute(
                select(Annotation.scene_track_id)
                .where(Annotation.task_id == task_id)
                .where(Annotation.attributes["_imported"].astext == "true")
                .where(Annotation.scene_track_id.is_not(None))
                .distinct()
            )
        ).scalars()
    )
    await db.execute(
        delete(Annotation).where(
            Annotation.task_id == task_id,
            Annotation.attributes["_imported"].astext == "true",
        )
    )
    if scene_track_ids:
        await db.execute(
            update(SceneTrack)
            .where(SceneTrack.id.in_(scene_track_ids))
            .values(revision=SceneTrack.revision + 1)
        )
    await db.flush()


async def _apply_imported_scene_tracks(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    envelope: AAPJsonV1Envelope,
    operator_user_id: uuid.UUID,
    result: AAPImportResult,
) -> None:
    """Replace member-derived envelopes with exported authoritative intervals."""
    for entry in envelope.scene_tracks:
        query = (
            select(SceneTrack)
            .join(Scene, Scene.id == SceneTrack.scene_id)
            .where(SceneTrack.project_id == project_id)
            .where(SceneTrack.track_id == entry.track_id)
        )
        if entry.scene_name is not None:
            query = query.where(Scene.name == entry.scene_name)
        tracks = list((await db.execute(query.with_for_update())).scalars())
        if len(tracks) != 1:
            result.errors.append(
                AAPImportErrorEntry(
                    task_match={
                        "scene_name": entry.scene_name,
                        "track_id": entry.track_id,
                    },
                    reason="scene track metadata could not be matched uniquely",
                )
            )
            continue
        track = tracks[0]
        if track.class_name != entry.class_name:
            result.errors.append(
                AAPImportErrorEntry(
                    task_match={
                        "scene_name": entry.scene_name,
                        "track_id": entry.track_id,
                    },
                    reason="scene track class conflicts with imported members",
                )
            )
            continue
        ordered = sorted(
            entry.intervals,
            key=lambda row: (
                row.start_frame,
                row.end_frame if row.end_frame is not None else 2**31 - 1,
            ),
        )
        invalid = any(
            left.end_frame is None or left.end_frame + 1 >= right.start_frame
            for left, right in zip(ordered, ordered[1:], strict=False)
        )
        if invalid:
            result.errors.append(
                AAPImportErrorEntry(
                    task_match={
                        "scene_name": entry.scene_name,
                        "track_id": entry.track_id,
                    },
                    reason="scene track intervals overlap or are adjacent",
                )
            )
            continue
        frame_tasks = await get_scene_frame_task_map(db, track.scene_id)
        task_frames = {task_id: frame for frame, task_id in frame_tasks.items()}
        members = list(
            (
                await db.execute(
                    select(Annotation)
                    .where(Annotation.scene_track_id == track.id)
                    .where(Annotation.is_active.is_(True))
                    .where(Annotation.was_cancelled.is_(False))
                )
            ).scalars()
        )
        member_frames = [
            task_frames[row.task_id] for row in members if row.task_id in task_frames
        ]
        if any(
            not any(
                interval.start_frame <= frame
                and (interval.end_frame is None or frame <= interval.end_frame)
                for interval in ordered
            )
            for frame in member_frames
        ):
            result.errors.append(
                AAPImportErrorEntry(
                    task_match={
                        "scene_name": entry.scene_name,
                        "track_id": entry.track_id,
                    },
                    reason="scene track intervals do not cover every imported member",
                )
            )
            continue
        await db.execute(
            delete(SceneTrackInterval).where(
                SceneTrackInterval.scene_track_id == track.id
            )
        )
        for interval in ordered:
            db.add(
                SceneTrackInterval(
                    id=uuid.uuid4(),
                    scene_track_id=track.id,
                    start_frame=interval.start_frame,
                    end_frame=interval.end_frame,
                    source="imported",
                    version=1,
                    created_by=operator_user_id,
                )
            )
        track.attributes = dict(entry.attributes or {})
        track.attributes_meta = dict(entry.attributes_meta or {})
        track.presence_mode = entry.presence_mode
        track.revision = int(track.revision or 1) + 1
    await db.flush()


# ── AAP JSON annotations 导入主函数 ──────────────────────────────────


async def import_aap_json_annotations(
    db: AsyncSession,
    project_id: uuid.UUID,
    file_bytes: bytes,
    *,
    operator_user_id: uuid.UUID,
    overwrite: bool = False,
    dry_run: bool = False,
) -> AAPImportResult:
    """导入 AAP JSON envelope 里的 annotations[] 数组。

    语义决策见 ADR-0028:
    - D1 · user_id 归操作者, 原 user_id 转存 attributes._imported_user_id
    - D2 · 保留原始 source（允许集合内）, attributes._imported=true 标记
    - D3 · 默认 append; overwrite 时仅清理 _imported==true 子集
    - D5 · created_at 保留原值（若有）, was_cancelled=False, lead_time 不设
    - D6 · 循环结束后批量更新 task 统计, 抑制 batch 自动流转
    """
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

    result = AAPImportResult(dry_run=dry_run)
    purged_tasks: set[uuid.UUID] = set()
    affected_tasks: set[uuid.UUID] = set()
    stored_mask_keys: set[str] = set()
    dry_run_primary_tracks: set[tuple[uuid.UUID, str]] = set()
    dry_run_camera_members: set[tuple[uuid.UUID, str, str]] = set()

    # 预先查好 project，以便 class_name 软校验（避免逐条 db.get）
    project = await db.get(Project, project_id)
    project_tool_bindings: dict[str, Any] = (
        project.tool_bindings or {} if project else {}
    )

    from app.services.project import lookup_classes_for_tool_unit

    for block in envelope.tasks:
        match_dict = block.task_match.model_dump(exclude_none=True)
        # 兜底兼容: task block 顶层 file_path
        if "file_path" not in match_dict and block.file_path:
            match_dict["file_path"] = block.file_path

        if not block.annotations:
            continue

        task = await resolve_task(db, project_id, match_dict)
        if task is None:
            for _ in block.annotations:
                result.errors.append(
                    AAPImportErrorEntry(
                        task_match=match_dict,
                        reason="task not found in project",
                    )
                )
                result.skipped += 1
            continue

        if not dry_run:
            # A2 · an import envelope may visit tasks in any order, so acquire the
            # task row with NOWAIT before the overwrite purge / inserts: a busy
            # lock rolls back to a retryable 409 instead of forming a wait cycle.
            from app.services.annotation_evidence import lock_tasks_for_evidence

            await lock_tasks_for_evidence(db, [task.id], nowait=True)

        # 先建主标注/SceneTrack，再挂同轨迹的相机成员；交换格式不要求调用方排序。
        for entry in sorted(
            block.annotations, key=lambda item: item.sensor_role is not None
        ):
            # 1. class_name 缺失检查
            if not entry.class_name:
                result.errors.append(
                    AAPImportErrorEntry(
                        task_match=match_dict,
                        reason="missing class_name",
                    )
                )
                result.skipped += 1
                continue

            # 2. geometry 有效性检查
            if not isinstance(entry.geometry, dict) or not entry.geometry.get("type"):
                result.errors.append(
                    AAPImportErrorEntry(
                        task_match=match_dict,
                        reason="invalid geometry: must be dict with 'type' field",
                    )
                )
                result.skipped += 1
                continue

            try:
                validated_geometry = _GEOMETRY_ADAPTER.validate_python(entry.geometry)
                entry.geometry = validated_geometry.model_dump(
                    mode="json", by_alias=True, exclude_unset=True
                )
                mask_objects = _validate_aap_mask_objects(
                    entry.geometry, envelope.mask_objects
                )
                await validate_mask_geometry_for_task(db, task, entry.geometry)
                if entry.geometry.get("type") == "raster_mask" and any(
                    coco_rle_area(rle) == 0 for _, rle in mask_objects
                ):
                    raise ValueError("raster mask must contain foreground pixels")
                if entry.geometry.get("type") == "raster_mask":
                    assert_raster_mask_write_enabled(project)
            except ValidationError as exc:
                result.errors.append(
                    AAPImportErrorEntry(
                        task_match=match_dict,
                        reason=f"invalid geometry: {exc.errors()[:2]}",
                    )
                )
                result.skipped += 1
                continue
            except ValueError as exc:
                result.errors.append(
                    AAPImportErrorEntry(
                        task_match=match_dict, reason=f"invalid geometry: {exc}"
                    )
                )
                result.skipped += 1
                continue

            # 3. tool_unit_id 派生
            tool_unit_id = entry.tool_unit_id or _derive_tool_unit(
                entry.geometry.get("type")
            )

            # 4. class_name 软校验（失败不让整批失败，累计 errors[] + skip）
            allowed = lookup_classes_for_tool_unit(project_tool_bindings, tool_unit_id)
            if (
                allowed
                and entry.class_name != "__unknown"
                and entry.class_name not in allowed
            ):
                result.errors.append(
                    AAPImportErrorEntry(
                        task_match=match_dict,
                        reason=(
                            f"class_name '{entry.class_name}' 不在工具单位 "
                            f"'{tool_unit_id}' 的类别集合内"
                        ),
                    )
                )
                result.skipped += 1
                continue

            # 5. source 语义（D2）: 保留原值，不在允许集合则默认 manual
            source = entry.source if entry.source in _ALLOWED_SOURCES else "manual"

            # 6. attributes（D1+D2）: 在 entry.attributes 基础上合并溯源标记
            attributes: dict[str, Any] = dict(entry.attributes or {})
            attributes["_imported"] = True
            if entry.user_id is not None:
                attributes["_imported_user_id"] = str(entry.user_id)

            if not dry_run:
                # 只在首条通过基础校验的 entry 即将写入时执行 overwrite，保持
                # 正式导入既有的清理顺序；dry-run 在下方查询中模拟这次清理。
                if overwrite and task.id not in purged_tasks:
                    await _purge_imported_annotations(db, task.id)
                    purged_tasks.add(task.id)

                if mask_objects:
                    unstored = [
                        item
                        for item in mask_objects
                        if str(item[0]["object_key"]) not in stored_mask_keys
                    ]
                    await store_mask_reference_objects(
                        db,
                        entry.geometry,
                        unstored,
                        task_id=task.id,
                    )
                    stored_mask_keys.update(
                        str(reference["object_key"]) for reference, _ in unstored
                    )

            geometry, track_id = prepare_compact_track_identity(
                entry.geometry, entry.track_id
            )
            if geometry.get("type") == "box_3d" and track_id is None:
                track_id = _new_track_id()
            temporal_role = entry.temporal_role or (
                "derived" if source == "interpolated" else "sample"
            )
            sensor_item_id = None
            scene_track_id = None
            if entry.sensor_role is not None:
                assert track_id is not None
                sensor_item_id = await db.scalar(
                    select(TaskDatasetItemLink.dataset_item_id)
                    .where(TaskDatasetItemLink.task_id == task.id)
                    .where(TaskDatasetItemLink.role == entry.sensor_role)
                )
                if sensor_item_id is None:
                    result.errors.append(
                        AAPImportErrorEntry(
                            task_match=match_dict,
                            reason=f"camera role '{entry.sensor_role}' is not linked to task",
                        )
                    )
                    result.skipped += 1
                    continue
                primary_query = (
                    select(Annotation.scene_track_id)
                    .where(Annotation.task_id == task.id)
                    .where(Annotation.track_id == track_id)
                    .where(Annotation.sensor_role.is_(None))
                    .where(Annotation.scene_track_id.is_not(None))
                    .where(Annotation.is_active.is_(True))
                )
                if dry_run and overwrite:
                    primary_query = primary_query.where(
                        func.coalesce(
                            Annotation.attributes["_imported"].astext, "false"
                        )
                        != "true"
                    )
                scene_track_id = await db.scalar(primary_query.limit(1))
                if (
                    scene_track_id is None
                    and (
                        task.id,
                        track_id,
                    )
                    not in dry_run_primary_tracks
                ):
                    result.errors.append(
                        AAPImportErrorEntry(
                            task_match=match_dict,
                            reason=(
                                f"camera member track '{track_id}' has no primary 3D annotation"
                            ),
                        )
                    )
                    result.skipped += 1
                    continue
                camera_member_key = (task.id, track_id, entry.sensor_role)
                if dry_run and camera_member_key in dry_run_camera_members:
                    result.errors.append(
                        AAPImportErrorEntry(
                            task_match=match_dict,
                            reason=(
                                f"camera member for role '{entry.sensor_role}' already exists"
                            ),
                        )
                    )
                    result.skipped += 1
                    continue
                if scene_track_id is not None:
                    camera_member_query = (
                        select(Annotation.id)
                        .where(Annotation.task_id == task.id)
                        .where(Annotation.scene_track_id == scene_track_id)
                        .where(Annotation.sensor_role == entry.sensor_role)
                        .where(Annotation.is_active.is_(True))
                        .where(Annotation.was_cancelled.is_(False))
                    )
                    if dry_run and overwrite:
                        camera_member_query = camera_member_query.where(
                            func.coalesce(
                                Annotation.attributes["_imported"].astext, "false"
                            )
                            != "true"
                        )
                    existing_camera_member = await db.scalar(
                        camera_member_query.limit(1)
                    )
                    if existing_camera_member is not None:
                        result.errors.append(
                            AAPImportErrorEntry(
                                task_match=match_dict,
                                reason=(
                                    f"camera member for role '{entry.sensor_role}' already exists"
                                ),
                            )
                        )
                        result.skipped += 1
                        continue

            # 7. dry_run: 完成关系校验后只计数不入库
            if dry_run:
                if entry.sensor_role is not None:
                    dry_run_camera_members.add((task.id, track_id, entry.sensor_role))
                elif geometry.get("type") == "box_3d" and track_id is not None:
                    dry_run_primary_tracks.add((task.id, track_id))
                result.imported += 1
                continue

            # 8. 构造 Annotation 行直接 db.add（不走 AnnotationService.create，
            #    因为它会逐条触发 _update_task_stats 并可能推进 batch 状态）
            ann_kwargs: dict[str, Any] = dict(
                id=uuid.uuid4(),
                task_id=task.id,
                project_id=project_id,
                user_id=operator_user_id,  # D1
                source=source,  # D2
                annotation_type=entry.geometry["type"],
                tool_unit_id=tool_unit_id,
                class_name=entry.class_name,
                geometry=geometry,  # geometry 透传内部格式，无需 LS 转换
                track_id=track_id,
                confidence=entry.confidence,
                was_cancelled=False,  # D5
                ground_truth=False,  # D5
                attributes=attributes,  # D1+D2
                temporal_role=temporal_role,
                scene_track_id=scene_track_id,
                sensor_dataset_item_id=sensor_item_id,
                sensor_role=entry.sensor_role,
                sensor_visibility=entry.sensor_visibility,
                calibration_revision=entry.calibration_revision,
                calibration_digest=entry.calibration_digest,
            )
            # D5: created_at 若 entry 提供则显式设置，否则走 server_default now()
            if entry.created_at is not None:
                ann_kwargs["created_at"] = entry.created_at

            annotation = Annotation(**ann_kwargs)
            try:
                async with db.begin_nested():
                    db.add(annotation)
                    if scene_track_id is not None:
                        track = await db.get(SceneTrack, scene_track_id)
                        if track is None:
                            raise SceneTrackIntegrityError(
                                "scene_track_missing",
                                "camera member SceneTrack was not found",
                            )
                        track.revision += 1
                        await db.flush()
                    else:
                        await bind_annotation_to_scene_track(
                            db,
                            annotation=annotation,
                            task=task,
                            temporal_role=temporal_role,
                            interval_source="imported",
                            actor_id=operator_user_id,
                        )
            except SceneTrackIntegrityError as exc:
                result.errors.append(
                    AAPImportErrorEntry(
                        task_match=match_dict,
                        reason=f"scene track conflict ({exc.code}): {exc}",
                    )
                )
                result.skipped += 1
                continue
            affected_tasks.add(task.id)
            result.imported += 1

    # D6: 循环结束后批量更新受影响 task 统计，抑制 batch 自动流转
    if affected_tasks and not dry_run:
        await db.flush()
        await _apply_imported_scene_tracks(
            db,
            project_id=project_id,
            envelope=envelope,
            operator_user_id=operator_user_id,
            result=result,
        )
        from app.services.annotation import AnnotationService
        from app.services.annotation_evidence import record_annotation_actors_for_tasks

        # D1 · every imported annotation is attributed to the operator, including
        # camera members and scene-track members. Recording is conservative:
        # a legacy task with an unknown accumulator stays unknown.
        await record_annotation_actors_for_tasks(
            db, affected_tasks, operator_user_id, nowait=True
        )

        svc = AnnotationService(db)
        for task_id in affected_tasks:
            await svc._update_task_stats(task_id, trigger_batch_transitions=False)

    return result
