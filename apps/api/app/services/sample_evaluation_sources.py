"""Resolve current source identity without mutating frozen evaluation truth."""

import asyncio
import hashlib
import io
import uuid
from datetime import datetime, timezone

from PIL import Image

from app.services import sample_evaluation_engine as engine
from app.services.labeling_evaluation_source_contract import (
    CurrentTaskSourceRead,
    CurrentTaskSourceReader,
    CurrentTaskSourceSnapshot,
)
from app.services.storage import storage_service as storage
from app.services.training_dataset_bridge import detection_labels
from app.services.training_datasets import assert_training_authority


def read_current_media(snapshot, *, include_raw=False):
    if snapshot["file_type"] != "image":
        raise ValueError("Current task is no longer an image")
    client = storage.client
    params = {"Bucket": snapshot["source_bucket"], "Key": snapshot["source_key"]}
    head = client.head_object(**params)
    size = head.get("ContentLength")
    if type(size) is not int or not 0 < size <= engine.MAX_RAW_BYTES:
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
            image.width * image.height > engine.MAX_PIXELS
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


def classify_labels(snapshot: CurrentTaskSourceSnapshot, classes):
    if len(snapshot["annotations"]) > engine.MAX_TRUTH_PER_IMAGE:
        return None
    try:
        return hashlib.sha256(
            detection_labels(snapshot["annotations"], classes).encode()
        ).hexdigest()
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


async def resolve_source(
    db,
    run,
    attempt_id,
    ordinal,
    user,
    api_key_id=None,
    *,
    source_reader: CurrentTaskSourceReader,
):
    frozen = run.spec["samples"][ordinal]
    task_id = uuid.UUID(frozen["task_id"])
    result = {
        "evaluation_id": run.id,
        "attempt_id": attempt_id,
        "ordinal": ordinal,
        "checked_at": datetime.now(timezone.utc),
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
    await assert_training_authority(
        db, project_id=run.project_id, actor_id=user.id, api_key_id=api_key_id
    )
    source_request = CurrentTaskSourceRead(
        project_id=run.project_id,
        task_id=task_id,
        annotation_limit=engine.MAX_TRUTH_PER_IMAGE + 1,
    )
    before = await source_reader.read_current_task_source(source_request)
    await db.commit()
    if before is None:
        return {
            **result,
            "source": "deleted",
            "reason": "当前源任务已删除或移出此项目；冻结样本仍保留",
        }
    media, media_error = None, False
    try:
        media = await asyncio.to_thread(read_current_media, before)
    except Exception:
        media_error = True
    # Recheck authority and a new statement snapshot after object IO.
    await assert_training_authority(
        db, project_id=run.project_id, actor_id=user.id, api_key_id=api_key_id
    )
    after = await source_reader.read_current_task_source(source_request)
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
    label_sha = classify_labels(after, run.spec["classes"])
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
        reason="当前图片未通过读取、尺寸或解码检查"
        if media_error
        else "当前标注超出冻结检测类别或支持范围"
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
