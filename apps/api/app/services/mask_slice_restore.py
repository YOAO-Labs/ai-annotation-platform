"""Version references and retention for the shared, restricted slice restore."""

from __future__ import annotations

import uuid
from copy import deepcopy
from datetime import datetime, timezone

from sqlalchemy import delete, func, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.annotation import Annotation
from app.db.models.mask_annotation_revision import MaskAnnotationRevision
from app.db.models.task import Task
from app.services.annotation_slice_contracts import (
    AnnotationSliceError as _AnnotationSliceError,
    annotation_slice_digest as _annotation_slice_digest,
)
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
    result = await db.execute(
        delete(MaskAnnotationRevision)
        .where(MaskAnnotationRevision.expires_at <= (now or datetime.now(timezone.utc)))
        .returning(MaskAnnotationRevision.id)
    )
    return len(result.scalars().all())


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
    db: AsyncSession, report: dict, expires_at: datetime
) -> None:
    """Call after annotation flush: triggers have captured every retiring version.

    SQL GREATEST keeps infinity and longer retention unchanged. Run after every
    restore so capture-trigger pruning cannot shorten a referenced old revision.
    """
    references = _references(report)
    await db.execute(
        update(MaskAnnotationRevision)
        .where(
            tuple_(
                MaskAnnotationRevision.annotation_id,
                MaskAnnotationRevision.annotation_version,
            ).in_(references)
        )
        .values(expires_at=func.greatest(MaskAnnotationRevision.expires_at, expires_at))
        .execution_options(synchronize_session=False)
    )


async def _resolve_mask_slice_geometries(
    db: AsyncSession, task: Task, report: dict, *, locked: bool
) -> dict[tuple[uuid.UUID, int], dict]:
    references = _references(report)
    annotations = {
        row.id: row
        for row in await db.scalars(
            select(Annotation)
            .where(
                Annotation.task_id == task.id,
                Annotation.id.in_({key[0] for key in references}),
            )
            .execution_options(populate_existing=True)
        )
    }

    result = {}
    for (annotation_id, version), snapshot in sorted(
        references.items(), key=lambda item: (str(item[0][0]), item[0][1])
    ):
        annotation = annotations.get(annotation_id)
        if annotation is not None and annotation.version == version:
            geometry = annotation.geometry
        else:
            query = (
                select(MaskAnnotationRevision)
                .where(
                    MaskAnnotationRevision.task_id == task.id,
                    MaskAnnotationRevision.annotation_id == annotation_id,
                    MaskAnnotationRevision.annotation_version == version,
                    MaskAnnotationRevision.expires_at > datetime.now(timezone.utc),
                )
                .execution_options(populate_existing=True)
            )
            revision = await db.scalar(query.with_for_update() if locked else query)
            if revision is None:
                raise _AnnotationSliceError(
                    409, "snapshot_unavailable", "Mask 恢复版本缺失或已过期"
                )
            geometry = revision.geometry
        if (
            geometry.get("type") != "raster_mask"
            or _annotation_slice_digest(geometry) != snapshot["geometry_digest"]
        ):
            raise _AnnotationSliceError(
                409, "snapshot_unavailable", "Mask 恢复版本内容校验失败"
            )
        result[(annotation_id, version)] = deepcopy(geometry)
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
