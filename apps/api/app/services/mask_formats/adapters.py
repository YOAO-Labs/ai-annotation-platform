from __future__ import annotations

import json
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.project import Project
from app.schemas.aap_json import AAPJsonV1Envelope
from app.schemas.mask_format import (
    MaskFormatCode,
    MaskFormatPlan,
    MaskFormatPlanItem,
)
from app.services.annotations_import import import_aap_json_annotations
from app.services.labeling_core_read_relations import annotation_row, task_row
from app.services.mask_formats.contracts import (
    MaskFormatDescriptor,
    StagedObject,
    canonical_digest,
)
from app.services.mask_formats.planning import _code, _plan, _worst_loss
from app.services.task_matcher import resolve_task
from app.services.exporting.video_scope import (
    VideoExportScope,
    clip_video_geometry,
)


_ALLOWED_GEOMETRY: dict[str, frozenset[str] | None] = {
    "aap_json": None,
    "coco": frozenset(
        {"bbox", "rotated_bbox", "polygon", "multi_polygon", "keypoint", "raster_mask"}
    ),
    "yolo": frozenset({"bbox"}),
    "yolo-det": frozenset({"bbox"}),
    "yolo-obb": frozenset({"rotated_bbox"}),
    "yolo-seg": frozenset({"polygon", "multi_polygon", "raster_mask"}),
    "label-studio-brush": frozenset({"raster_mask"}),
    "binary-png": frozenset({"raster_mask"}),
    "indexed-png": frozenset({"raster_mask"}),
    "video_json": None,
    "yolo-frames-det": frozenset({"video_track", "bbox"}),
    "yolo-frames-seg": frozenset({"video_track", "video_track_mask", "polygon"}),
    "coco-frames-seg": frozenset({"video_track", "video_track_mask", "polygon"}),
    "davis": frozenset({"video_track_mask"}),
    "youtube-vos": frozenset({"video_track_mask"}),
    "mots": frozenset({"video_track_mask"}),
    "mot": frozenset({"video_track", "bbox"}),
    "coco-multicamera": None,
    "kitti": None,
    "nuscenes": None,
    "pointmask": None,
    "voc": frozenset({"bbox"}),
}


_TARGET_LOSSES: dict[str, tuple[str, ...]] = {
    "video_json": ("nonportable_media_reference",),
    "yolo-seg": ("holes_polygonized", "components_split"),
    "yolo-frames-seg": (
        "holes_polygonized",
        "components_split",
        "track_identity_lost",
        "frame_base_changed",
    ),
    "yolo-frames-det": ("track_identity_lost", "frame_base_changed"),
    "coco-frames-seg": ("frame_base_changed",),
    "davis": ("occlusion_lost", "frame_base_changed"),
    "youtube-vos": ("sparse_frames_collapsed", "occlusion_lost"),
    "mots": ("occlusion_lost", "frame_base_changed"),
    "mot": ("frame_base_changed",),
    "kitti": ("frame_base_changed",),
}


class LegacyPackagingAdapter:
    def __init__(self, descriptor: MaskFormatDescriptor) -> None:
        self.descriptor = descriptor

    async def preflight_export(
        self,
        db: AsyncSession,
        *,
        project: Project,
        scope: dict[str, Any],
        options: dict[str, Any],
    ) -> MaskFormatPlan:
        video_scope = VideoExportScope.from_dict(scope.get("video_export_scope"))
        task_query = select(task_row.id, task_row.file_path).where(
            task_row.project_id == project.id
        )
        batch_id = scope.get("batch_id")
        if batch_id:
            task_query = task_query.where(task_row.batch_id == uuid.UUID(str(batch_id)))
        if video_scope is not None:
            task_query = task_query.where(task_row.id == video_scope.task_id)
        task_rows = list(
            (await db.execute(task_query.order_by(task_row.id).limit(100_001))).all()
        )
        if len(task_rows) > 100_000:
            raise ValueError("format_preflight_task_budget_exceeded")

        task_ids = [row.id for row in task_rows]
        grouped: dict[uuid.UUID, dict[str, int]] = defaultdict(dict)
        if task_ids and video_scope is not None:
            annotation_rows = (
                (
                    await db.execute(
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
                await db.execute(
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

        allowed = _ALLOWED_GEOMETRY.get(self.descriptor.format_id)
        target_losses = tuple(_TARGET_LOSSES.get(self.descriptor.format_id, ()))
        items: list[MaskFormatPlanItem] = []
        for task_id, file_path in task_rows:
            geometry_counts = grouped.get(task_id, {})
            unsupported = sorted(
                geometry_type
                for geometry_type, count in geometry_counts.items()
                if count and allowed is not None and geometry_type not in allowed
            )
            losses = [_code(code) for code in target_losses]
            warnings: list[MaskFormatCode] = []
            loss_class = "lossy" if losses else "lossless"
            if unsupported:
                loss_class = "unsupported"
                warnings.append(
                    _code("unsupported_geometry", geometry_types=unsupported)
                )
            object_count = sum(geometry_counts.values())
            items.append(
                MaskFormatPlanItem(
                    item_id=canonical_digest(
                        {
                            "format_id": self.descriptor.format_id,
                            "task_id": str(task_id),
                            "geometry_counts": geometry_counts,
                        }
                    ),
                    task_id=task_id,
                    media_path=file_path,
                    loss_class=loss_class,
                    estimated_objects=object_count,
                    estimated_files=1,
                    estimated_bytes=max(128, object_count * 512),
                    losses=losses,
                    warnings=warnings,
                )
            )

        loss_class = _worst_loss([item.loss_class for item in items])
        all_losses = [_code(code) for code in target_losses]
        return _plan(
            {
                "format_id": self.descriptor.format_id,
                "direction": "export",
                "adapter_version": self.descriptor.adapter_version,
                "manifest_version": self.descriptor.manifest_version,
                "media_type": project.data_type or "image",
                "mapping_digest": canonical_digest(
                    video_scope.as_dict() if video_scope else {}
                ),
                "options_digest": canonical_digest(options),
                "items": [item.model_dump(mode="json") for item in items],
                "loss_class": loss_class,
                "estimated_objects": sum(item.estimated_objects for item in items),
                "estimated_files": sum(item.estimated_files for item in items),
                "estimated_bytes": sum(item.estimated_bytes for item in items),
                "losses": [item.model_dump(mode="json") for item in all_losses],
            }
        )

    async def preflight_import(self, *_args, **_kwargs) -> MaskFormatPlan:
        raise ValueError(f"format_import_unsupported:{self.descriptor.format_id}")

    async def execute_import_item(self, *_args, **_kwargs) -> dict[str, Any]:
        raise ValueError(f"format_import_unsupported:{self.descriptor.format_id}")


def _aap_error_code(reason: str) -> str:
    lowered = reason.lower()
    if "task not found" in lowered:
        return "task_not_found"
    if "class_name" in lowered or "类别" in reason:
        return "unknown_label"
    if "size" in lowered or "尺寸" in reason:
        return "image_size_mismatch"
    return "unsupported_geometry"


def _aap_subset_bytes(envelope: AAPJsonV1Envelope, source_index: int) -> bytes:
    subset = envelope.model_copy(update={"tasks": [envelope.tasks[source_index]]})
    return json.dumps(
        subset.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


class AAPJsonAdapter(LegacyPackagingAdapter):
    async def _load(self, staged: StagedObject) -> AAPJsonV1Envelope:
        if staged.size_bytes > 64 * 1024 * 1024:
            raise ValueError("resource_budget_exceeded:max_aap_json_bytes")
        try:
            return AAPJsonV1Envelope.model_validate_json(
                Path(staged.local_path).read_bytes()
            )
        except Exception as exc:
            raise ValueError(f"aap_json_invalid:{exc}") from exc

    async def preflight_import(
        self,
        db: AsyncSession,
        *,
        project: Project,
        staged: StagedObject,
        mapping: dict[str, Any],
        options: dict[str, Any],
    ) -> MaskFormatPlan:
        envelope = await self._load(staged)
        items: list[MaskFormatPlanItem] = []
        unknown_labels: set[str] = set()
        for source_index, block in enumerate(envelope.tasks):
            match = block.task_match.model_dump(exclude_none=True)
            if "file_path" not in match and block.file_path:
                match["file_path"] = block.file_path
            task = await resolve_task(db, project.id, match)
            subset = _aap_subset_bytes(envelope, source_index)
            preview = await import_aap_json_annotations(
                db,
                project.id,
                subset,
                operator_user_id=project.owner_id,
                overwrite=bool(options.get("overwrite")),
                dry_run=True,
            )
            skips = [
                _code(_aap_error_code(error.reason), reason=error.reason)
                for error in preview.errors
            ]
            unknown_labels.update(
                entry.class_name
                for entry in block.annotations
                if entry.class_name
                and any(skip.code == "unknown_label" for skip in skips)
            )
            if not block.annotations:
                skips.append(_code("not_selected"))
            loss_class = "lossless" if not skips else "unsupported"
            items.append(
                MaskFormatPlanItem(
                    item_id=canonical_digest(block.model_dump(mode="json")),
                    task_id=task.id if task is not None else None,
                    media_path=block.file_path or match.get("file_path"),
                    source_index=source_index,
                    loss_class=loss_class,
                    estimated_objects=len(block.annotations),
                    estimated_files=1,
                    estimated_bytes=len(subset),
                    skips=skips,
                )
            )
        return _plan(
            {
                "format_id": self.descriptor.format_id,
                "direction": "import",
                "adapter_version": self.descriptor.adapter_version,
                "manifest_version": self.descriptor.manifest_version,
                "media_type": project.data_type or "image",
                "staged_object_key": staged.object_key,
                "staged_sha256": staged.sha256,
                "mapping_digest": canonical_digest(mapping),
                "options_digest": canonical_digest(options),
                "items": [item.model_dump(mode="json") for item in items],
                "loss_class": _worst_loss([item.loss_class for item in items]),
                "unknown_labels": sorted(unknown_labels),
                "estimated_objects": sum(item.estimated_objects for item in items),
                "estimated_files": sum(item.estimated_files for item in items),
                "estimated_bytes": staged.size_bytes,
                "skips": [
                    skip.model_dump(mode="json")
                    for item in items
                    for skip in item.skips
                ],
            }
        )

    async def execute_import_item(
        self,
        db: AsyncSession,
        *,
        project: Project,
        staged: StagedObject,
        plan: MaskFormatPlan,
        item_index: int,
        operator_user_id,
        mapping: dict[str, Any] | None = None,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        item = plan.items[item_index]
        if item.loss_class == "unsupported" or item.task_id is None:
            return {
                "status": "skipped",
                "skip_code": item.skips[0].code if item.skips else "not_selected",
            }
        envelope = await self._load(staged)
        if item.source_index is None or item.source_index >= len(envelope.tasks):
            raise ValueError("format_plan_source_index_conflict")
        block = envelope.tasks[item.source_index]
        if canonical_digest(block.model_dump(mode="json")) != item.item_id:
            raise ValueError("format_plan_item_digest_conflict")
        match = block.task_match.model_dump(exclude_none=True)
        if "file_path" not in match and block.file_path:
            match["file_path"] = block.file_path
        resolved = await resolve_task(db, project.id, match)
        if resolved is None or resolved.id != item.task_id:
            raise ValueError("format_plan_task_conflict")
        result = await import_aap_json_annotations(
            db,
            project.id,
            _aap_subset_bytes(envelope, item.source_index),
            operator_user_id=operator_user_id,
            overwrite=bool((options or {}).get("overwrite")),
            dry_run=False,
        )
        if result.errors or result.skipped:
            raise ValueError("format_execute_diverged_from_preflight")
        return {
            "status": "committed",
            "task_id": str(item.task_id),
            "imported": result.imported,
            "skipped": result.skipped,
        }
