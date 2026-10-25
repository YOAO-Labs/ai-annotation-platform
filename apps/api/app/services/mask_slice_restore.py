"""Version references and retention for the shared, restricted slice restore."""

from __future__ import annotations

import uuid
from copy import deepcopy
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.annotation import Annotation
from app.db.models.task import Task
from app.services.annotation_slice_contracts import (
    AnnotationSliceError as _AnnotationSliceError,
    annotation_slice_digest as _annotation_slice_digest,
)
from aap_labeling.mask_revision_contract import (
    ExpireMaskRevisions,
    MaskRevisionReference,
    ProtectMaskRevisions,
    ResolveMaskRevisions,
    MASK_REVISION_PROTOCOL,
)
from app.services.labeling_mask_edit import MaskOwnerCommandError, mask_revision_command
from app.services.raster_mask_storage import (
    _VerifiedMaskContent,
    RasterMaskContractError,
    prepare_mask_payload_for_write,
    preflight_mask_payload_for_write,
)


async def expire_mask_annotation_revisions(
    db: AsyncSession, *, now: datetime | None = None
) -> int:
    """Delete snapshots only after their stored retention deadline has elapsed."""
    try:
        result = await mask_revision_command(
            db,
            ExpireMaskRevisions(
                schema=MASK_REVISION_PROTOCOL, operation="expire", now=now
            ),
        )
    except MaskOwnerCommandError as exc:
        raise _AnnotationSliceError(
            exc.status_code, "revision_expiry_failed", str(exc)
        ) from exc
    return result.affected


def mask_version_snapshot(
    annotation: Annotation, *, active: bool | None = None
) -> dict:
    return {
        "annotation_version": annotation.version,
        "geometry_digest": _annotation_slice_digest(annotation.geometry),
        "active": annotation.is_active if active is None else active,
    }


def _references(report: dict) -> dict[tuple[uuid.UUID, int], dict]:
    references = {}
    for side in ("before", "after"):
        for key, snapshot in report.get(side, {}).items():
            if (
                set(snapshot) != {"annotation_version", "geometry_digest", "active"}
                or type(snapshot["annotation_version"]) is not int
                or snapshot["annotation_version"] < 1
                or not isinstance(snapshot["active"], bool)
                or not isinstance(snapshot["geometry_digest"], str)
            ):
                raise _AnnotationSliceError(
                    409, "snapshot_unavailable", "Mask 恢复引用无效"
                )
            identity = (uuid.UUID(key), snapshot["annotation_version"])
            if (
                identity in references
                and references[identity]["geometry_digest"]
                != snapshot["geometry_digest"]
            ):
                raise _AnnotationSliceError(
                    409, "snapshot_unavailable", "Mask 恢复引用不一致"
                )
            references[identity] = snapshot
    return references


async def protect_mask_slice_versions(
    db: AsyncSession, task_id: uuid.UUID, report: dict, expires_at: datetime
) -> None:
    """Call after annotation flush: triggers have captured every retiring version.

    SQL GREATEST keeps infinity and longer retention unchanged. Run after every
    restore so capture-trigger pruning cannot shorten a referenced old revision.
    """
    references = _references(report)
    try:
        await mask_revision_command(
            db,
            ProtectMaskRevisions(
                schema=MASK_REVISION_PROTOCOL,
                operation="protect",
                task_id=task_id,
                references=[
                    MaskRevisionReference(
                        annotation_id=annotation_id,
                        annotation_version=version,
                        geometry_digest=snapshot["geometry_digest"],
                    )
                    for (annotation_id, version), snapshot in references.items()
                ],
                expires_at=expires_at,
            ),
        )
    except MaskOwnerCommandError as exc:
        raise _AnnotationSliceError(
            exc.status_code, "snapshot_unavailable", str(exc)
        ) from exc


async def _resolve_mask_slice_geometries(
    db: AsyncSession, task: Task, report: dict, *, locked: bool
) -> dict[tuple[uuid.UUID, int], dict]:
    references = _references(report)
    request = ResolveMaskRevisions(
        schema=MASK_REVISION_PROTOCOL,
        operation="resolve",
        task_id=task.id,
        references=[
            MaskRevisionReference(
                annotation_id=annotation_id,
                annotation_version=version,
                geometry_digest=snapshot["geometry_digest"],
            )
            for (annotation_id, version), snapshot in sorted(
                references.items(), key=lambda item: (str(item[0][0]), item[0][1])
            )
        ],
        lock=locked,
    )
    try:
        resolved = await mask_revision_command(db, request)
    except MaskOwnerCommandError as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        raise _AnnotationSliceError(
            exc.status_code,
            str(detail.get("reason", "snapshot_unavailable")),
            str(detail.get("message", "Mask 恢复版本缺失或已过期")),
        ) from exc
    result = {}
    for row in resolved.geometries:
        identity = (row.annotation_id, row.annotation_version)
        snapshot = references.get(identity)
        if (
            snapshot is None
            or row.geometry.get("type") != "raster_mask"
            or _annotation_slice_digest(row.geometry) != snapshot["geometry_digest"]
        ):
            raise _AnnotationSliceError(
                409, "snapshot_unavailable", "Mask 恢复版本内容校验失败"
            )
        result[identity] = deepcopy(row.geometry)
    return result


async def preflight_mask_slice_snapshots(
    db: AsyncSession, task: Task, report: dict
) -> _VerifiedMaskContent:
    """Verify snapshot objects before the restore acquires Task/Annotation locks."""
    geometries = await _resolve_mask_slice_geometries(db, task, report, locked=False)
    try:
        return await preflight_mask_payload_for_write(
            db, task, list(geometries.values())
        )
    except RasterMaskContractError as exc:
        raise _AnnotationSliceError(
            exc.status_code, "snapshot_unavailable", "Mask 恢复资源不可用：" + str(exc)
        ) from exc


async def load_mask_slice_snapshots(
    db: AsyncSession,
    task: Task,
    report: dict,
    *,
    verified_content: _VerifiedMaskContent | None = None,
) -> dict[str, dict[str, dict]]:
    """Finalize verified snapshots and serialize references with GC."""
    first = await _resolve_mask_slice_geometries(db, task, report, locked=False)
    try:
        await prepare_mask_payload_for_write(
            db,
            task,
            list(first.values()),
            verified_content=verified_content,
            require_verified_content=True,
        )
    except RasterMaskContractError as exc:
        raise _AnnotationSliceError(
            exc.status_code, "snapshot_unavailable", "Mask 恢复资源不可用：" + str(exc)
        ) from exc
    verified = await _resolve_mask_slice_geometries(db, task, report, locked=True)
    return {
        side: {
            key: {
                "geometry": verified[(uuid.UUID(key), snapshot["annotation_version"])],
                "active": snapshot["active"],
            }
            for key, snapshot in report[side].items()
        }
        for side in ("before", "after")
    }
