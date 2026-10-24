"""Resolve browser Mask locators into authorized backend prompt payloads."""

from __future__ import annotations

import re
import uuid
from typing import Any, NoReturn

from aap_protocol_v2 import MaskPromptPayload
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models.annotation import Annotation
from app.db.models.task import Task
from app.services.raster_mask_storage import (
    RasterMaskContractError,
    load_coco_rle,
    validate_mask_geometry_for_task,
)
from app.services.video_tracks import resolve_track_at_frame


class MaskPromptSourceLocator(BaseModel):
    model_config = ConfigDict(extra="forbid")

    annotation_id: uuid.UUID
    source_version: StrictInt = Field(ge=1)


def _prompt_error(
    status: int,
    reason: str,
    message: str,
    *,
    retryable: bool | None = None,
) -> NoReturn:
    detail: dict[str, Any] = {"reason": reason, "message": message}
    if retryable is not None:
        detail["retryable"] = retryable
    raise HTTPException(
        status_code=status,
        detail=detail,
    )


async def _source_annotation(db, task, raw_source):
    try:
        source = MaskPromptSourceLocator.model_validate(raw_source)
    except ValidationError:
        _prompt_error(
            422,
            "invalid_mask_prompt_source",
            "Mask prompt source locator failed schema validation",
        )

    annotation = await db.get(Annotation, source.annotation_id, populate_existing=True)
    if (
        annotation is None
        or not annotation.is_active
        or annotation.was_cancelled
        or annotation.task_id != task.id
        or annotation.project_id != task.project_id
    ):
        _prompt_error(404, "mask_prompt_source_not_found", "Mask source was not found")
    if annotation.version != source.source_version:
        _prompt_error(
            409,
            "mask_prompt_source_version_mismatch",
            "Mask source version has changed",
        )
    if annotation.is_locked:
        _prompt_error(409, "annotation_locked", "Mask source is locked")
    return annotation


def _assert_locator_context(context):
    if "mask_prompt" in context:
        _prompt_error(
            422,
            "client_mask_prompt_forbidden",
            "clients must send mask_prompt_source instead of inline Mask content",
        )


async def capture_image_mask_prompt_source(db, *, task, context):
    """Freeze image prompt metadata before original or Mask object I/O."""
    _assert_locator_context(context)
    raw_source = context.get("mask_prompt_source")
    if raw_source is None:
        return None
    annotation = await _source_annotation(db, task, raw_source)
    geometry = annotation.geometry or {}
    if geometry.get("type") != "raster_mask":
        reason = (
            "mask_prompt_frame_required"
            if geometry.get("type") in {"video_mask", "video_track_mask"}
            else "mask_prompt_geometry_unsupported"
        )
        _prompt_error(422, reason, "Image Mask prompts require a raster_mask source")
    if not settings.raster_mask_read_enabled:
        _prompt_error(
            404, "raster_mask_read_disabled", "Raster Mask reading is disabled"
        )
    mask = geometry.get("mask") or {}
    digest = mask.get("sha256") if isinstance(mask, dict) else None
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        _prompt_error(
            409, "invalid_mask_prompt_content", "Mask prompt content is invalid"
        )
    return {
        "source_annotation_id": str(annotation.id),
        "source_version": annotation.version,
        "source_digest": digest,
    }


async def resolve_authorized_mask_prompt(
    db: AsyncSession,
    *,
    task: Task,
    context: dict[str, Any],
    frame_index: int | None,
    verified_image_size: tuple[int, int] | None = None,
) -> dict[str, Any]:
    """Replace a source annotation locator with bounded, verified inline RLE.

    Image consumers may supply dimensions from their fully verified original
    Snapshot. This read-only prompt path never supplies persistence authority.
    """
    prepared = dict(context)
    _assert_locator_context(prepared)
    raw_source = prepared.pop("mask_prompt_source", None)
    if raw_source is None:
        return prepared
    annotation = await _source_annotation(db, task, raw_source)

    geometry = annotation.geometry or {}
    geometry_type = geometry.get("type")
    if geometry_type == "raster_mask":
        if frame_index is not None:
            _prompt_error(
                422,
                "mask_prompt_frame_unexpected",
                "image Mask prompts cannot include a frame",
            )
        if not settings.raster_mask_read_enabled:
            _prompt_error(
                404, "raster_mask_read_disabled", "Raster Mask reading is disabled"
            )
        mask_reference = geometry.get("mask")
    elif geometry_type == "video_mask":
        if frame_index is None:
            _prompt_error(
                422,
                "mask_prompt_frame_required",
                "video Mask prompts require a frame",
            )
        if geometry.get("frame_index") != frame_index:
            _prompt_error(
                404, "mask_prompt_outside", "Mask source is outside at this frame"
            )
        mask_reference = geometry.get("mask")
    elif geometry_type == "video_track_mask":
        if frame_index is None:
            _prompt_error(
                422,
                "mask_prompt_frame_required",
                "video Mask prompts require a frame",
            )
        resolved = resolve_track_at_frame(geometry, frame_index)
        mask_reference = resolved.get("mask") if resolved is not None else None
    else:
        _prompt_error(
            422,
            "mask_prompt_geometry_unsupported",
            "Mask prompt source must be raster_mask, video_mask, or video_track_mask",
        )
    if not isinstance(mask_reference, dict):
        _prompt_error(
            404, "mask_prompt_outside", "Mask source is outside at this frame"
        )

    try:
        if geometry_type == "raster_mask" and verified_image_size is not None:
            from app.services.raster_mask_storage import validate_image_mask_dimensions

            validate_image_mask_dimensions(geometry, *verified_image_size)
        else:
            await validate_mask_geometry_for_task(db, task, geometry)
    except (KeyError, ValueError, RasterMaskContractError):
        _prompt_error(
            409,
            "invalid_mask_prompt_content",
            "Mask prompt content is invalid",
        )
    try:
        rle = await load_coco_rle(mask_reference)
    except (KeyError, ValueError):
        _prompt_error(
            409,
            "invalid_mask_prompt_content",
            "Mask prompt content is invalid",
        )
    except Exception:
        _prompt_error(
            503,
            "mask_storage_unavailable",
            "Mask object storage is unavailable",
            retryable=True,
        )
    try:
        payload = MaskPromptPayload.model_validate(
            {
                "rle": rle,
                "source_annotation_id": str(annotation.id),
                "source_version": annotation.version,
                "source_digest": mask_reference.get("sha256"),
            }
        )
    except (KeyError, ValueError, ValidationError):
        _prompt_error(
            409,
            "invalid_mask_prompt_content",
            "Mask prompt content is invalid",
        )
    prepared["mask_prompt"] = payload.model_dump(mode="json")
    return prepared


__all__ = [
    "MaskPromptSourceLocator",
    "capture_image_mask_prompt_source",
    "resolve_authorized_mask_prompt",
]
