"""Restricted, versioned two-result slice transactions and ledger-owned restore."""

from __future__ import annotations

import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.annotation import Annotation
from app.db.models.annotation_operation import AnnotationOperation
from app.db.models.task import Task
from app.db.models.user import User
from app.schemas.annotation_slice import (
    AnnotationSliceResponse,
    AnnotationSliceRestoreRequest,
    PolygonSliceCommitRequest,
)
from app.services import task_access
from app.services.annotation import AnnotationService
from app.services.annotation_evidence import record_annotation_actor
from app.services.annotation_slice_contracts import (
    AnnotationSliceError as _AnnotationSliceError,
    SLICE_RESTORE_TTL as _SLICE_RESTORE_TTL,
    annotation_slice_digest as _annotation_slice_digest,
)
from app.services.audit_origin_contract import AuditOriginContext
from app.services.audit import AuditAction, AuditService
from app.services.labeling_write_scope import (
    capture_task_source_binding,
    task_source_binding_matches,
)
from app.services.polygon_slice import PolygonSliceGeometryError, slice_polygon
from app.services.task_lock import TaskLockConflictError
from aap_labeling.mask_edit_contract import (
    ApplyPolygonSlice,
    ApplySliceRestore,
    AnnotationLineage,
    FindOperation,
    ReadOperation,
    WriteAnnotationOperation,
    MASK_EDIT_PROTOCOL,
)
from app.services.labeling_mask_edit import (
    MaskOwnerCommandError,
    mask_edit_command,
    mask_task_fence,
)
from app.services.video_collaboration import (
    assert_task_lock_for_legacy_video,
    heartbeat_task_lock_for_legacy_video,
)


def _snapshot(annotation: Annotation) -> dict:
    return {"geometry": deepcopy(annotation.geometry), "active": annotation.is_active}


class AnnotationSliceService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def _assert_task_writable(
        self, task: Task, actor: User, *, access: Any = None
    ) -> None:

        task_access.assert_task_editable(task, actor.id, access=access)
        if task.file_type != "image":
            raise _AnnotationSliceError(422, "unsupported_media", "切割仅支持图片任务")
        try:
            await assert_task_lock_for_legacy_video(self.db, task, actor.id)
        except TaskLockConflictError as exc:
            raise _AnnotationSliceError(
                409, "task_lock_conflict", "任务正由其他用户编辑"
            ) from exc

    async def _lock_task(
        self, task_id: uuid.UUID, actor: User, *, access: Any = None
    ) -> tuple[Task, Any]:

        task = await self.db.scalar(
            select(Task)
            .where(Task.id == task_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if task is None:
            raise _AnnotationSliceError(404, "task_not_found", "任务不存在")
        if access is None:
            access = await task_access.resolve_task_access(
                self.db, task, actor.id, lock_membership=True
            )
        await task_access.assert_task_visible(self.db, task, actor.id, access=access)
        task_access.assert_annotation_write_allowed(task, access)
        await task_access.assert_review_adjustment_evidence(
            self.db, task, actor.id, access
        )
        return task, access

    async def _owner(self, command):
        try:
            return await mask_edit_command(self.db, command)
        except MaskOwnerCommandError as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            raise _AnnotationSliceError(
                exc.status_code,
                str(detail.get("reason", "mask_write_conflict")),
                str(detail.get("message", exc)),
            ) from exc

    async def _replay(
        self, task_id: uuid.UUID, actor: User, key: str, digest: str
    ) -> AnnotationSliceResponse | None:
        operation = await self._owner(
            FindOperation(
                schema=MASK_EDIT_PROTOCOL,
                operation="find_operation",
                task_id=task_id,
                actor_id=actor.id,
                idempotency_key=key,
            )
        )
        if not operation.found:
            return None
        if operation.request_digest != digest or operation.kind not in {
            "slice_polygon",
            "restore_slice",
        }:
            raise _AnnotationSliceError(
                409, "idempotency_conflict", "此幂等键已用于另一请求"
            )
        return AnnotationSliceResponse.model_validate(
            {**(operation.response_json or {}), "idempotent_replay": True}
        )

    async def _read_operation(
        self, task_id: uuid.UUID, operation_id: uuid.UUID, *, lock: bool
    ):
        return await self._owner(
            ReadOperation(
                schema=MASK_EDIT_PROTOCOL,
                operation="read_operation",
                task_id=task_id,
                operation_id=operation_id,
                lock=lock,
            )
        )

    async def _lock_annotations(
        self, task_id: uuid.UUID, ids: list[uuid.UUID]
    ) -> dict[str, Annotation]:
        rows = list(
            (
                await self.db.scalars(
                    select(Annotation)
                    .where(
                        Annotation.task_id == task_id,
                        Annotation.id.in_(ids),
                    )
                    .order_by(Annotation.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        if len(rows) != len(ids):
            raise _AnnotationSliceError(
                409, "annotation_missing", "切割对象已不存在，请刷新任务"
            )
        if any(
            row.is_locked or row.was_cancelled or row.sensor_role is not None
            for row in rows
        ):
            raise _AnnotationSliceError(
                409, "annotation_locked", "切割对象已锁定或不可编辑"
            )
        child = await self.db.scalar(
            select(Annotation.id)
            .where(
                Annotation.parent_annotation_id.in_(ids),
                Annotation.is_active.is_(True),
            )
            .limit(1)
        )
        if child:
            raise _AnnotationSliceError(
                409, "active_children", "有活动子对象的标注不能切割或恢复"
            )
        return {str(row.id): row for row in rows}

    async def _record(
        self,
        *,
        task: Task,
        actor: User,
        operation: AnnotationOperation,
        origin: AuditOriginContext | None,
        detail: dict,
        lineage: list[AnnotationLineage] | None = None,
        original_operation_id: uuid.UUID | None = None,
        original_report: dict[str, Any] | None = None,
    ) -> None:
        await self._owner(
            WriteAnnotationOperation(
                schema=MASK_EDIT_PROTOCOL,
                operation="write_operation",
                **mask_task_fence(task, actor.id),
                operation_id=operation.id,
                kind=operation.kind,
                idempotency_key=operation.idempotency_key,
                request_digest=operation.request_digest,
                scope_fingerprint=operation.scope_fingerprint,
                source_versions=operation.source_versions,
                result_versions=operation.result_versions,
                report=operation.report,
                response_json=operation.response_json,
                created_at=operation.created_at,
                completed_at=operation.completed_at,
                lineage=lineage or [],
                original_operation_id=original_operation_id,
                original_report=original_report,
            )
        )
        await record_annotation_actor(self.db, task, actor.id)
        await AuditService.log(
            self.db,
            actor=actor,
            action=AuditAction.ANNOTATION_SLICE,
            target_type="annotation_operation",
            target_id=operation.id,
            origin=origin,
            status_code=200,
            detail={"task_id": str(task.id), **detail},
        )
        await AnnotationService(self.db)._update_task_stats(task.id)
        await heartbeat_task_lock_for_legacy_video(self.db, task, actor.id)

    async def _commit(
        self,
        task_id: uuid.UUID,
        payload: PolygonSliceCommitRequest,
        actor: User,
        *,
        origin: AuditOriginContext | None = None,
        access: Any = None,
    ) -> AnnotationSliceResponse:
        task, access = await self._lock_task(task_id, actor, access=access)
        digest = _annotation_slice_digest(
            {"kind": "slice_polygon", **payload.model_dump(mode="json")}
        )
        replay = await self._replay(task_id, actor, payload.idempotency_key, digest)
        if replay:
            return replay
        await self._assert_task_writable(task, actor, access=access)
        rows = await self._lock_annotations(task_id, [payload.annotation_id])
        source = rows[str(payload.annotation_id)]
        if not source.is_active or source.version != payload.expected_version:
            raise _AnnotationSliceError(
                409, "version_mismatch", "来源对象已改变，请重新生成切割预览"
            )
        if source.annotation_type != "polygon":
            raise _AnnotationSliceError(
                422, "unsupported_geometry", "切割仅支持简单单外环多边形"
            )
        if source.parent_annotation_id is not None:
            await AnnotationService(self.db)._validate_parent_annotation(
                task_id, source.parent_annotation_id
            )
        try:
            kept_geometry, created_geometry = slice_polygon(
                source.geometry, payload.cut_path
            )
        except PolygonSliceGeometryError as exc:
            raise _AnnotationSliceError(422, "invalid_cut", str(exc)) from exc
        before = {str(source.id): _snapshot(source)}
        created_id = uuid.uuid4()
        persisted = await self._owner(
            ApplyPolygonSlice(
                schema=MASK_EDIT_PROTOCOL,
                operation="apply_polygon_slice",
                **mask_task_fence(task, actor.id),
                source_annotation_id=source.id,
                created_annotation_id=created_id,
                expected_version=payload.expected_version,
                source_geometry=kept_geometry,
                created_geometry=created_geometry,
            )
        )
        set_committed_value(source, "geometry", persisted.source.geometry)
        set_committed_value(source, "version", persisted.source.version)
        set_committed_value(source, "updated_at", persisted.source.updated_at)
        created = Annotation(
            id=persisted.created.annotation_id,
            task_id=task.id,
            project_id=task.project_id,
            user_id=persisted.created.user_id,
            source="manual",
            annotation_type=persisted.created.annotation_type,
            tool_unit_id=persisted.created.tool_unit_id,
            class_name=persisted.created.class_name,
            geometry=persisted.created.geometry,
            confidence=persisted.created.confidence,
            attributes=persisted.created.attributes,
            attributes_meta=persisted.created.attributes_meta,
            z_order=persisted.created.z_order,
            parent_annotation_id=persisted.created.parent_annotation_id,
            version=persisted.created.version,
            is_active=persisted.created.is_active,
            updated_at=persisted.created.updated_at,
        )
        await self.db.flush()
        from app.services.accepted_inputs import copy_annotation_input_sources

        await copy_annotation_input_sources(
            self.db,
            source_annotation_id=source.id,
            target_annotation_id=created.id,
        )
        if task.file_type == "video":
            from app.services.accepted_video_inputs import (
                copy_annotation_video_input_sources,
            )
            from app.services.accepted_tracker_inputs import (
                copy_annotation_video_tracker_input_sources,
            )

            await copy_annotation_video_input_sources(
                self.db,
                source_annotation_id=source.id,
                target_annotation_id=created.id,
            )
            await copy_annotation_video_tracker_input_sources(
                self.db,
                actor_id=actor.id,
                source_annotation_id=source.id,
                target_annotation_id=created.id,
            )
        before[str(created.id)] = {
            "geometry": deepcopy(created_geometry),
            "active": False,
        }
        after = {str(row.id): _snapshot(row) for row in (source, created)}
        versions = {str(row.id): row.version for row in (source, created)}
        now = datetime.now(timezone.utc)
        operation_id = uuid.uuid4()
        response = AnnotationSliceResponse(
            operation_id=operation_id,
            slice_operation_id=operation_id,
            source_annotation_id=source.id,
            created_annotation_id=created.id,
            result_versions=versions,
            active_annotation_ids=[source.id, created.id],
            target="after",
            restore_expires_at=now + _SLICE_RESTORE_TTL,
        )
        operation = AnnotationOperation(
            id=operation_id,
            task_id=task.id,
            actor_id=actor.id,
            kind="slice_polygon",
            idempotency_key=payload.idempotency_key,
            request_digest=digest,
            scope_fingerprint=_annotation_slice_digest(before),
            source_versions={str(source.id): payload.expected_version},
            result_versions=versions,
            report={
                "slice_snapshot_schema": 1,
                "before": before,
                "after": after,
                "current_side": "after",
                "current_versions": versions,
            },
            response_json=response.model_dump(mode="json"),
            created_at=now,
            completed_at=now,
        )
        await self._record(
            task=task,
            actor=actor,
            operation=operation,
            origin=origin,
            detail={"kind": "slice_polygon", "result_versions": versions},
            lineage=[
                AnnotationLineage(
                    source_annotation_id=source.id,
                    result_annotation_id=row.id,
                    source_version=payload.expected_version,
                    result_version=row.version,
                    relation="split",
                    frame_index=None,
                )
                for row in (source, created)
            ],
        )
        await self.db.flush()
        return response

    async def _restore(
        self,
        task_id: uuid.UUID,
        operation_id: uuid.UUID,
        payload: AnnotationSliceRestoreRequest,
        actor: User,
        *,
        origin: AuditOriginContext | None = None,
        access: Any = None,
    ) -> AnnotationSliceResponse:
        digest = _annotation_slice_digest(
            {
                "kind": "restore_slice",
                "slice_operation_id": str(operation_id),
                **payload.model_dump(mode="json"),
            }
        )
        preflight_task = await self.db.get(Task, task_id, populate_existing=True)
        if preflight_task is None:
            raise _AnnotationSliceError(404, "task_not_found", "任务不存在")
        source_binding = capture_task_source_binding(preflight_task)
        preflight_access = await task_access.resolve_task_access(
            self.db, preflight_task, actor.id
        )
        await task_access.assert_task_visible(
            self.db, preflight_task, actor.id, access=preflight_access
        )
        task_access.assert_annotation_write_allowed(preflight_task, preflight_access)
        await task_access.assert_review_adjustment_evidence(
            self.db, preflight_task, actor.id, preflight_access
        )
        preflight_replay = await self._replay(
            task_id, actor, payload.idempotency_key, digest
        )
        verified_mask_content = None
        if preflight_replay is None:
            await self._assert_task_writable(
                preflight_task, actor, access=preflight_access
            )
            preflight_original = await self._read_operation(
                task_id, operation_id, lock=False
            )
            if (
                preflight_original.found
                and preflight_original.kind == "slice_mask"
                and datetime.now(timezone.utc)
                < preflight_original.created_at + _SLICE_RESTORE_TTL
                and (preflight_original.report or {}).get("slice_snapshot_schema") == 2
                and {
                    str(key): value for key, value in payload.expected_versions.items()
                }
                == (preflight_original.report or {}).get("current_versions")
            ):
                from app.services.mask_slice_restore import (
                    preflight_mask_slice_snapshots,
                )

                verified_mask_content = await preflight_mask_slice_snapshots(
                    self.db, preflight_task, preflight_original.report or {}
                )

        final_access = await task_access.resolve_task_access(
            self.db, preflight_task, actor.id, lock_membership=True
        )
        task, access = await self._lock_task(task_id, actor, access=final_access)
        digest = _annotation_slice_digest(
            {
                "kind": "restore_slice",
                "slice_operation_id": str(operation_id),
                **payload.model_dump(mode="json"),
            }
        )
        replay = await self._replay(task_id, actor, payload.idempotency_key, digest)
        if replay:
            return replay
        if verified_mask_content is not None and not task_source_binding_matches(
            task, source_binding
        ):
            raise _AnnotationSliceError(
                409, "source_changed", "任务媒体来源在验证恢复资源期间发生变化"
            )
        await self._assert_task_writable(task, actor, access=access)
        original = await self._read_operation(task_id, operation_id, lock=True)
        if not original.found or original.kind not in {"slice_polygon", "slice_mask"}:
            raise _AnnotationSliceError(
                404, "slice_not_found", "此操作不是可恢复的切割"
            )
        expires_at = original.created_at + _SLICE_RESTORE_TTL
        if datetime.now(timezone.utc) >= expires_at:
            raise _AnnotationSliceError(
                410, "restore_expired", "切割已超过 30 天恢复期限"
            )
        report = original.report or {}
        is_mask = original.kind == "slice_mask"
        if report.get("slice_snapshot_schema") != (2 if is_mask else 1):
            raise _AnnotationSliceError(
                409, "snapshot_unavailable", "切割恢复快照不可用"
            )
        expected = {str(key): value for key, value in payload.expected_versions.items()}
        recorded = report.get("current_versions")
        if expected != recorded:
            raise _AnnotationSliceError(
                409,
                "version_mismatch",
                "恢复版本集不匹配；请保留原历史，不能覆盖后续修改",
            )
        if is_mask:
            from app.services.mask_slice_restore import load_mask_slice_snapshots

            snapshots = await load_mask_slice_snapshots(
                self.db,
                task,
                report,
                verified_content=verified_mask_content,
            )
        else:
            snapshots = report
        rows = await self._lock_annotations(
            task_id, [uuid.UUID(key) for key in expected]
        )
        target = snapshots.get(payload.target, {})
        current = snapshots.get(report.get("current_side"), {})
        if set(target) != set(rows) or set(current) != set(rows):
            raise _AnnotationSliceError(
                409, "snapshot_unavailable", "切割恢复快照不完整"
            )
        restore_changes = []
        for key, row in rows.items():
            if row.version != expected[key] or _snapshot(row) != current[key]:
                raise _AnnotationSliceError(
                    409, "version_mismatch", "切割结果已被修改，不能撤销或重做"
                )
            snapshot = target[key]
            if (
                set(snapshot) != {"geometry", "active"}
                or not isinstance(snapshot["active"], bool)
                or snapshot["geometry"].get("type")
                != ("raster_mask" if is_mask else "polygon")
            ):
                raise _AnnotationSliceError(
                    409, "snapshot_unavailable", "切割恢复快照类型不受支持"
                )
            restore_changes.append(
                {
                    "annotation_id": row.id,
                    "expected_version": expected[key],
                    "expected_geometry": current[key]["geometry"],
                    "expected_active": current[key]["active"],
                    "geometry": snapshot["geometry"],
                    "active": snapshot["active"],
                }
            )
        no_op = report["current_side"] == payload.target
        restore_id = uuid.uuid4()
        persisted = await self._owner(
            ApplySliceRestore(
                schema=MASK_EDIT_PROTOCOL,
                operation="apply_slice_restore",
                **mask_task_fence(task, actor.id),
                original_operation_id=operation_id,
                target=payload.target,
                no_op=no_op,
                annotations=restore_changes,
            )
        )
        versions = persisted.result_versions
        for key, row in rows.items():
            if not no_op:
                set_committed_value(row, "geometry", target[key]["geometry"])
                set_committed_value(row, "is_active", target[key]["active"])
                set_committed_value(row, "version", versions[key])
        original_receipt = (
            (original.response_json or {})["slice_restore"]
            if is_mask
            else (original.response_json or {})
        )
        response = AnnotationSliceResponse(
            operation_id=restore_id,
            slice_operation_id=operation_id,
            source_annotation_id=original_receipt["source_annotation_id"],
            created_annotation_id=original_receipt["created_annotation_id"],
            result_versions=versions,
            active_annotation_ids=[row.id for row in rows.values() if row.is_active],
            target=payload.target,
            restore_expires_at=expires_at,
            no_op=no_op,
        )
        restore_operation = AnnotationOperation(
            id=restore_id,
            task_id=task_id,
            actor_id=actor.id,
            kind="restore_slice",
            idempotency_key=payload.idempotency_key,
            request_digest=digest,
            scope_fingerprint=_annotation_slice_digest(current),
            source_versions=expected,
            result_versions=versions,
            report={
                "slice_operation_id": str(operation_id),
                "target": payload.target,
                "no_op": no_op,
            },
            response_json=response.model_dump(mode="json"),
        )
        original_report = None
        lineage = []
        if not no_op:
            original_report = {
                **persisted.original_report,
                "current_side": payload.target,
                "current_versions": versions,
            }
            lineage = [
                AnnotationLineage(
                    source_annotation_id=row.id,
                    result_annotation_id=row.id,
                    source_version=expected[key],
                    result_version=versions[key],
                    relation="slice_restored",
                    frame_index=None,
                )
                for key, row in rows.items()
            ]
        await self._record(
            task=task,
            actor=actor,
            operation=restore_operation,
            origin=origin,
            detail=restore_operation.report,
            lineage=lineage,
            original_operation_id=operation_id if original_report is not None else None,
            original_report=original_report,
        )
        await self.db.flush()
        if is_mask:
            from app.services.mask_slice_restore import protect_mask_slice_versions

            await protect_mask_slice_versions(self.db, task.id, report, expires_at)
        return response
