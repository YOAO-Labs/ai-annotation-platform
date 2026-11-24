from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from yoao_workspace.configuration_policy import lookup_classes_for_tool_unit

from app.services.annotations_import import import_native_json_annotations
from app.services.assets_media_summary import AssetsTaskMediaSummaryReader
from app.services.exporting.video_scope import (
    VideoExportScope,
    clip_video_geometry,
)
from app.services.labeling_core_read_relations import annotation_row, task_row
from app.services.mask_formats.contracts import (
    ExportMaskRecord,
    ExportTaskGeometrySummary,
    FormatImportCommitResult,
    FormatImportPreviewResult,
    FormatProjectContext,
    ResolvedTaskInfo,
)
from app.services.task_matcher import (
    resolve_task as match_task,
    resolve_task_by_file_stem,
    resolve_task_by_video_sequence,
)


def project_context_from_project(project: Any) -> FormatProjectContext:
    if isinstance(project, FormatProjectContext):
        return project
    tool_bindings = getattr(project, "tool_bindings", None) or {}
    return FormatProjectContext(
        project_id=project.id,
        data_type=getattr(project, "data_type", None) or "image",
        owner_id=getattr(project, "owner_id", None),
        allowed_region_classes=frozenset(
            lookup_classes_for_tool_unit(tool_bindings, "region")
        ),
    )


class SqlAlchemyFormatHost:
    def __init__(self, db: AsyncSession, project: FormatProjectContext) -> None:
        self._db = db
        self._project = project

    async def resolve_task(self, match: dict[str, Any]) -> ResolvedTaskInfo | None:
        task = await match_task(self._db, self._project.project_id, match)
        if task is None:
            return None
        return ResolvedTaskInfo(
            task_id=task.id,
            display_id=task.display_id,
            file_path=task.file_path,
        )

    async def resolve_image_task(
        self, media_path: str, *, stem_only: bool = False
    ) -> ResolvedTaskInfo | None:
        task = None
        if not stem_only:
            task = await match_task(
                self._db, self._project.project_id, {"file_path": media_path}
            )
        if task is None:
            task, _reason = await resolve_task_by_file_stem(
                self._db, self._project.project_id, media_path
            )
        if task is None:
            return None
        width: int | None = None
        height: int | None = None
        if task.dataset_item_id is not None:
            summary = await AssetsTaskMediaSummaryReader(
                self._db
            ).current_image_summary(task.dataset_item_id)
            if summary is not None and summary[0] and summary[1]:
                width, height = int(summary[0]), int(summary[1])
        return ResolvedTaskInfo(
            task_id=task.id,
            display_id=task.display_id,
            file_path=task.file_path,
            width=width,
            height=height,
        )

    async def resolve_video_task(self, sequence: str) -> ResolvedTaskInfo | None:
        task = await match_task(
            self._db, self._project.project_id, {"file_path": sequence}
        )
        if task is None:
            task, _reason = await resolve_task_by_file_stem(
                self._db, self._project.project_id, sequence
            )
        if task is None:
            task = await resolve_task_by_video_sequence(
                self._db, self._project.project_id, sequence
            )
        if task is None:
            return None
        width: int | None = None
        height: int | None = None
        frame_count: int | None = None
        if task.dataset_item_id is not None:
            summary = await AssetsTaskMediaSummaryReader(
                self._db
            ).current_video_summary(task.dataset_item_id)
            if summary is not None:
                raw_w, raw_h, video = summary
                if raw_w and raw_h:
                    width, height = int(raw_w), int(raw_h)
                    raw_fc = video.get("frame_count")
                    frame_count = int(raw_fc) if raw_fc else None
        return ResolvedTaskInfo(
            task_id=task.id,
            display_id=task.display_id,
            file_path=task.file_path,
            width=width,
            height=height,
            frame_count=frame_count,
        )

    async def list_export_task_geometries(
        self, *, scope: dict[str, Any]
    ) -> list[ExportTaskGeometrySummary]:
        video_scope = VideoExportScope.from_dict(scope.get("video_export_scope"))
        task_query = select(task_row.id, task_row.file_path).where(
            task_row.project_id == self._project.project_id
        )
        batch_id = scope.get("batch_id")
        if batch_id:
            task_query = task_query.where(task_row.batch_id == uuid.UUID(str(batch_id)))
        if video_scope is not None:
            task_query = task_query.where(task_row.id == video_scope.task_id)
        task_rows = list(
            (
                await self._db.execute(task_query.order_by(task_row.id).limit(100_001))
            ).all()
        )
        if len(task_rows) > 100_000:
            raise ValueError("format_preflight_task_budget_exceeded")

        task_ids = [row.id for row in task_rows]
        grouped: dict[uuid.UUID, dict[str, int]] = defaultdict(dict)
        if task_ids and video_scope is not None:
            annotation_rows = (
                (
                    await self._db.execute(
                        select(annotation_row).where(
                            annotation_row.task_id.in_(task_ids),
                            annotation_row.is_active.is_(True),
                            annotation_row.was_cancelled.is_(False),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for annotation in annotation_rows:
                geometry = clip_video_geometry(annotation.geometry, video_scope)
                if geometry is None:
                    continue
                geometry_type = str(geometry.get("type") or "")
                counts = grouped[annotation.task_id]
                counts[geometry_type] = counts.get(geometry_type, 0) + 1
        elif task_ids:
            geometry_type = annotation_row.geometry["type"].astext
            annotation_rows = (
                await self._db.execute(
                    select(
                        annotation_row.task_id,
                        geometry_type,
                        func.count(annotation_row.id),
                    )
                    .where(
                        annotation_row.task_id.in_(task_ids),
                        annotation_row.is_active.is_(True),
                        annotation_row.was_cancelled.is_(False),
                    )
                    .group_by(annotation_row.task_id, geometry_type)
                )
            ).all()
            for task_id, geometry_type, count in annotation_rows:
                grouped[task_id][str(geometry_type)] = int(count)

        return [
            ExportTaskGeometrySummary(
                task_id=task_id,
                file_path=file_path,
                geometry_counts=dict(grouped.get(task_id, {})),
            )
            for task_id, file_path in task_rows
        ]

    async def _list_export_masks(
        self, *, task_ids: list[uuid.UUID], geometry_type: str
    ) -> dict[uuid.UUID, list[ExportMaskRecord]]:
        if not task_ids:
            return {}
        rows = (
            (
                await self._db.execute(
                    select(annotation_row)
                    .where(
                        annotation_row.task_id.in_(task_ids),
                        annotation_row.is_active.is_(True),
                        annotation_row.was_cancelled.is_(False),
                        annotation_row.geometry["type"].astext == geometry_type,
                    )
                    .order_by(annotation_row.task_id, annotation_row.id)
                )
            )
            .scalars()
            .all()
        )
        grouped: dict[uuid.UUID, list[ExportMaskRecord]] = defaultdict(list)
        for row in rows:
            grouped[row.task_id].append(
                ExportMaskRecord(
                    id=row.id,
                    task_id=row.task_id,
                    geometry=dict(row.geometry or {}),
                    z_order=int(row.z_order or 0),
                )
            )
        return dict(grouped)

    async def list_export_raster_masks(
        self, *, task_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, list[ExportMaskRecord]]:
        return await self._list_export_masks(
            task_ids=task_ids, geometry_type="raster_mask"
        )

    async def list_export_video_masks(
        self, *, task_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, list[ExportMaskRecord]]:
        return await self._list_export_masks(
            task_ids=task_ids, geometry_type="video_track_mask"
        )

    async def preview_native_json(
        self, payload_bytes: bytes, *, overwrite: bool
    ) -> FormatImportPreviewResult:
        preview = await import_native_json_annotations(
            self._db,
            self._project.project_id,
            payload_bytes,
            operator_user_id=self._project.owner_id,
            overwrite=overwrite,
            dry_run=True,
        )
        return FormatImportPreviewResult(
            errors=tuple(error.reason for error in preview.errors)
        )

    async def commit_native_json(
        self,
        payload_bytes: bytes,
        *,
        operator_user_id: uuid.UUID,
        overwrite: bool,
    ) -> FormatImportCommitResult:
        result = await import_native_json_annotations(
            self._db,
            self._project.project_id,
            payload_bytes,
            operator_user_id=operator_user_id,
            overwrite=overwrite,
            dry_run=False,
        )
        return FormatImportCommitResult(
            imported=result.imported,
            skipped=result.skipped,
            errors=tuple(error.reason for error in result.errors),
        )


def format_host(
    db: AsyncSession, project: Any
) -> tuple[SqlAlchemyFormatHost, FormatProjectContext]:
    context = project_context_from_project(project)
    return SqlAlchemyFormatHost(db, context), context
