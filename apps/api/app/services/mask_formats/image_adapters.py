from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models.dataset import DatasetItem
from app.db.models.annotation import Annotation
from app.db.models.project import Project
from app.db.models.task import Task
from app.schemas.mask_format import MaskFormatPlan, MaskFormatPlanItem
from app.services.annotations_import import import_aap_json_annotations
from app.services.labeling_core_read_relations import annotation_row
from app.services.mask_formats.contracts import (
    MaskFormatDescriptor,
    StagedObject,
    canonical_digest,
)
from app.services.mask_formats.image_codecs import (
    binary_png_to_coco,
    indexed_png_to_coco,
    label_studio_rle_to_coco,
    normalize_coco_segmentation_rle,
    rasterize_coco_polygons,
    rasterize_normalized_polygon,
)
from app.services.mask_formats.image_export import indexed_overlap_summary
from aap_assets_source.archive import ArchiveLimits, SafeZipArchive
from aap_workspace.configuration_policy import lookup_classes_for_tool_unit
from app.services.raster_mask_storage import build_rle_reference
from app.services.task_matcher import resolve_task, resolve_task_by_file_stem
from app.utils.raster_mask_rle import coco_rle_area, validate_coco_rle

from .adapters import LegacyPackagingAdapter
from .png_contract import validate_png_contract
from .planning import _code, _plan, _worst_loss


@dataclass
class ParsedMask:
    class_name: str
    rle: dict[str, Any]
    source_id: str | None = None


@dataclass
class ParsedImageItem:
    media_path: str
    width: int
    height: int
    masks: list[ParsedMask]
    task: Task | None = None
    losses: list[str] = field(default_factory=list)
    warnings: list[tuple[str, dict[str, Any]]] = field(default_factory=list)


def _archive_limits() -> ArchiveLimits:
    return ArchiveLimits(
        max_files=settings.mask_format_max_archive_files,
        max_entry_bytes=settings.mask_format_max_entry_bytes,
        max_total_bytes=settings.mask_format_temp_quota_bytes,
        max_compression_ratio=settings.mask_format_max_compression_ratio,
    )


def _mapped_label(raw: str, mapping: dict[str, Any]) -> str:
    labels = (
        mapping.get("labels") if isinstance(mapping.get("labels"), dict) else mapping
    )
    mapped = labels.get(raw, raw) if isinstance(labels, dict) else raw
    return str(mapped).strip()


def _project_mask_classes(project: Project) -> set[str]:
    return lookup_classes_for_tool_unit(project.tool_bindings or {}, "region")


async def _task_dimensions(
    db: AsyncSession, task: Task | None
) -> tuple[int, int] | None:
    if task is None or task.dataset_item_id is None:
        return None
    item = await db.get(DatasetItem, task.dataset_item_id)
    if item is None or not item.width or not item.height:
        return None
    return int(item.width), int(item.height)


async def _resolve_image_task(
    db: AsyncSession, project_id: uuid.UUID, media_path: str
) -> Task | None:
    task = await resolve_task(db, project_id, {"file_path": media_path})
    if task is not None:
        return task
    task, _reason = await resolve_task_by_file_stem(db, project_id, media_path)
    return task


def _item_identity(item: ParsedImageItem) -> str:
    return canonical_digest(
        {
            "media_path": item.media_path,
            "width": item.width,
            "height": item.height,
            "masks": [
                {
                    "class_name": mask.class_name,
                    "source_id": mask.source_id,
                    "rle": mask.rle,
                }
                for mask in item.masks
            ],
            "losses": item.losses,
        }
    )


def _read_json_file(path: str, max_bytes: int = 64 * 1024 * 1024) -> Any:
    data = Path(path).read_bytes()
    if len(data) > max_bytes:
        raise ValueError("resource_budget_exceeded:max_json_bytes")
    try:
        return json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("format JSON is invalid") from exc


def _read_archive_json(archive: SafeZipArchive, path: str) -> Any:
    with archive.open(path) as source:
        data = source.read()
    try:
        return json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"archive JSON is invalid: {path}") from exc


async def _parse_coco(
    db: AsyncSession,
    project: Project,
    staged: StagedObject,
    mapping: dict[str, Any],
    _options: dict[str, Any],
) -> list[ParsedImageItem]:
    raw = _read_json_file(staged.local_path)
    if not isinstance(raw, dict):
        raise ValueError("COCO JSON root must be an object")
    categories = {
        row.get("id"): row.get("name")
        for row in raw.get("categories") or []
        if isinstance(row, dict)
        and isinstance(row.get("id"), int)
        and isinstance(row.get("name"), str)
    }
    annotations: dict[int, list[dict[str, Any]]] = {}
    for annotation in raw.get("annotations") or []:
        if isinstance(annotation, dict) and isinstance(annotation.get("image_id"), int):
            annotations.setdefault(annotation["image_id"], []).append(annotation)
    items: list[ParsedImageItem] = []
    for image in raw.get("images") or []:
        if not isinstance(image, dict) or not isinstance(image.get("id"), int):
            continue
        media_path = str(image.get("file_name") or "").strip()
        width, height = image.get("width"), image.get("height")
        if (
            type(width) is not int
            or type(height) is not int
            or width <= 0
            or height <= 0
        ):
            raise ValueError("COCO image width / height must be positive integers")
        task = await _resolve_image_task(db, project.id, media_path)
        masks: list[ParsedMask] = []
        for annotation in annotations.get(image["id"], []):
            raw_label = categories.get(annotation.get("category_id"))
            if not raw_label:
                raise ValueError(
                    f"unknown COCO category_id: {annotation.get('category_id')!r}"
                )
            segmentation = annotation.get("segmentation")
            if isinstance(segmentation, dict):
                rle = normalize_coco_segmentation_rle(
                    segmentation, expected_width=width, expected_height=height
                )
            elif isinstance(segmentation, list):
                rle = rasterize_coco_polygons(segmentation, width=width, height=height)
            else:
                continue
            if coco_rle_area(rle) == 0:
                raise ValueError("COCO mask must contain foreground pixels")
            masks.append(
                ParsedMask(
                    class_name=_mapped_label(raw_label, mapping),
                    rle=rle,
                    source_id=str(annotation.get("id"))
                    if annotation.get("id") is not None
                    else None,
                )
            )
        items.append(ParsedImageItem(media_path, width, height, masks, task=task))
    return items


async def _parse_label_studio(
    db: AsyncSession,
    project: Project,
    staged: StagedObject,
    mapping: dict[str, Any],
    options: dict[str, Any],
) -> list[ParsedImageItem]:
    raw = _read_json_file(staged.local_path)
    tasks = raw if isinstance(raw, list) else [raw]
    data_key = str(options.get("data_key") or "image")
    expected_from = str(options.get("from_name") or "brush")
    expected_to = str(options.get("to_name") or "image")
    items: list[ParsedImageItem] = []
    for task_row in tasks:
        if not isinstance(task_row, dict):
            continue
        data = task_row.get("data") if isinstance(task_row.get("data"), dict) else {}
        media_path = str(data.get(data_key) or "").strip()
        platform_task = await _resolve_image_task(db, project.id, media_path)
        containers = task_row.get("annotations") or task_row.get("predictions") or []
        results: list[dict[str, Any]] = []
        for container in containers:
            if isinstance(container, dict):
                results.extend(
                    row
                    for row in container.get("result") or []
                    if isinstance(row, dict)
                )
        masks: list[ParsedMask] = []
        width = height = 0
        for result in results:
            if str(result.get("type") or "").lower() != "brushlabels":
                continue
            if (
                result.get("from_name") != expected_from
                or result.get("to_name") != expected_to
            ):
                raise ValueError(
                    "Label Studio from_name / to_name does not match adapter options"
                )
            value = result.get("value") if isinstance(result.get("value"), dict) else {}
            if value.get("format") != "rle" or not isinstance(value.get("rle"), list):
                raise ValueError(
                    "Label Studio BrushLabels result requires value.format=rle"
                )
            labels = value.get("brushlabels")
            if (
                not isinstance(labels, list)
                or len(labels) != 1
                or not isinstance(labels[0], str)
            ):
                raise ValueError(
                    "Label Studio BrushLabels result requires exactly one label"
                )
            row_width, row_height = (
                result.get("original_width"),
                result.get("original_height"),
            )
            if (
                type(row_width) is not int
                or type(row_height) is not int
                or row_width <= 0
                or row_height <= 0
            ):
                raise ValueError(
                    "Label Studio original dimensions must be positive integers"
                )
            if width and (width, height) != (row_width, row_height):
                raise ValueError(
                    "Label Studio task has inconsistent original dimensions"
                )
            width, height = row_width, row_height
            rle = label_studio_rle_to_coco(value["rle"], width=width, height=height)
            if coco_rle_area(rle) == 0:
                raise ValueError("Label Studio mask must contain foreground pixels")
            masks.append(
                ParsedMask(
                    class_name=_mapped_label(labels[0], mapping),
                    rle=rle,
                    source_id=str(result.get("id"))
                    if result.get("id") is not None
                    else None,
                )
            )
        if not width or not height:
            dims = await _task_dimensions(db, platform_task)
            if dims:
                width, height = dims
        items.append(
            ParsedImageItem(media_path, width, height, masks, task=platform_task)
        )
    return items


def _manifest_items(raw: Any, expected_format: str) -> list[dict[str, Any]]:
    if not isinstance(raw, dict) or raw.get("format_id") != expected_format:
        raise ValueError(f"manifest format_id must be {expected_format}")
    if str(raw.get("manifest_version")) != "1":
        raise ValueError("unsupported mask PNG manifest version")
    if raw.get("adapter_version") != "1.0.0":
        raise ValueError("unsupported mask PNG adapter version")
    items = raw.get("items")
    if not isinstance(items, list):
        raise ValueError("manifest items must be an array")
    return [item for item in items if isinstance(item, dict)]


async def _parse_png_archive(
    db: AsyncSession,
    project: Project,
    staged: StagedObject,
    mapping: dict[str, Any],
    _options: dict[str, Any],
    *,
    indexed: bool,
) -> list[ParsedImageItem]:
    expected_format = "indexed-png" if indexed else "binary-png"
    with SafeZipArchive(staged.local_path, _archive_limits()) as archive:
        raw = _read_archive_json(archive, "manifest.json")
        manifest_items = _manifest_items(raw, expected_format)
        referenced: list[str] = []
        for item in manifest_items:
            if indexed:
                referenced.append(str(item.get("mask_path") or ""))
            else:
                referenced.extend(
                    str(instance.get("mask_path") or "")
                    for instance in item.get("instances") or []
                    if isinstance(instance, dict)
                )
        archive.require_paths(referenced)
        items: list[ParsedImageItem] = []
        for item in manifest_items:
            media_path = str(item.get("media_path") or "").strip()
            if not media_path:
                raise ValueError("manifest item requires media_path")
            width, height = item.get("width"), item.get("height")
            if (
                type(width) is not int
                or type(height) is not int
                or width <= 0
                or height <= 0
            ):
                raise ValueError("manifest dimensions must be positive integers")
            task = await _resolve_image_task(db, project.id, media_path)
            masks: list[ParsedMask] = []
            shared_png: bytes | None = None
            shared_sha256: str | None = None
            if indexed:
                mask_path = str(item.get("mask_path") or "")
                with archive.open(mask_path) as source:
                    validate_png_contract(
                        source, expected_width=width, expected_height=height
                    )
                with archive.open(mask_path) as source:
                    shared_png = source.read()
                shared_sha256 = hashlib.sha256(shared_png).hexdigest()
                if item.get("content_sha256") != shared_sha256:
                    raise ValueError("indexed PNG content_sha256 mismatch")
            instance_ids: set[str] = set()
            pixel_ids: set[int] = set()
            for instance in item.get("instances") or []:
                if not isinstance(instance, dict):
                    continue
                class_id = instance.get("class_id")
                if type(class_id) is not int or class_id < 0:
                    raise ValueError("manifest instance requires non-negative class_id")
                instance_id = str(instance.get("instance_id") or "").strip()
                if not instance_id or instance_id in instance_ids:
                    raise ValueError("manifest instance_id must be present and unique")
                instance_ids.add(instance_id)
                if not str(instance.get("source_annotation_id") or "").strip():
                    raise ValueError("manifest instance requires source_annotation_id")
                source_version = instance.get("source_annotation_version")
                if type(source_version) is not int or source_version < 1:
                    raise ValueError(
                        "manifest instance requires positive source_annotation_version"
                    )
                if indexed:
                    pixel_id = instance.get("pixel_id")
                    if type(pixel_id) is not int:
                        raise ValueError(
                            "indexed PNG instance requires integer pixel_id"
                        )
                    if pixel_id in pixel_ids:
                        raise ValueError("indexed PNG pixel_id must be unique")
                    pixel_ids.add(pixel_id)
                    assert shared_png is not None
                    rle = indexed_png_to_coco(
                        shared_png, width=width, height=height, pixel_id=pixel_id
                    )
                else:
                    mask_path = str(instance.get("mask_path") or "")
                    with archive.open(mask_path) as source:
                        validate_png_contract(
                            source,
                            expected_width=width,
                            expected_height=height,
                            allowed_color_types=frozenset({0}),
                        )
                    with archive.open(mask_path) as source:
                        png = source.read()
                    if (
                        instance.get("content_sha256")
                        != hashlib.sha256(png).hexdigest()
                    ):
                        raise ValueError("binary PNG content_sha256 mismatch")
                    rle = binary_png_to_coco(png, width=width, height=height)
                if coco_rle_area(rle) == 0:
                    raise ValueError("PNG mask must contain foreground pixels")
                raw_label = str(instance.get("class_name") or "").strip()
                if not raw_label:
                    raise ValueError("manifest instance requires class_name")
                masks.append(
                    ParsedMask(
                        class_name=_mapped_label(raw_label, mapping),
                        rle=rle,
                        source_id=str(instance.get("source_annotation_id") or "")
                        or None,
                    )
                )
            items.append(ParsedImageItem(media_path, width, height, masks, task=task))
        return items


def _yolo_classes(archive: SafeZipArchive) -> list[str]:
    from app.services.predictions_import import _parse_yolo_yaml_names

    for entry in archive.entries:
        if entry.normalized_path.rsplit("/", 1)[-1] == "classes.txt":
            with archive.open(entry.normalized_path) as source:
                return [
                    line.strip()
                    for line in source.read().decode("utf-8-sig").splitlines()
                    if line.strip() and not line.lstrip().startswith("#")
                ]
    for entry in archive.entries:
        if entry.normalized_path.rsplit("/", 1)[-1] not in {"data.yaml", "data.yml"}:
            continue
        with archive.open(entry.normalized_path) as source:
            names = _parse_yolo_yaml_names(source.read().decode("utf-8-sig"))
        if names:
            return names
    return []


async def _parse_yolo(
    db: AsyncSession,
    project: Project,
    staged: StagedObject,
    mapping: dict[str, Any],
    _options: dict[str, Any],
) -> list[ParsedImageItem]:
    with SafeZipArchive(
        staged.local_path, _archive_limits(), skip_unsafe_paths=False
    ) as archive:
        classes = _yolo_classes(archive)
        if not classes:
            raise ValueError("YOLO archive requires classes.txt or data.yaml names")
        label_paths = sorted(
            entry.normalized_path
            for entry in archive.entries
            if entry.normalized_path.lower().endswith(".txt")
            and entry.normalized_path.rsplit("/", 1)[-1] != "classes.txt"
            and not entry.normalized_path.lower().endswith(".attrs.txt")
        )
        items: list[ParsedImageItem] = []
        for label_path in label_paths:
            task, _reason = await resolve_task_by_file_stem(db, project.id, label_path)
            dims = await _task_dimensions(db, task)
            if dims is None:
                width = height = 0
            else:
                width, height = dims
            masks: list[ParsedMask] = []
            with archive.open(label_path) as source:
                lines = source.read().decode("utf-8-sig").splitlines()
            for line_number, line in enumerate(lines, start=1):
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                parts = stripped.split()
                try:
                    class_id = int(parts[0])
                    values = [float(value) for value in parts[1:]]
                except (ValueError, IndexError) as exc:
                    raise ValueError(
                        f"invalid YOLO row: {label_path}:{line_number}"
                    ) from exc
                if class_id < 0 or class_id >= len(classes):
                    raise ValueError(f"YOLO class index out of range: {class_id}")
                if len(values) < 6 or len(values) % 2:
                    raise ValueError("YOLO Seg row requires at least three x/y pairs")
                if width <= 0 or height <= 0:
                    continue
                points = [
                    [values[index], values[index + 1]]
                    for index in range(0, len(values), 2)
                ]
                rle = rasterize_normalized_polygon(points, width=width, height=height)
                if coco_rle_area(rle) == 0:
                    raise ValueError("YOLO polygon rasterizes to an empty mask")
                masks.append(
                    ParsedMask(
                        class_name=_mapped_label(classes[class_id], mapping),
                        rle=rle,
                        source_id=f"{label_path}:{line_number}",
                    )
                )
            media_path = task.file_path if task is not None else label_path
            items.append(
                ParsedImageItem(
                    media_path,
                    width,
                    height,
                    masks,
                    task=task,
                    losses=["holes_polygonized", "components_split"],
                )
            )
        return items


Parser = Any


class ImageMaskImportAdapter:
    def __init__(self, descriptor: MaskFormatDescriptor, parser: Parser) -> None:
        self.descriptor = descriptor
        self._parser = parser

    async def _items(
        self,
        db: AsyncSession,
        project: Project,
        staged: StagedObject,
        mapping: dict[str, Any],
        options: dict[str, Any],
    ) -> list[ParsedImageItem]:
        items = await self._parser(db, project, staged, mapping, options)
        if len(items) > 100_000:
            raise ValueError("resource_budget_exceeded:max_import_items")
        return items

    async def preflight_import(
        self,
        db: AsyncSession,
        *,
        project: Project,
        staged: StagedObject,
        mapping: dict[str, Any],
        options: dict[str, Any],
    ) -> MaskFormatPlan:
        items = await self._items(db, project, staged, mapping, options)
        allowed_classes = _project_mask_classes(project)
        unknown_labels: set[str] = set()
        size_conflicts: list[dict[str, Any]] = []
        plan_items: list[MaskFormatPlanItem] = []
        for index, item in enumerate(items):
            skips = []
            warnings = [_code(code, **detail) for code, detail in item.warnings]
            task = item.task
            if task is None:
                skips.append(_code("task_not_found", media_path=item.media_path))
            dimensions = await _task_dimensions(db, task)
            if task is not None and dimensions is None:
                skips.append(
                    _code("image_size_mismatch", reason="task dimensions unavailable")
                )
            elif dimensions is not None and dimensions != (item.width, item.height):
                conflict = {
                    "media_path": item.media_path,
                    "expected": list(dimensions),
                    "observed": [item.width, item.height],
                }
                size_conflicts.append(conflict)
                skips.append(_code("image_size_mismatch", **conflict))
            for mask in item.masks:
                validate_coco_rle(mask.rle)
                if allowed_classes and mask.class_name not in allowed_classes:
                    unknown_labels.add(mask.class_name)
            if any(mask.class_name in unknown_labels for mask in item.masks):
                skips.append(
                    _code(
                        "unknown_label",
                        labels=sorted(
                            {
                                mask.class_name
                                for mask in item.masks
                                if mask.class_name in unknown_labels
                            }
                        ),
                    )
                )
            if not item.masks:
                skips.append(_code("not_selected"))
            loss_class = (
                "unsupported" if skips else ("lossy" if item.losses else "lossless")
            )
            plan_items.append(
                MaskFormatPlanItem(
                    item_id=_item_identity(item),
                    task_id=task.id if task else None,
                    media_path=item.media_path,
                    source_index=index,
                    loss_class=loss_class,
                    estimated_objects=len(item.masks),
                    estimated_files=1,
                    estimated_bytes=sum(
                        len(mask.rle["counts"]) * 4 for mask in item.masks
                    ),
                    losses=[_code(code) for code in item.losses],
                    skips=skips,
                    warnings=warnings,
                )
            )
        return _plan(
            {
                "format_id": self.descriptor.format_id,
                "direction": "import",
                "adapter_version": self.descriptor.adapter_version,
                "manifest_version": self.descriptor.manifest_version,
                "media_type": "image",
                "staged_object_key": staged.object_key,
                "staged_sha256": staged.sha256,
                "mapping_digest": canonical_digest(mapping),
                "options_digest": canonical_digest(options),
                "items": [item.model_dump(mode="json") for item in plan_items],
                "loss_class": _worst_loss([item.loss_class for item in plan_items]),
                "unknown_labels": sorted(unknown_labels),
                "size_conflicts": size_conflicts,
                "estimated_objects": sum(item.estimated_objects for item in plan_items),
                "estimated_files": sum(item.estimated_files for item in plan_items),
                "estimated_bytes": staged.size_bytes,
                "losses": [
                    loss.model_dump(mode="json")
                    for item in plan_items
                    for loss in item.losses
                ],
                "skips": [
                    skip.model_dump(mode="json")
                    for item in plan_items
                    for skip in item.skips
                ],
                "warnings": [
                    warning.model_dump(mode="json")
                    for item in plan_items
                    for warning in item.warnings
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
        operator_user_id: uuid.UUID,
        mapping: dict[str, Any],
        options: dict[str, Any],
    ) -> dict[str, Any]:
        plan_item = plan.items[item_index]
        if plan_item.loss_class == "unsupported" or plan_item.task_id is None:
            return {
                "status": "skipped",
                "skip_code": plan_item.skips[0].code
                if plan_item.skips
                else "not_selected",
            }
        items = await self._items(db, project, staged, mapping, options)
        if plan_item.source_index is None or plan_item.source_index >= len(items):
            raise ValueError("format_plan_source_index_conflict")
        item = items[plan_item.source_index]
        if _item_identity(item) != plan_item.item_id:
            raise ValueError("format_plan_item_digest_conflict")
        if item.task is None or item.task.id != plan_item.task_id:
            raise ValueError("format_plan_task_conflict")
        mask_objects: dict[str, dict[str, Any]] = {}
        annotations: list[dict[str, Any]] = []
        for mask in item.masks:
            reference = build_rle_reference(mask.rle)
            mask_objects[reference["sha256"]] = mask.rle
            annotations.append(
                {
                    "geometry": {"type": "raster_mask", "mask": reference},
                    "class_name": mask.class_name,
                    "tool_unit_id": "region",
                    "source": "manual",
                    "attributes": {
                        "_import_format": self.descriptor.format_id,
                        "_import_source_id": mask.source_id,
                    },
                }
            )
        payload = {
            "schema_version": "1.3",
            "mask_objects": mask_objects,
            "tasks": [
                {
                    "task_match": {"display_id": item.task.display_id},
                    "file_path": item.task.file_path,
                    "media_type": "image",
                    "annotations": annotations,
                }
            ],
        }
        result = await import_aap_json_annotations(
            db,
            project.id,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(),
            operator_user_id=operator_user_id,
            overwrite=bool(options.get("overwrite")),
            dry_run=False,
        )
        if result.errors or result.skipped:
            raise ValueError("format_execute_diverged_from_preflight")
        return {
            "status": "committed",
            "task_id": str(item.task.id),
            "imported": result.imported,
            "skipped": 0,
        }

    async def preflight_export(
        self,
        db: AsyncSession,
        *,
        project: Project,
        scope: dict[str, Any],
        options: dict[str, Any],
    ) -> MaskFormatPlan:
        base = await LegacyPackagingAdapter(self.descriptor).preflight_export(
            db, project=project, scope=scope, options=options
        )
        if self.descriptor.format_id != "indexed-png":
            return base

        overlap_policy = str(options.get("indexed_overlap_policy") or "error")
        if overlap_policy not in {"error", "z_order", "larger_area", "smaller_area"}:
            raise ValueError("invalid indexed PNG overlap policy")
        task_ids = [item.task_id for item in base.items if item.task_id is not None]
        grouped: dict[uuid.UUID, list[Annotation]] = {}
        if task_ids:
            rows = (
                (
                    await db.execute(
                        select(annotation_row)
                        .where(
                            annotation_row.task_id.in_(task_ids),
                            annotation_row.is_active.is_(True),
                            annotation_row.was_cancelled.is_(False),
                            annotation_row.geometry["type"].astext == "raster_mask",
                        )
                        .order_by(annotation_row.task_id, annotation_row.id)
                    )
                )
                .scalars()
                .all()
            )
            for annotation in rows:
                grouped.setdefault(annotation.task_id, []).append(annotation)

        overlap_conflicts: list[dict[str, Any]] = []
        updated_items: list[MaskFormatPlanItem] = []
        for item in base.items:
            annotations = grouped.get(item.task_id, []) if item.task_id else []
            warnings = list(item.warnings)
            losses = list(item.losses)
            loss_class = item.loss_class
            if len(annotations) > 255:
                loss_class = "unsupported"
                warnings.append(
                    _code("instance_id_overflow", observed=len(annotations), limit=255)
                )
            elif annotations:
                lost, _count = await indexed_overlap_summary(
                    annotations,
                    overlap_policy=(
                        "z_order" if overlap_policy == "error" else overlap_policy
                    ),
                )
                if lost:
                    conflict = {
                        "task_id": str(item.task_id),
                        "media_path": item.media_path,
                        "overlap_policy": overlap_policy,
                        "covered_pixels": lost,
                    }
                    overlap_conflicts.append(conflict)
                    if overlap_policy == "error":
                        loss_class = "unsupported"
                        warnings.append(_code("overlap_policy_required", **conflict))
                    else:
                        loss_class = "lossy"
                        losses.append(_code("overlap_resolved", **conflict))
            updated_items.append(
                item.model_copy(
                    update={
                        "loss_class": loss_class,
                        "warnings": warnings,
                        "losses": losses,
                    }
                )
            )
        payload = base.model_dump(mode="json", exclude={"plan_digest"})
        payload.update(
            {
                "items": [item.model_dump(mode="json") for item in updated_items],
                "loss_class": _worst_loss([item.loss_class for item in updated_items]),
                "overlap_conflicts": overlap_conflicts,
                "losses": [
                    loss.model_dump(mode="json")
                    for item in updated_items
                    for loss in item.losses
                ],
                "warnings": [
                    warning.model_dump(mode="json")
                    for item in updated_items
                    for warning in item.warnings
                ],
            }
        )
        return _plan(payload)


def binary_png_parser(db, project, staged, mapping, options):
    return _parse_png_archive(db, project, staged, mapping, options, indexed=False)


def indexed_png_parser(db, project, staged, mapping, options):
    return _parse_png_archive(db, project, staged, mapping, options, indexed=True)


IMAGE_IMPORT_PARSERS = {
    "coco": _parse_coco,
    "label-studio-brush": _parse_label_studio,
    "binary-png": binary_png_parser,
    "indexed-png": indexed_png_parser,
    "yolo-seg": _parse_yolo,
}
