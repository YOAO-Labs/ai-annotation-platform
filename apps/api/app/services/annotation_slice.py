"""Restricted, versioned two-result slice transactions and ledger-owned restore."""

from __future__ import annotations

import hashlib
import json
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.annotation import Annotation
from app.db.models.annotation_operation import (
    AnnotationLineageEdge,
    AnnotationOperation,
)
from app.db.models.task import Task
from app.db.models.user import User
from app.schemas.annotation_slice import (
    AnnotationSliceResponse,
    AnnotationSliceRestoreRequest,
    PolygonSliceCommitRequest,
)
from app.services.annotation import AnnotationService
from app.services.annotation_evidence import record_annotation_actor
from app.services.audit import AuditAction, AuditService
from app.services.polygon_slice import PolygonSliceGeometryError, slice_polygon
from app.services.task_lock import TaskLockConflictError
from app.services.video_collaboration import (
    assert_task_lock_for_legacy_video,
    heartbeat_task_lock_for_legacy_video,
)

SLICE_RESTORE_TTL = timedelta(days=30)


class AnnotationSliceError(ValueError):
    def __init__(self, status_code: int, reason: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.detail = {"reason": reason, "message": message}


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def _snapshot(annotation: Annotation) -> dict:
    return {"geometry": deepcopy(annotation.geometry), "active": annotation.is_active}


class AnnotationSliceService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def _assert_task_writable(
        self, task: Task, actor: User, *, access: Any = None
    ) -> None:
        from app.api.v1.tasks._shared import _assert_task_editable

        _assert_task_editable(task, actor, access=access)
        if task.file_type != "image":
            raise AnnotationSliceError(422, "unsupported_media", "切割仅支持图片任务")
        try:
            await assert_task_lock_for_legacy_video(self.db, task, actor.id)
        except TaskLockConflictError as exc:
            raise AnnotationSliceError(
                409, "task_lock_conflict", "任务正由其他用户编辑"
            ) from exc

    async def _lock_task(
        self, task_id: uuid.UUID, actor: User, *, access: Any = None
    ) -> tuple[Task, Any]:
        from app.api.v1.tasks._shared import (
            _assert_review_adjustment_evidence,
            _assert_task_visible,
            _resolve_task_access,
            assert_annotation_write_allowed,
        )

        task = await self.db.scalar(
            select(Task)
            .where(Task.id == task_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if task is None:
            raise AnnotationSliceError(404, "task_not_found", "任务不存在")
        if access is None:
            access = await _resolve_task_access(
                self.db, task, actor, lock_membership=True
            )
        await _assert_task_visible(self.db, task, actor, access=access)
        assert_annotation_write_allowed(task, access)
        await _assert_review_adjustment_evidence(self.db, task, actor, access)
        return task, access

    async def _replay(
        self, task_id: uuid.UUID, actor: User, key: str, digest: str
    ) -> AnnotationSliceResponse | None:
        operation = await self.db.scalar(
            select(AnnotationOperation).where(
                AnnotationOperation.task_id == task_id,
                AnnotationOperation.actor_id == actor.id,
                AnnotationOperation.idempotency_key == key,
            )
        )
        if operation is None:
            return None
        if operation.request_digest != digest or operation.kind not in {
            "slice_polygon",
            "restore_slice",
        }:
            raise AnnotationSliceError(
                409, "idempotency_conflict", "此幂等键已用于另一请求"
            )
        return AnnotationSliceResponse.model_validate(
            {**operation.response_json, "idempotent_replay": True}
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
            raise AnnotationSliceError(
                409, "annotation_missing", "切割对象已不存在，请刷新任务"
            )
        if any(
            row.is_locked or row.was_cancelled or row.sensor_role is not None
            for row in rows
        ):
            raise AnnotationSliceError(
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
            raise AnnotationSliceError(
                409, "active_children", "有活动子对象的标注不能切割或恢复"
            )
        return {str(row.id): row for row in rows}

    async def _record(
        self,
        *,
        task: Task,
        actor: User,
        operation: AnnotationOperation,
        request: Any,
        detail: dict,
    ) -> None:
        self.db.add(operation)
        await self.db.flush([operation])
        # Both slice commit and ledger restore funnel through here.
        await record_annotation_actor(self.db, task, actor.id)
        await AuditService.log(
            self.db,
            actor=actor,
            action=AuditAction.ANNOTATION_SLICE,
            target_type="annotation_operation",
            target_id=operation.id,
            request=request,
            status_code=200,
            detail={"task_id": str(task.id), **detail},
        )
        await AnnotationService(self.db)._update_task_stats(task.id)
        await heartbeat_task_lock_for_legacy_video(self.db, task, actor.id)

    async def commit(
        self,
        task_id: uuid.UUID,
        payload: PolygonSliceCommitRequest,
        actor: User,
        *,
        request: Any = None,
        access: Any = None,
    ) -> AnnotationSliceResponse:
        task, access = await self._lock_task(task_id, actor, access=access)
        digest = _digest({"kind": "slice_polygon", **payload.model_dump(mode="json")})
        replay = await self._replay(task_id, actor, payload.idempotency_key, digest)
        if replay:
            return replay
        await self._assert_task_writable(task, actor, access=access)
        rows = await self._lock_annotations(task_id, [payload.annotation_id])
        source = rows[str(payload.annotation_id)]
        if not source.is_active or source.version != payload.expected_version:
            raise AnnotationSliceError(
                409, "version_mismatch", "来源对象已改变，请重新生成切割预览"
            )
        if source.annotation_type != "polygon":
            raise AnnotationSliceError(
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
            raise AnnotationSliceError(422, "invalid_cut", str(exc)) from exc
        before = {str(source.id): _snapshot(source)}
        source.geometry = kept_geometry
        source.version += 1
        created = Annotation(
            id=uuid.uuid4(),
            task_id=task.id,
            project_id=task.project_id,
            user_id=actor.id,
            source="manual",
            annotation_type="polygon",
            tool_unit_id=source.tool_unit_id,
            class_name=source.class_name,
            geometry=created_geometry,
            confidence=None,
            attributes=deepcopy(source.attributes),
            attributes_meta=deepcopy(source.attributes_meta),
            z_order=source.z_order,
            parent_annotation_id=source.parent_annotation_id,
            version=1,
            is_active=True,
        )
        self.db.add(created)
        await self.db.flush()
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
            restore_expires_at=now + SLICE_RESTORE_TTL,
        )
        operation = AnnotationOperation(
            id=operation_id,
            task_id=task.id,
            actor_id=actor.id,
            kind="slice_polygon",
            idempotency_key=payload.idempotency_key,
            request_digest=digest,
            scope_fingerprint=_digest(before),
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
            request=request,
            detail={"kind": "slice_polygon", "result_versions": versions},
        )
        self.db.add_all(
            [
                AnnotationLineageEdge(
                    operation_id=operation_id,
                    source_annotation_id=source.id,
                    result_annotation_id=row.id,
                    source_version=payload.expected_version,
                    result_version=row.version,
                    relation="split",
                )
                for row in (source, created)
            ]
        )
        await self.db.flush()
        return response

    async def restore(
        self,
        task_id: uuid.UUID,
        operation_id: uuid.UUID,
        payload: AnnotationSliceRestoreRequest,
        actor: User,
        *,
        request: Any = None,
        access: Any = None,
    ) -> AnnotationSliceResponse:
        task, access = await self._lock_task(task_id, actor, access=access)
        digest = _digest(
            {
                "kind": "restore_slice",
                "slice_operation_id": str(operation_id),
                **payload.model_dump(mode="json"),
            }
        )
        replay = await self._replay(task_id, actor, payload.idempotency_key, digest)
        if replay:
            return replay
        await self._assert_task_writable(task, actor, access=access)
        original = await self.db.scalar(
            select(AnnotationOperation)
            .where(
                AnnotationOperation.id == operation_id,
                AnnotationOperation.task_id == task_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if original is None or original.kind not in {"slice_polygon", "slice_mask"}:
            raise AnnotationSliceError(404, "slice_not_found", "此操作不是可恢复的切割")
        expires_at = original.created_at + SLICE_RESTORE_TTL
        if datetime.now(timezone.utc) >= expires_at:
            raise AnnotationSliceError(
                410, "restore_expired", "切割已超过 30 天恢复期限"
            )
        report = original.report
        is_mask = original.kind == "slice_mask"
        if report.get("slice_snapshot_schema") != (2 if is_mask else 1):
            raise AnnotationSliceError(
                409, "snapshot_unavailable", "切割恢复快照不可用"
            )
        expected = {str(key): value for key, value in payload.expected_versions.items()}
        recorded = report.get("current_versions")
        if expected != recorded:
            raise AnnotationSliceError(
                409,
                "version_mismatch",
                "恢复版本集不匹配；请保留原历史，不能覆盖后续修改",
            )
        if is_mask:
            from app.services.mask_slice_restore import load_mask_slice_snapshots

            snapshots = await load_mask_slice_snapshots(self.db, task, report)
        else:
            snapshots = report
        rows = await self._lock_annotations(
            task_id, [uuid.UUID(key) for key in expected]
        )
        target = snapshots.get(payload.target, {})
        current = snapshots.get(report.get("current_side"), {})
        if set(target) != set(rows) or set(current) != set(rows):
            raise AnnotationSliceError(
                409, "snapshot_unavailable", "切割恢复快照不完整"
            )
        for key, row in rows.items():
            if row.version != expected[key] or _snapshot(row) != current[key]:
                raise AnnotationSliceError(
                    409, "version_mismatch", "切割结果已被修改，不能撤销或重做"
                )
            snapshot = target[key]
            if (
                set(snapshot) != {"geometry", "active"}
                or not isinstance(snapshot["active"], bool)
                or snapshot["geometry"].get("type")
                != ("raster_mask" if is_mask else "polygon")
            ):
                raise AnnotationSliceError(
                    409, "snapshot_unavailable", "切割恢复快照类型不受支持"
                )
        no_op = report["current_side"] == payload.target
        if not no_op:
            for key, row in rows.items():
                row.geometry = deepcopy(target[key]["geometry"])
                row.is_active = target[key]["active"]
                row.version += 1
        versions = {key: row.version for key, row in rows.items()}
        restore_id = uuid.uuid4()
        original_receipt = (
            original.response_json["slice_restore"]
            if is_mask
            else original.response_json
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
            scope_fingerprint=_digest(current),
            source_versions=expected,
            result_versions=versions,
            report={
                "slice_operation_id": str(operation_id),
                "target": payload.target,
                "no_op": no_op,
            },
            response_json=response.model_dump(mode="json"),
        )
        await self._record(
            task=task,
            actor=actor,
            operation=restore_operation,
            request=request,
            detail=restore_operation.report,
        )
        if not no_op:
            original.report = {
                **report,
                "current_side": payload.target,
                "current_versions": versions,
            }
            self.db.add_all(
                [
                    AnnotationLineageEdge(
                        operation_id=restore_id,
                        source_annotation_id=row.id,
                        result_annotation_id=row.id,
                        source_version=expected[key],
                        result_version=row.version,
                        relation="slice_restored",
                    )
                    for key, row in rows.items()
                ]
            )
        await self.db.flush()
        if is_mask:
            from app.services.mask_slice_restore import protect_mask_slice_versions

            await protect_mask_slice_versions(self.db, report, expires_at)
        return response
