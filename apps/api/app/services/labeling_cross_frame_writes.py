"""Execute cross-frame annotation writes through Labeling's native owner."""

from __future__ import annotations

import copy
import json
import uuid

from fastapi import HTTPException
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import make_transient_to_detached
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.orm.util import identity_key

from aap_labeling.cross_frame_write_contract import (
    CROSS_FRAME_WRITE_FUNCTION,
    CrossFrameAnnotationSnapshot,
    CrossFrameSceneTrackEnsure,
    CrossFrameSourceFence,
    CrossFrameSourceSnapshot,
    CrossFrameTaskFence,
    CrossFrameTargetWrite,
    CrossFrameWriteCommand,
    CrossFrameWriteData,
    CrossFrameWriteUnavailable,
    cross_frame_write_command,
)
from aap_labeling.cross_frame_write_data import CrossFrameWriteDataClient
from aap_labeling.cross_frame_write_policy import (
    PROPAGATABLE_GEOMETRY_TYPES,
    compensate_psr,
    interpolate_psr,
)

from app.db.models.annotation import Annotation
from app.db.models.task import Task
from app.services.labeling_core_read_relations import (
    annotation_row,
    get_core_row,
    task_row,
)
from app.services.annotation_evidence import (
    record_annotation_actor,
    record_annotation_actors_for_tasks,
)
from app.services.annotation_propagation import _PropagateContext, _new_track_id
from app.services.labeling_annotation_statistics import update_annotation_statistics
from app.services.scene_track_domain import _attach_scene_track


_CROSS_FRAME_STEP = text(
    f"SELECT {CROSS_FRAME_WRITE_FUNCTION.partition('(')[0]}(CAST(:request AS jsonb))"
)
_LOCK_FAILURES = {"55P03", "40P01", "40001"}
_SOURCE_SYNC_FIELDS = (
    "track_id",
    "scene_track_id",
    "temporal_role",
    "version",
    "updated_at",
)


def _native_error_fields(error: BaseException):
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        diagnostic = getattr(current, "diag", None)
        message = getattr(current, "message", None) or getattr(
            diagnostic, "message_primary", None
        )
        detail = getattr(current, "detail", None) or getattr(
            diagnostic, "message_detail", None
        )
        sqlstate = getattr(current, "sqlstate", None) or getattr(
            current, "pgcode", None
        )
        if (
            message == "aap_labeling_cross_frame_write_error"
            or sqlstate in _LOCK_FAILURES
        ):
            return message, detail, sqlstate
        for name in ("orig", "__cause__", "__context__"):
            child = getattr(current, name, None)
            if isinstance(child, BaseException):
                pending.append(child)
    return None, None, None


class LabelingCrossFrameWriteService:
    """Keep the legacy AnnotationService tuple results and caller transaction."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _command(
        self, request: CrossFrameWriteCommand, *, flush_before: bool = False
    ) -> CrossFrameWriteData:
        if flush_before and self.db.autoflush:
            await self.db.flush()

        async def execute(request_json: str):
            with self.db.no_autoflush:
                return await self.db.scalar(
                    _CROSS_FRAME_STEP, {"request": request_json}
                )

        try:
            return await CrossFrameWriteDataClient(execute).command(request)
        except CrossFrameWriteUnavailable as exc:
            raise HTTPException(
                status_code=503,
                detail={"reason": "cross_frame_write_unavailable"},
            ) from exc
        except DBAPIError as exc:
            message, detail, sqlstate = _native_error_fields(exc)
            if message == "aap_labeling_cross_frame_write_error":
                try:
                    payload = json.loads(detail or "{}")
                    raise HTTPException(
                        status_code=int(payload.get("status_code", 409)),
                        detail=payload.get(
                            "detail", {"reason": "cross_frame_write_conflict"}
                        ),
                    ) from exc
                except (TypeError, ValueError, json.JSONDecodeError):
                    raise HTTPException(
                        status_code=409,
                        detail={"reason": "cross_frame_write_conflict"},
                    ) from exc
            if sqlstate in _LOCK_FAILURES:
                raise HTTPException(
                    status_code=409, detail={"reason": "task_busy"}
                ) from exc
            raise

    async def _load_created(
        self, snapshots: list[CrossFrameAnnotationSnapshot]
    ) -> list[Annotation]:
        annotations: list[Annotation] = []
        for snapshot in snapshots:
            annotation = Annotation(**snapshot.model_dump(mode="python"))
            make_transient_to_detached(annotation)
            annotations.append(await self.db.merge(annotation, load=False))
        return annotations

    def _sync_sources(
        self,
        snapshots: list[CrossFrameSourceSnapshot],
    ) -> None:
        for snapshot in snapshots:
            annotation = self.db.sync_session.identity_map.get(
                identity_key(Annotation, ident=snapshot.annotation_id)
            )
            if annotation is None:
                continue
            state = inspect(annotation)
            written = set(snapshot.written_fields)
            for field in _SOURCE_SYNC_FIELDS:
                value = getattr(snapshot, field)
                if (
                    field in written
                    or field in state.expired_attributes
                    or not state.attrs[field].history.has_changes()
                ):
                    set_committed_value(annotation, field, value)

    def _task_fence(self, task: Task) -> CrossFrameTaskFence:
        return CrossFrameTaskFence(
            task_id=task.id,
            project_id=task.project_id,
            expected_version=int(task.version or 1),
        )

    def _source_fence(
        self, annotation: Annotation, *, target_track_id: str | None
    ) -> CrossFrameSourceFence:
        geometry_type = str((annotation.geometry or {}).get("type") or "")
        return CrossFrameSourceFence(
            annotation_id=annotation.id,
            task_id=annotation.task_id,
            project_id=annotation.project_id,
            expected_version=int(annotation.version or 1),
            geometry_type=geometry_type,
            class_name=annotation.class_name,
            expected_track_id=annotation.track_id,
            target_track_id=target_track_id,
            expected_scene_track_id=annotation.scene_track_id,
            expected_temporal_role=annotation.temporal_role,
        )

    def _target_write(
        self, annotation: Annotation, *, source_annotation_id: uuid.UUID
    ) -> CrossFrameTargetWrite:
        return CrossFrameTargetWrite(
            annotation_id=annotation.id,
            source_annotation_id=source_annotation_id,
            task_id=annotation.task_id,
            project_id=annotation.project_id,
            video_segment_id=annotation.video_segment_id,
            source=annotation.source,
            annotation_type=annotation.annotation_type,
            tool_unit_id=annotation.tool_unit_id,
            class_name=annotation.class_name,
            geometry=copy.deepcopy(annotation.geometry or {}),
            track_id=annotation.track_id,
            attributes=copy.deepcopy(annotation.attributes or {}),
            temporal_role=annotation.temporal_role,
        )

    def _scene_track_ensure(
        self,
        *,
        project_id: uuid.UUID,
        scene_id: uuid.UUID,
        track_id: str,
        class_name: str,
        frames: set[int],
    ) -> CrossFrameSceneTrackEnsure:
        return CrossFrameSceneTrackEnsure(
            project_id=project_id,
            scene_id=scene_id,
            track_id=track_id,
            class_name=class_name,
            frames=sorted(frames),
            interval_source="derived",
        )

    async def _execute_write(
        self,
        *,
        operation: str,
        actor_id: uuid.UUID,
        tasks: list[Task],
        sources: list[CrossFrameSourceFence],
        targets: list[CrossFrameTargetWrite],
        scene_tracks: list[CrossFrameSceneTrackEnsure],
    ) -> list[Annotation]:
        unique_tasks = {task.id: task for task in tasks}
        request = cross_frame_write_command(
            operation,
            actor_id=actor_id,
            task_fences=[self._task_fence(task) for task in unique_tasks.values()],
            source_annotations=sources,
            target_annotations=targets,
            scene_tracks=scene_tracks,
        )
        result = await self._command(request, flush_before=bool(scene_tracks))
        self._sync_sources(result.sources)
        for snapshot in result.scene_tracks:
            await _attach_scene_track(
                self.db,
                snapshot.track,
                changed_fields=("revision", "updated_at") if snapshot.changed else (),
            )
        return await self._load_created(result.annotations)

    async def _resolve_axis_convention(self, task: Task) -> str | None:
        """Read the target dataset convention through Assets' public spatial API."""
        from app.services.labeling_scene import read_spatial_item_facts
        from app.services.labeling_scene import resolve_primary_item_id

        primary_item_id = await resolve_primary_item_id(self.db, task)
        if primary_item_id is None:
            return None
        item_facts, _ = await read_spatial_item_facts(self.db, [primary_item_id])
        item = item_facts.get(primary_item_id)
        return item.axis_convention if item is not None else None

    async def _frame_pose(self, scene_id, frame_index):
        """Read one scene pose through Assets' public spatial API."""
        if scene_id is None or frame_index is None:
            return None
        from app.services.assets_spatial import AssetsSpatialGateway

        return await AssetsSpatialGateway(self.db).read_frame_pose(
            scene_id, frame_index
        )

    async def _resolve_propagate_context(
        self, *, src_task: Task, target_task: Task
    ) -> _PropagateContext:
        if target_task.id == src_task.id:
            raise HTTPException(
                status_code=422, detail="propagate 目标不能是源 task 自身"
            )
        if src_task.project_id != target_task.project_id:
            raise HTTPException(status_code=422, detail="跨 project propagate 不被允许")

        from app.services.labeling_scene import read_spatial_item_facts
        from app.services.labeling_scene import resolve_primary_item_id

        src_item_id = await resolve_primary_item_id(self.db, src_task)
        target_item_id = await resolve_primary_item_id(self.db, target_task)
        item_facts, _ = await read_spatial_item_facts(
            self.db,
            [
                item_id
                for item_id in (src_item_id, target_item_id)
                if item_id is not None
            ],
        )
        src_item = item_facts.get(src_item_id) if src_item_id is not None else None
        target_item = (
            item_facts.get(target_item_id) if target_item_id is not None else None
        )
        src_scene_id = src_item.scene_id if src_item is not None else None
        target_scene_id = target_item.scene_id if target_item is not None else None
        src_frame_index = src_item.frame_index if src_item is not None else None
        target_frame_index = (
            target_item.frame_index if target_item is not None else None
        )
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
        if src is None or not src.is_active:
            raise HTTPException(status_code=404, detail="source annotation not found")

        geometry_type = (src.geometry or {}).get("type")
        if geometry_type not in PROPAGATABLE_GEOMETRY_TYPES:
            raise HTTPException(
                status_code=422,
                detail=f"geometry type '{geometry_type}' 不支持跨帧 propagate",
            )

        track_id = src.track_id if src.track_id is not None else _new_track_id()
        geometry = copy.deepcopy(src.geometry or {})
        motion_compensated = False
        if geometry_type == "box_3d":
            await self._ensure_box3d_context(ctx)
            geometry["convention_at_create"] = ctx.axis_convention
            if override_psr:
                for key in ("center", "size", "rotation"):
                    if key in override_psr:
                        geometry[key] = override_psr[key]
            elif all(key in geometry for key in ("center", "size", "rotation")):
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

        return (
            Annotation(
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
                temporal_role="derived" if geometry_type == "box_3d" else "sample",
            ),
            motion_compensated,
        )

    async def _propagate(
        self,
        *,
        source_annotation_id: uuid.UUID,
        target_task_id: uuid.UUID,
        user_id: uuid.UUID,
        override_psr: dict | None = None,
    ) -> tuple[Annotation, bool]:
        src = await get_core_row(
            self.db, annotation_row, source_annotation_id, populate_existing=True
        )
        if src is None or not src.is_active:
            raise HTTPException(status_code=404, detail="source annotation not found")
        target_task = await get_core_row(self.db, task_row, target_task_id)
        if target_task is None:
            raise HTTPException(status_code=404, detail="target task not found")
        src_task = await get_core_row(self.db, task_row, src.task_id)
        if src_task is None:
            raise HTTPException(status_code=404, detail="source task not found")

        await record_annotation_actor(self.db, target_task, user_id)
        context = await self._resolve_propagate_context(
            src_task=src_task, target_task=target_task
        )
        new_annotation, motion_compensated = await self._propagate_one(
            src=src, ctx=context, user_id=user_id, override_psr=override_psr
        )
        geometry_type = (src.geometry or {}).get("type")
        source = self._source_fence(src, target_track_id=new_annotation.track_id)
        if geometry_type == "box_3d":
            assert context.src_frame_index is not None
            assert context.target_frame_index is not None
            assert context.target_scene_id is not None
        scene_tracks = (
            [
                self._scene_track_ensure(
                    project_id=target_task.project_id,
                    scene_id=context.target_scene_id,
                    track_id=new_annotation.track_id,
                    class_name=src.class_name,
                    frames={context.src_frame_index, context.target_frame_index},
                )
            ]
            if geometry_type == "box_3d"
            else []
        )
        created = await self._execute_write(
            operation="propagate",
            actor_id=user_id,
            tasks=[src_task, target_task],
            sources=[source],
            targets=[
                self._target_write(
                    new_annotation, source_annotation_id=source_annotation_id
                )
            ],
            scene_tracks=scene_tracks,
        )
        await self.db.flush()
        await update_annotation_statistics(self.db, target_task_id)
        return created[0], motion_compensated

    async def _propagate_batch(
        self,
        *,
        source_task_id: uuid.UUID,
        target_task_id: uuid.UUID,
        annotation_ids: list[uuid.UUID] | None,
        user_id: uuid.UUID,
    ) -> tuple[list[tuple[uuid.UUID, Annotation]], bool]:
        if annotation_ids is None:
            rows = list(
                (
                    await self.db.execute(
                        select(annotation_row)
                        .where(annotation_row.task_id == source_task_id)
                        .where(annotation_row.is_active.is_(True))
                        .where(annotation_row.geometry["type"].astext == "box_3d")
                        .order_by(annotation_row.created_at, annotation_row.id)
                        .execution_options(populate_existing=True)
                    )
                ).scalars()
            )
            if not rows:
                raise HTTPException(
                    status_code=422, detail="源 task 没有可延续的 box_3d 标注"
                )
        else:
            if not annotation_ids:
                raise HTTPException(status_code=422, detail="annotation_ids 不能为空")
            rows = list(
                (
                    await self.db.execute(
                        select(annotation_row)
                        .where(annotation_row.id.in_(annotation_ids))
                        .execution_options(populate_existing=True)
                    )
                ).scalars()
            )
            by_id = {row.id: row for row in rows}
            missing = [
                str(annotation_id)
                for annotation_id in annotation_ids
                if annotation_id not in by_id
                or not by_id[annotation_id].is_active
                or by_id[annotation_id].task_id != source_task_id
            ]
            if missing:
                raise HTTPException(
                    status_code=404,
                    detail=f"annotations 不存在/不属于源 task: {', '.join(missing)}",
                )
            rows = [by_id[annotation_id] for annotation_id in annotation_ids]

        src_task = await get_core_row(self.db, task_row, source_task_id)
        if src_task is None:
            raise HTTPException(status_code=404, detail="source task not found")
        target_task = await get_core_row(self.db, task_row, target_task_id)
        if target_task is None:
            raise HTTPException(status_code=404, detail="target task not found")
        await record_annotation_actor(self.db, target_task, user_id)
        context = await self._resolve_propagate_context(
            src_task=src_task, target_task=target_task
        )

        staged: list[tuple[uuid.UUID, Annotation]] = []
        compensated_any = False
        sources_by_id: dict[uuid.UUID, CrossFrameSourceFence] = {}
        track_id_by_source: dict[uuid.UUID, str | None] = {}
        writes: list[CrossFrameTargetWrite] = []
        scene_tracks: list[CrossFrameSceneTrackEnsure] = []
        for src in rows:
            new_annotation, compensated = await self._propagate_one(
                src=src, ctx=context, user_id=user_id
            )
            compensated_any = compensated_any or compensated
            if src.id in track_id_by_source:
                new_annotation.track_id = track_id_by_source[src.id]
            else:
                track_id_by_source[src.id] = new_annotation.track_id
                sources_by_id[src.id] = self._source_fence(
                    src, target_track_id=new_annotation.track_id
                )
            writes.append(
                self._target_write(new_annotation, source_annotation_id=src.id)
            )
            staged.append((src.id, new_annotation))
            if (src.geometry or {}).get("type") == "box_3d":
                assert context.src_frame_index is not None
                assert context.target_frame_index is not None
                assert context.target_scene_id is not None
                scene_tracks.append(
                    self._scene_track_ensure(
                        project_id=target_task.project_id,
                        scene_id=context.target_scene_id,
                        track_id=new_annotation.track_id,
                        class_name=src.class_name,
                        frames={context.src_frame_index, context.target_frame_index},
                    )
                )

        created = await self._execute_write(
            operation="propagate_batch",
            actor_id=user_id,
            tasks=[src_task, target_task],
            sources=list(sources_by_id.values()),
            targets=writes,
            scene_tracks=scene_tracks,
        )
        await self.db.flush()
        await update_annotation_statistics(self.db, target_task_id)
        return [
            (source_id, annotation)
            for (source_id, _), annotation in zip(staged, created, strict=True)
        ], compensated_any

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
        from aap_assets_catalog.spatial_contract import ReadPosesRequest
        from app.services.assets_spatial import AssetsSpatialGateway
        from app.services.labeling_scene import (
            get_scene_frame_task_map,
            read_spatial_item_facts,
            resolve_primary_item_id,
        )

        if from_task_id == to_task_id:
            raise HTTPException(status_code=422, detail="插值区间两端不能是同一 task")
        from_task = await get_core_row(self.db, task_row, from_task_id)
        to_task = await get_core_row(self.db, task_row, to_task_id)
        if from_task is None or to_task is None:
            raise HTTPException(status_code=404, detail="task not found")
        if from_task.project_id != to_task.project_id:
            raise HTTPException(status_code=422, detail="跨 project 插值不被允许")

        await record_annotation_actors_for_tasks(
            self.db, [from_task.id, to_task.id], user_id, nowait=True
        )

        async def scene_frame(task: Task) -> tuple[uuid.UUID | None, int | None]:
            item_id = await resolve_primary_item_id(self.db, task)
            if item_id is None:
                return None, None
            item_facts, _ = await read_spatial_item_facts(self.db, [item_id])
            item = item_facts.get(item_id)
            if item is None:
                return None, None
            return item.scene_id, item.frame_index

        from_scene, from_frame = await scene_frame(from_task)
        to_scene, to_frame = await scene_frame(to_task)
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

        async def endpoint_box(task_id: uuid.UUID) -> Annotation:
            rows = list(
                (
                    await self.db.execute(
                        select(annotation_row)
                        .where(annotation_row.task_id == task_id)
                        .where(annotation_row.track_id == track_id)
                        .where(annotation_row.is_active.is_(True))
                        .where(annotation_row.geometry["type"].astext == "box_3d")
                        .execution_options(populate_existing=True)
                    )
                ).scalars()
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

        box_from = await endpoint_box(from_task_id)
        box_to = await endpoint_box(to_task_id)
        if from_frame > to_frame:
            from_frame, to_frame = to_frame, from_frame
            box_from, box_to = box_to, box_from

        frame_to_task = await get_scene_frame_task_map(self.db, from_scene)
        mid_frames = sorted(
            frame for frame in frame_to_task if from_frame < frame < to_frame
        )
        if not mid_frames:
            raise HTTPException(
                status_code=422, detail="插值区间内没有可写的中间帧 task"
            )

        mid_tasks: dict[int, Task] = {}
        for frame in mid_frames:
            task = await get_core_row(self.db, task_row, frame_to_task[frame])
            if task is None:
                continue
            if assert_task_visible is not None:
                await assert_task_visible(task)
            if assert_task_editable is not None:
                assert_task_editable(task)
            mid_tasks[frame] = task

        trajectory = {
            pose.frame_index: pose
            for pose in await AssetsSpatialGateway(self.db).read_poses(
                ReadPosesRequest(actor_id=None, scene_id=from_scene)
            )
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

        created_writes: list[CrossFrameTargetWrite] = []
        skipped_frames: list[int] = []
        motion_compensated = True
        for frame, mid_task in mid_tasks.items():
            existing = await self.db.scalar(
                select(func.count())
                .select_from(annotation_row)
                .where(annotation_row.task_id == mid_task.id)
                .where(annotation_row.track_id == track_id)
                .where(annotation_row.is_active.is_(True))
            )
            if existing:
                skipped_frames.append(frame)
                continue
            if lock_task_for_write is not None:
                mid_task = await lock_task_for_write(mid_task)
            await record_annotation_actor(self.db, mid_task, user_id, nowait=True)

            t = (frame - from_frame) / (to_frame - from_frame)
            psr, compensated = interpolate_psr(
                psr_a,
                psr_b,
                t,
                pose_a=trajectory.get(from_frame),
                pose_b=trajectory.get(to_frame),
                pose_mid=trajectory.get(frame),
            )
            motion_compensated = motion_compensated and compensated
            geometry = copy.deepcopy(box_from.geometry or {})
            geometry.update(psr)
            geometry["convention_at_create"] = convention
            annotation = Annotation(
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
            created_writes.append(
                self._target_write(annotation, source_annotation_id=box_from.id)
            )

        sources = [
            self._source_fence(box_from, target_track_id=track_id),
            self._source_fence(box_to, target_track_id=track_id),
        ]
        ensure = self._scene_track_ensure(
            project_id=from_task.project_id,
            scene_id=from_scene,
            track_id=track_id,
            class_name=box_from.class_name,
            frames=set(range(from_frame, to_frame + 1)),
        )
        created = await self._execute_write(
            operation="interpolate_range",
            actor_id=user_id,
            tasks=[
                from_task,
                to_task,
                *[
                    task
                    for task in mid_tasks.values()
                    if any(write.task_id == task.id for write in created_writes)
                ],
            ],
            sources=sources,
            targets=created_writes,
            scene_tracks=[ensure],
        )
        await self.db.flush()
        for annotation in created:
            await update_annotation_statistics(self.db, annotation.task_id)
        return created, motion_compensated and bool(created), skipped_frames


__all__ = ["LabelingCrossFrameWriteService"]
