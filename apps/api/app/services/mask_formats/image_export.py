from __future__ import annotations

import hashlib
import json
from typing import Any
from zipfile import ZipFile

from app.db.models.annotation import Annotation
from app.db.models.dataset import DatasetItem
from app.db.models.project import Project
from app.db.models.task import Task
from app.services.mask_formats.image_codecs import (
    binary_png_bytes,
    compose_indexed_mask,
    encode_label_studio_mask,
    indexed_png_bytes,
    mask_to_yolo_polygons,
)
from app.services.project import derive_classes_list
from app.services.raster_mask_storage import load_coco_rle
from app.utils.raster_mask_rle import decode_coco_rle, validate_coco_rle


def _dimensions(
    task: Task, item: DatasetItem | None, rle: dict[str, Any]
) -> tuple[int, int]:
    height, width, _ = validate_coco_rle(rle)
    if item is not None and item.width and item.height:
        expected = (int(item.width), int(item.height))
        if expected != (width, height):
            raise ValueError("image_size_mismatch:raster mask and dataset item")
    return width, height


def _manifest_instance(
    annotation: Annotation,
    *,
    class_id: int,
    mask_path: str | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "class_id": class_id,
        "class_name": annotation.class_name,
        "instance_id": str(annotation.id),
        "source_annotation_id": str(annotation.id),
        "source_annotation_version": int(annotation.version),
    }
    if mask_path is not None:
        row["mask_path"] = mask_path
    return row


async def write_binary_png_export(
    zf: ZipFile,
    *,
    prefix: str,
    project: Project,
    chunks,
    media_paths=None,
    task_items=None,
) -> int:
    media_paths = media_paths or {}
    class_ids = {
        name: index
        for index, name in enumerate(derive_classes_list(project.tool_bindings or {}))
    }
    manifest_items: list[dict[str, Any]] = []
    file_count = 0
    async for tasks, ann_by_task, dataset_items in chunks:
        for task in tasks:
            dataset_item = (
                task_items.get(task.id)
                if task_items is not None
                else dataset_items.get(task.dataset_item_id)
                if task.dataset_item_id
                else None
            )
            instances: list[dict[str, Any]] = []
            width = (
                int(dataset_item.width) if dataset_item and dataset_item.width else 0
            )
            height = (
                int(dataset_item.height) if dataset_item and dataset_item.height else 0
            )
            for annotation in sorted(
                ann_by_task.get(task.id, []), key=lambda row: str(row.id)
            ):
                geometry = annotation.geometry or {}
                if geometry.get("type") != "raster_mask":
                    continue
                rle = await load_coco_rle(geometry.get("mask") or {})
                width, height = _dimensions(task, dataset_item, rle)
                png = binary_png_bytes(rle)
                relative_path = f"masks/{task.display_id}/{annotation.id}.png"
                zf.writestr(f"{prefix}{relative_path}", png)
                instance = _manifest_instance(
                    annotation,
                    class_id=class_ids[annotation.class_name],
                    mask_path=relative_path,
                )
                instance["content_sha256"] = hashlib.sha256(png).hexdigest()
                instances.append(instance)
                file_count += 1
            if instances:
                manifest_items.append(
                    {
                        "media_path": media_paths.get(task.id, task.file_path),
                        "width": width,
                        "height": height,
                        "instances": instances,
                    }
                )
    manifest = {
        "format_id": "binary-png",
        "manifest_version": "1",
        "adapter_version": "1.0.0",
        "project_id": str(project.id),
        "items": manifest_items,
        "loss_report": [],
    }
    zf.writestr(
        f"{prefix}manifest.json",
        json.dumps(manifest, ensure_ascii=False, indent=2),
    )
    return file_count


async def write_indexed_png_export(
    zf: ZipFile,
    *,
    prefix: str,
    project: Project,
    chunks,
    overlap_policy: str,
    media_paths=None,
    task_items=None,
) -> int:
    media_paths = media_paths or {}
    class_ids = {
        name: index
        for index, name in enumerate(derive_classes_list(project.tool_bindings or {}))
    }
    manifest_items: list[dict[str, Any]] = []
    loss_report: list[dict[str, Any]] = []
    file_count = 0
    async for tasks, ann_by_task, dataset_items in chunks:
        for task in tasks:
            dataset_item = (
                task_items.get(task.id)
                if task_items is not None
                else dataset_items.get(task.dataset_item_id)
                if task.dataset_item_id
                else None
            )
            rows: list[tuple[Annotation, dict[str, Any]]] = []
            for annotation in sorted(
                ann_by_task.get(task.id, []), key=lambda row: str(row.id)
            ):
                geometry = annotation.geometry or {}
                if geometry.get("type") == "raster_mask":
                    rows.append(
                        (annotation, await load_coco_rle(geometry.get("mask") or {}))
                    )
            if not rows:
                continue
            if len(rows) > 255:
                raise ValueError(
                    "instance_id_overflow:indexed PNG supports at most 255 instances"
                )
            width, height = _dimensions(task, dataset_item, rows[0][1])
            instances: list[dict[str, Any]] = []
            composition: list[tuple[int, dict[str, Any], int]] = []
            by_pixel_id: dict[int, Annotation] = {}
            for pixel_id, (annotation, rle) in enumerate(rows, start=1):
                _dimensions(task, dataset_item, rle)
                composition.append((pixel_id, rle, int(annotation.z_order or 0)))
                by_pixel_id[pixel_id] = annotation
                instance = _manifest_instance(
                    annotation,
                    class_id=class_ids[annotation.class_name],
                )
                instance["pixel_id"] = pixel_id
                instances.append(instance)
            pixels, lost = compose_indexed_mask(
                composition, overlap_policy=overlap_policy
            )
            png = indexed_png_bytes(pixels, width=width, height=height)
            relative_path = f"masks/{task.display_id}.png"
            zf.writestr(f"{prefix}{relative_path}", png)
            item_losses: list[dict[str, Any]] = []
            for pixel_id, lost_pixels in sorted(lost.items()):
                annotation = by_pixel_id[pixel_id]
                detail = {
                    "code": "overlap_resolved",
                    "source_annotation_id": str(annotation.id),
                    "lost_pixels": lost_pixels,
                    "overlap_policy": overlap_policy,
                }
                item_losses.append(detail)
                loss_report.append(
                    {"media_path": media_paths.get(task.id, task.file_path), **detail}
                )
            manifest_items.append(
                {
                    "media_path": media_paths.get(task.id, task.file_path),
                    "width": width,
                    "height": height,
                    "mask_path": relative_path,
                    "content_sha256": hashlib.sha256(png).hexdigest(),
                    "overlap_policy": overlap_policy,
                    "instances": instances,
                    "loss_report": item_losses,
                }
            )
            file_count += 1
    manifest = {
        "format_id": "indexed-png",
        "manifest_version": "1",
        "adapter_version": "1.0.0",
        "project_id": str(project.id),
        "overlap_policy": overlap_policy,
        "items": manifest_items,
        "loss_report": loss_report,
    }
    zf.writestr(
        f"{prefix}manifest.json",
        json.dumps(manifest, ensure_ascii=False, indent=2),
    )
    return file_count


async def write_label_studio_export(
    zf: ZipFile,
    *,
    prefix: str,
    chunks,
    data_key: str,
    from_name: str,
    to_name: str,
    media_paths=None,
    task_items=None,
) -> int:
    media_paths = media_paths or {}
    output: list[dict[str, Any]] = []
    file_count = 0
    async for tasks, ann_by_task, dataset_items in chunks:
        for task in tasks:
            dataset_item = (
                task_items.get(task.id)
                if task_items is not None
                else dataset_items.get(task.dataset_item_id)
                if task.dataset_item_id
                else None
            )
            results: list[dict[str, Any]] = []
            for annotation in sorted(
                ann_by_task.get(task.id, []), key=lambda row: str(row.id)
            ):
                geometry = annotation.geometry or {}
                if geometry.get("type") != "raster_mask":
                    continue
                rle = await load_coco_rle(geometry.get("mask") or {})
                width, height = _dimensions(task, dataset_item, rle)
                results.append(
                    {
                        "id": str(annotation.id)[:8],
                        "type": "brushlabels",
                        "from_name": from_name,
                        "to_name": to_name,
                        "origin": "manual",
                        "image_rotation": 0,
                        "original_width": width,
                        "original_height": height,
                        "value": {
                            "format": "rle",
                            "rle": encode_label_studio_mask(decode_coco_rle(rle)),
                            "brushlabels": [annotation.class_name],
                        },
                    }
                )
            output.append(
                {
                    "id": task.display_id,
                    "data": {data_key: media_paths.get(task.id, task.file_path)},
                    "annotations": [{"result": results}],
                }
            )
            file_count += 1
    zf.writestr(
        f"{prefix}annotations.json",
        json.dumps(output, ensure_ascii=False, indent=2),
    )
    return file_count


async def yolo_seg_lines_with_masks(
    annotations: list[Annotation],
    class_map: dict[str, int],
    *,
    include_attributes: bool,
) -> tuple[list[str], list[dict[str, Any]]]:
    lines: list[str] = []
    attributes: list[dict[str, Any]] = []
    for annotation in annotations:
        geometry = annotation.geometry or {}
        if geometry.get("type") == "raster_mask":
            rle = await load_coco_rle(geometry.get("mask") or {})
            rings = mask_to_yolo_polygons(rle)
        elif geometry.get("type") == "polygon":
            rings = [geometry.get("points") or []]
        elif geometry.get("type") == "multi_polygon":
            rings = [
                polygon.get("points") or []
                for polygon in geometry.get("polygons") or []
                if isinstance(polygon, dict)
            ]
        else:
            continue
        class_id = class_map.get(annotation.class_name, 0)
        for ring in rings:
            if len(ring) < 3:
                continue
            values = " ".join(
                f"{float(value):.6f}" for point in ring for value in point
            )
            lines.append(f"{class_id} {values}")
            if include_attributes:
                attributes.append(annotation.attributes or {})
    return lines, attributes


async def indexed_overlap_summary(
    annotations: list[Annotation], *, overlap_policy: str
) -> tuple[dict[str, int], int]:
    rows: list[tuple[int, dict[str, Any], int]] = []
    annotation_ids: dict[int, str] = {}
    for pixel_id, annotation in enumerate(
        sorted(annotations, key=lambda row: str(row.id)), start=1
    ):
        if (annotation.geometry or {}).get("type") != "raster_mask":
            continue
        rle = await load_coco_rle(annotation.geometry["mask"])
        rows.append((pixel_id, rle, int(annotation.z_order or 0)))
        annotation_ids[pixel_id] = str(annotation.id)
    if len(rows) > 255:
        return {}, len(rows)
    _pixels, lost = compose_indexed_mask(rows, overlap_policy=overlap_policy)
    return {annotation_ids[pixel_id]: count for pixel_id, count in lost.items()}, len(
        rows
    )
