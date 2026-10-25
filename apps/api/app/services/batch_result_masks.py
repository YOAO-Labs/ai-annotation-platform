"""Prepare durable batch results for the existing Prediction writer."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from aap_protocol_v2 import NativeMaskCandidate
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.project import Project
from app.services.project_access import get_workspace_project
from app.db.models.task import Task
from app.services.labeling_prediction_contract import ApplyPredictionResultRequest
from app.services.raster_mask_storage import (
    RasterMaskContractError,
    assert_raster_mask_write_enabled,
    build_rle_reference,
    store_generated_mask_content,
    validate_mask_geometry_for_task,
)
from app.services.labeling_core_read_relations import get_core_row, task_row


def _native_mask_candidate(value: Any) -> NativeMaskCandidate | None:
    if (
        not isinstance(value, dict)
        or value.get("type") != "mask"
        or not isinstance(value.get("value"), dict)
        or "rle" not in value["value"]
    ):
        return None
    try:
        return NativeMaskCandidate.model_validate(value)
    except ValidationError as exc:
        raise ValueError(
            "batch result contains an invalid native Mask candidate"
        ) from exc


def _has_inline_raster_mask(value: Any) -> bool:
    if isinstance(value, dict):
        if value.get("type") == "raster_mask":
            mask = value.get("mask")
            if isinstance(mask, dict) and "object_key" not in mask:
                return True
        return any(_has_inline_raster_mask(child) for child in value.values())
    if isinstance(value, list):
        return any(_has_inline_raster_mask(child) for child in value)
    return False


async def _store_rle(
    db: AsyncSession,
    task: Task,
    rle: dict[str, Any],
    references: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    expected = build_rle_reference(rle)
    object_key = str(expected["object_key"])
    await validate_mask_geometry_for_task(
        db, task, {"type": "raster_mask", "mask": rle}
    )
    existing = references.get(object_key)
    if existing is not None:
        return existing

    # This guarded producer renews the content-write epoch before storing bytes.
    # Keep its permit in the caller transaction; committing here would also commit
    # unrelated workflow state and would split the checkpoint's atomic boundary.
    reference = await store_generated_mask_content(db, rle)
    if reference.get("object_key") != object_key:
        raise ValueError("generated Mask storage changed its content address")
    references[object_key] = reference
    return reference


async def _externalize_inline_raster_masks(
    db: AsyncSession,
    task: Task,
    value: Any,
    references: dict[str, dict[str, Any]],
) -> Any:
    if isinstance(value, dict):
        if value.get("type") == "raster_mask":
            mask = value.get("mask")
            if isinstance(mask, dict) and "object_key" not in mask:
                reference = await _store_rle(db, task, mask, references)
                return {**value, "mask": reference}
        updated: dict[str, Any] | None = None
        for key, child in value.items():
            normalized = await _externalize_inline_raster_masks(
                db, task, child, references
            )
            if normalized is not child:
                if updated is None:
                    updated = dict(value)
                updated[key] = normalized
        return updated if updated is not None else value
    if isinstance(value, list):
        updated_list: list[Any] | None = None
        for index, child in enumerate(value):
            normalized = await _externalize_inline_raster_masks(
                db, task, child, references
            )
            if normalized is not child:
                if updated_list is None:
                    updated_list = list(value)
                updated_list[index] = normalized
        return updated_list if updated_list is not None else value
    return value


def _native_mask_prediction(candidate: NativeMaskCandidate, reference: dict[str, Any]):
    return {
        "type": "raster_mask",
        "class_name": candidate.value.masklabels[0],
        "geometry": {"type": "raster_mask", "mask": reference},
        "confidence": candidate.score if candidate.score is not None else 0.0,
        "tool_unit_id": "region",
        "attributes": {},
        "candidate_id": candidate.candidate_id,
    }


async def normalize_batch_prediction_masks(
    db: AsyncSession,
    requests: tuple[ApplyPredictionResultRequest, ...],
) -> tuple[ApplyPredictionResultRequest, ...]:
    """Externalize per-object inline Masks before prepared batch snapshots.

    No aggregate result budget is applied; each RLE keeps the existing storage
    contract's per-object validation and size limit.
    """
    references: dict[str, dict[str, Any]] = {}
    task_cache: dict[tuple[Any, Any], Task] = {}
    project_cache: dict[Any, Project | None] = {}
    normalized_requests: list[ApplyPredictionResultRequest] = []

    for request in requests:
        candidates = [_native_mask_candidate(item) for item in request.result]
        has_inline_mask = any(_has_inline_raster_mask(item) for item in request.result)
        if not any(candidates) and not has_inline_mask:
            normalized_requests.append(request)
            continue

        cache_key = (request.task_id, request.project_id)
        task = task_cache.get(cache_key)
        if task is None:
            task = await get_core_row(
                db, task_row, request.task_id, populate_existing=True
            )
            if task is None or task.project_id != request.project_id:
                raise RasterMaskContractError(
                    status_code=409,
                    reason="mask_task_context_missing",
                    message="mask persistence requires an existing task",
                )
            task_cache[cache_key] = task
        project = project_cache.get(request.project_id)
        if request.project_id not in project_cache:
            project = await get_workspace_project(
                db, project_id=request.project_id, populate_existing=True
            )
            project_cache[request.project_id] = project
        assert_raster_mask_write_enabled(project)

        normalized_result: list[dict[str, Any]] = []
        for item, candidate in zip(request.result, candidates, strict=True):
            if candidate is not None:
                rle = candidate.value.rle.model_dump(mode="json")
                reference = await _store_rle(db, task, rle, references)
                normalized_result.append(_native_mask_prediction(candidate, reference))
                continue
            normalized_result.append(
                await _externalize_inline_raster_masks(db, task, item, references)
            )
        normalized_requests.append(replace(request, result=normalized_result))

    return tuple(normalized_requests)
