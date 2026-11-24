from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from app.schemas.mask_format import (
    MaskFormatCode,
    MaskFormatPlan,
    MaskFormatPlanItem,
)
from app.schemas.native_json import NativeJsonEnvelope, decode_native_json
from app.services.exporting.video import (
    VIDEO_BBOX_COMPATIBLE_SINGLE_FRAME_GEOMETRY_TYPES,
    VIDEO_TRACK_GEOMETRY_TYPES,
)
from app.services.exporting.video_scope import VideoExportScope
from app.services.mask_formats.contracts import (
    FormatHostPort,
    FormatProjectContext,
    MaskFormatDescriptor,
    StagedObject,
    canonical_digest,
)
from app.services.mask_formats.planning import _code, _plan, _worst_loss


_ALLOWED_GEOMETRY: dict[str, frozenset[str] | None] = {
    "native_json": None,
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
    "yolo-frames-det": (
        VIDEO_TRACK_GEOMETRY_TYPES | VIDEO_BBOX_COMPATIBLE_SINGLE_FRAME_GEOMETRY_TYPES
    ),
    "yolo-frames-seg": frozenset({"video_track_polygon", "video_polygon"}),
    "coco-frames-seg": frozenset(
        {"video_track_polygon", "video_track_mask", "video_polygon"}
    ),
    "davis": frozenset({"video_track_mask"}),
    "youtube-vos": frozenset({"video_track_mask"}),
    "mots": frozenset({"video_track_mask"}),
    "mot": VIDEO_TRACK_GEOMETRY_TYPES
    | VIDEO_BBOX_COMPATIBLE_SINGLE_FRAME_GEOMETRY_TYPES,
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
        host: FormatHostPort,
        *,
        project: FormatProjectContext,
        scope: dict[str, Any],
        options: dict[str, Any],
    ) -> MaskFormatPlan:
        video_scope = VideoExportScope.from_dict(scope.get("video_export_scope"))
        task_summaries = await host.list_export_task_geometries(scope=scope)

        allowed = _ALLOWED_GEOMETRY.get(self.descriptor.format_id)
        target_losses = tuple(_TARGET_LOSSES.get(self.descriptor.format_id, ()))
        items: list[MaskFormatPlanItem] = []
        for summary in task_summaries:
            geometry_counts = summary.geometry_counts
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
                            "task_id": str(summary.task_id),
                            "geometry_counts": geometry_counts,
                        }
                    ),
                    task_id=summary.task_id,
                    media_path=summary.file_path,
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


def _native_import_error_code(reason: str) -> str:
    lowered = reason.lower()
    if "task not found" in lowered:
        return "task_not_found"
    if "class_name" in lowered or "类别" in reason:
        return "unknown_label"
    if "size" in lowered or "尺寸" in reason:
        return "image_size_mismatch"
    return "unsupported_geometry"


def _native_task_subset_bytes(envelope: NativeJsonEnvelope, source_index: int) -> bytes:
    subset = envelope.model_copy(update={"tasks": [envelope.tasks[source_index]]})
    return json.dumps(
        subset.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


class NativeJsonAdapter(LegacyPackagingAdapter):
    async def _load(self, staged: StagedObject) -> NativeJsonEnvelope:
        if staged.size_bytes > 64 * 1024 * 1024:
            raise ValueError("resource_budget_exceeded:max_native_json_bytes")
        try:
            return decode_native_json(json.loads(Path(staged.local_path).read_bytes()))
        except Exception as exc:
            raise ValueError(f"native_json_invalid:{exc}") from exc

    async def preflight_import(
        self,
        host: FormatHostPort,
        *,
        project: FormatProjectContext,
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
            task = await host.resolve_task(match)
            subset = _native_task_subset_bytes(envelope, source_index)
            preview = await host.preview_native_json(
                subset,
                overwrite=bool(options.get("overwrite")),
            )
            skips = [
                _code(_native_import_error_code(reason), reason=reason)
                for reason in preview.errors
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
                    task_id=task.task_id if task is not None else None,
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
        host: FormatHostPort,
        *,
        project: FormatProjectContext,
        staged: StagedObject,
        plan: MaskFormatPlan,
        item_index: int,
        operator_user_id: uuid.UUID,
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
        resolved = await host.resolve_task(match)
        if resolved is None or resolved.task_id != item.task_id:
            raise ValueError("format_plan_task_conflict")
        result = await host.commit_native_json(
            _native_task_subset_bytes(envelope, item.source_index),
            operator_user_id=operator_user_id,
            overwrite=bool((options or {}).get("overwrite")),
        )
        if result.errors or result.skipped:
            raise ValueError("format_execute_diverged_from_preflight")
        return {
            "status": "committed",
            "task_id": str(item.task_id),
            "imported": result.imported,
            "skipped": result.skipped,
        }
