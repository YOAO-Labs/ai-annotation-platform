"""Resolve current source identity without mutating frozen evaluation truth."""

import hashlib
import io
import uuid

from PIL import Image
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import String, cast, func, select

from app.db.models.annotation import Annotation
from app.db.models.dataset import DatasetItem
from app.db.models.task import Task
from app.services import sample_evaluations as service
from app.services.training_dataset_bridge import detection_labels


async def current_snapshot(db, project_id, task_id, *, task="detect"):
    limited = (
        select(
            Annotation.id,
            Annotation.version,
            Annotation.class_name,
            Annotation.tool_unit_id,
            (
                Annotation.geometry
                if task == "segment"
                else func.jsonb_build_object(
                    *[
                        item
                        for key in ("type", "x", "y", "w", "h")
                        for item in (key, Annotation.geometry[key])
                    ]
                )
            ).label("geometry"),
        )
        .where(
            Annotation.task_id == Task.id,
            Annotation.is_active.is_(True),
            Annotation.was_cancelled.is_(False),
        )
        .order_by(Annotation.id)
        .limit(service.engine.MAX_TRUTH_PER_IMAGE + 1)
        .correlate(Task)
        .subquery()
    )
    annotations = (
        select(
            func.jsonb_agg(
                func.jsonb_build_object(
                    "id",
                    cast(limited.c.id, String),
                    "version",
                    limited.c.version,
                    "class_name",
                    limited.c.class_name,
                    "tool_unit_id",
                    limited.c.tool_unit_id,
                    "geometry",
                    limited.c.geometry,
                )
            )
        )
        .select_from(limited)
        .scalar_subquery()
    )
    result = (
        await db.execute(
            select(Task, DatasetItem, annotations)
            .outerjoin(DatasetItem, DatasetItem.id == Task.dataset_item_id)
            .where(Task.id == task_id, Task.project_id == project_id)
            .execution_options(populate_existing=True)
        )
    ).first()
    if not result:
        return None
    task, item, labels = result
    return {
        "task_id": str(task.id),
        "project_id": str(task.project_id),
        "batch_id": str(task.batch_id) if task.batch_id else None,
        "dataset_item_id": str(task.dataset_item_id) if task.dataset_item_id else None,
        "source_bucket": service.storage.datasets_bucket
        if task.dataset_item_id
        else service.storage.bucket,
        "source_key": task.file_path,
        "file_type": task.file_type,
        "version": task.version,
        "status": task.status,
        "submitted_at": task.submitted_at.isoformat() if task.submitted_at else None,
        "skipped_at": task.skipped_at.isoformat() if task.skipped_at else None,
        "skip_reason": task.skip_reason,
        "reviewed_at": task.reviewed_at.isoformat() if task.reviewed_at else None,
        "expected_width": item.width if item else None,
        "expected_height": item.height if item else None,
        "annotations": sorted(labels or [], key=lambda a: a["id"]),
    }


def read_current_media(snapshot, *, include_raw=False):
    if snapshot["file_type"] != "image":
        raise ValueError("Current task is no longer an image")
    client = service.storage.client
    params = {"Bucket": snapshot["source_bucket"], "Key": snapshot["source_key"]}
    head = client.head_object(**params)
    size = head.get("ContentLength")
    if type(size) is not int or not 0 < size <= service.engine.MAX_RAW_BYTES:
        raise ValueError("Current source exceeds byte budget")
    response = client.get_object(**params, IfMatch=head["ETag"])
    try:
        raw = response["Body"].read(size + 1)
    finally:
        response["Body"].close()
    if len(raw) != size or response.get("ETag") != head["ETag"]:
        raise ValueError("Current source changed during read")
    with Image.open(io.BytesIO(raw)) as image:
        if (
            image.width * image.height > service.engine.MAX_PIXELS
            or getattr(image, "n_frames", 1) != 1
        ):
            raise ValueError("Current source exceeds pixel/frame budget")
        image.load()
        orientation = image.getexif().get(274, 1)
        if type(orientation) is not int or not 1 <= orientation <= 8:
            raise ValueError("Current source orientation is invalid")
        width, height = (
            (image.height, image.width) if orientation in {5, 6, 7, 8} else image.size
        )
    if (
        snapshot["expected_width"]
        and snapshot["expected_height"]
        and (width, height) != (snapshot["expected_width"], snapshot["expected_height"])
    ):
        raise ValueError("Current source logical dimensions differ from its item")
    after = client.head_object(**params)
    if (
        after.get("ETag") != head["ETag"]
        or after.get("ContentLength") != size
        or after.get("VersionId") != head.get("VersionId")
    ):
        raise ValueError("Current source changed during verification")
    return {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "width": width,
        "height": height,
        **({"raw": raw, "size_bytes": size} if include_raw else {}),
    }


def classify_labels(snapshot, classes, *, task="detect", preserve_unsettled=False):
    if len(snapshot["annotations"]) > service.engine.MAX_TRUTH_PER_IMAGE:
        return None
    try:
        if task == "segment":
            from app.services import sample_evaluation_masks as masks
            from app.services.mask_conversion import rasterize_region_geometry
            from app.services.raster_mask_storage import load_coco_rle_sync

            rows = []
            rasterization_operations = 0
            width, height = (
                snapshot.get("expected_width"),
                snapshot.get("expected_height"),
            )
            if not width or not height or width * height > service.engine.MAX_PIXELS:
                return None
            for annotation in snapshot["annotations"]:
                if annotation["tool_unit_id"] != "region":
                    continue
                geometry = annotation["geometry"]
                if annotation["class_name"] not in classes:
                    return None
                if geometry["type"] == "raster_mask":
                    rle = load_coco_rle_sync(geometry["mask"])
                else:
                    components = (
                        geometry.get("polygons", [])
                        if geometry["type"] == "multi_polygon"
                        else [geometry]
                    )
                    vertices = sum(
                        len(component.get("points", []))
                        + sum(map(len, component.get("holes", [])))
                        for component in components
                    )
                    rasterization_operations += width * height * vertices
                    if rasterization_operations > 100_000_000:
                        return None
                    rle = rasterize_region_geometry(geometry, width, height)
                evidence = masks.mask_evidence(rle)
                if evidence["rle"]["size"] != [height, width]:
                    return None
                rows.append(
                    (classes.index(annotation["class_name"]), evidence["mask_sha256"])
                )
            return service.content_digest(sorted(rows))
        return hashlib.sha256(
            detection_labels(snapshot["annotations"], classes).encode()
        ).hexdigest()
    except (
        KeyError,
        TypeError,
        ValueError,
        OverflowError,
        BotoCoreError,
        ClientError,
    ) as exc:
        if preserve_unsettled and getattr(exc, "storage_operation_unsettled", False):
            raise
        return None


async def resolve_source(
    db, run, attempt_id, ordinal, user, api_key_id=None, *, request
):
    from app.services import sample_evaluation_originals as originals

    frozen = run.spec["samples"][ordinal]
    task_id = uuid.UUID(frozen["task_id"])
    result = {
        "evaluation_id": run.id,
        "attempt_id": attempt_id,
        "ordinal": ordinal,
        "checked_at": service.now(),
        "source": "unavailable",
        "task_id": None,
        "project_id": None,
        "batch_id": None,
        "media": "unavailable",
        "labels": "unavailable",
        "current_sha256": None,
        "current_label_sha256": None,
        "current_width": None,
        "current_height": None,
        "task_status": None,
        "reviewed": None,
        "negative_confirmed": None,
        "reason": None,
    }
    authority = await originals.authorize(db, run.project_id, user, request, api_key_id)
    kwargs = {"task": "segment"} if run.spec.get("schema") == 2 else {}
    before = await current_snapshot(db, run.project_id, task_id, **kwargs)
    await db.commit()
    await originals.recheck_authority(db, authority)
    if before is None:
        return {
            **result,
            "source": "deleted",
            "reason": "当前源任务已删除或移出此项目；冻结样本仍保留",
        }
    async with originals.current_media(db, authority, before) as (source, media):
        # The raw stage/permit remains owned through label work and its final
        # checks. Its context closes every real handle before publishing JSON.
        candidate = await _compare_current(
            db, run, task_id, frozen, result, before, source, media, authority, kwargs
        )
    # A slow physical close can outlive the label comparison. Do not publish a
    # receipt for annotations or task state that changed while it was closing.
    final = await current_snapshot(db, run.project_id, task_id, **kwargs)
    await db.commit()
    await originals.recheck_authority(db, authority)
    if final is None:
        return {
            **result,
            "source": "deleted",
            "reason": "核验期间源任务已删除或移出此项目",
        }
    if final != before:
        return {
            **result,
            "reason": "核验期间源任务引用、状态或标注发生变化，请重新核验",
        }
    if source is not None:
        await originals.recheck_source(db, authority, source)
    return candidate


async def _compare_current(
    db, run, task_id, frozen, result, before, source, media, authority, kwargs
):
    from app.services import sample_evaluation_originals as originals

    result = {**result}
    await originals.recheck_authority(db, authority)
    after = await current_snapshot(db, run.project_id, task_id, **kwargs)
    await db.commit()
    if after is None:
        return {
            **result,
            "source": "deleted",
            "reason": "核验期间源任务已删除或移出此项目",
        }
    if before != after:
        return {
            **result,
            "reason": "核验期间源任务引用、状态或标注发生变化，请重新核验",
        }
    if source is not None:
        await originals.recheck_source(db, authority, source)
    label_snapshot = after
    if kwargs and media:
        label_snapshot = {
            **after,
            "expected_width": media["width"],
            "expected_height": media["height"],
        }
    label_sha = await originals.bounded_thread(
        classify_labels,
        label_snapshot,
        run.spec["classes"],
        preserve_unsettled=True,
        **kwargs,
    )
    await originals.recheck_authority(db, authority)
    if source is not None:
        await originals.recheck_source(db, authority, source)
    reviewed = after["status"] == "completed" and after["reviewed_at"] is not None
    negative = (
        not after["annotations"]
        and reviewed
        and after["skipped_at"] is not None
        and after["skipped_at"] == after["submitted_at"]
        and after["skip_reason"] == "no_target"
    )
    result.update(
        source="found",
        task_id=task_id,
        project_id=run.project_id,
        batch_id=after["batch_id"],
        task_status=after["status"],
        reviewed=reviewed,
        negative_confirmed=negative,
        labels="unsupported"
        if label_sha is None
        else "same"
        if label_sha == frozen["label_sha256"]
        else "changed",
        current_label_sha256=label_sha,
        reason="当前图片未通过原件归属、内容、尺寸或解码检查"
        if media is None
        else "当前标注超出冻结类别或支持范围"
        if label_sha is None
        else None,
    )
    if media:
        result.update(
            media="same" if media["sha256"] == frozen["sha256"] else "changed",
            current_sha256=media["sha256"],
            current_width=media["width"],
            current_height=media["height"],
        )
    return result
