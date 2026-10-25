from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlencode

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.annotation import Annotation
from aap_workspace.mask_configuration_values import (
    MaskCompareBaseline as _MaskCompareBaseline,
)
from app.schemas.mask_qc import MaskCompareOut
from app.services.mask_qc import compare_rles
from app.services.mask_qc.service import (
    MaskQCError,
    read_mask_annotation_revision,
    read_tracker_candidate_context,
)
from app.services.raster_mask_storage import load_coco_rle
from app.services.video_tracking import runner as tracker_runner
from app.services.video_tracks import resolve_mask_track_state_at_frame


@dataclass(frozen=True)
class ResolvedCompareSide:
    annotation_id: uuid.UUID
    annotation_version: int
    frame_index: int | None
    source: str
    state: str | None
    reference: dict
    content_path: str
    candidate_job_id: uuid.UUID | None = None
    candidate_digest: str | None = None
    candidate_instance_id: str | None = None

    def api_payload(self) -> dict:
        return {
            "annotation_id": self.annotation_id,
            "annotation_version": self.annotation_version,
            "frame_index": self.frame_index,
            "source": self.source,
            "state": self.state,
            "digest": self.reference["sha256"],
            "size": tuple(self.reference["size"]),
            "content_path": self.content_path,
            "candidate_job_id": self.candidate_job_id,
            "candidate_digest": self.candidate_digest,
            "candidate_instance_id": self.candidate_instance_id,
        }


async def geometry_for_annotation_version(
    db: AsyncSession,
    *,
    annotation: Annotation,
    annotation_version: int,
    missing_reason: str,
) -> tuple[dict, str]:
    if annotation_version == annotation.version:
        return dict(annotation.geometry or {}), str(annotation.source or "current")
    revision = await read_mask_annotation_revision(
        db,
        task_id=annotation.task_id,
        annotation_id=annotation.id,
        annotation_version=annotation_version,
    )
    expires_at = (
        datetime.fromisoformat(str(revision["expires_at"]).replace("Z", "+00:00"))
        if revision is not None
        else None
    )
    if (
        revision is None
        or expires_at is None
        or expires_at <= datetime.now(timezone.utc)
    ):
        raise MaskQCError(
            status_code=409,
            reason=missing_reason,
            message="the requested immutable Mask version is no longer retained",
            annotation_id=str(annotation.id),
            annotation_version=annotation_version,
        )
    return dict(revision["geometry"] or {}), str(revision["source_kind"])


def _reference_for_geometry(
    geometry: dict,
    *,
    frame_index: int | None,
    missing_reason: str,
) -> tuple[dict, str | None, str, int | None]:
    geometry_type = geometry.get("type")
    if geometry_type == "raster_mask":
        if frame_index is not None:
            raise MaskQCError(
                status_code=422,
                reason="mask_compare_frame_not_applicable",
                message="image Mask comparison does not accept frame_index",
            )
        reference = geometry.get("mask")
        if not isinstance(reference, dict):
            raise MaskQCError(
                status_code=409,
                reason=missing_reason,
                message="the Mask version has no immutable content reference",
            )
        return reference, None, "exact", None
    if geometry_type != "video_track_mask":
        raise MaskQCError(
            status_code=422,
            reason="mask_compare_geometry_unsupported",
            message="only Raster and video track Masks can be compared",
        )
    if frame_index is None:
        raise MaskQCError(
            status_code=422,
            reason="mask_compare_frame_required",
            message="video Mask comparison requires frame_index",
        )
    resolved = resolve_mask_track_state_at_frame(geometry, frame_index)
    reference = resolved.get("mask")
    if not isinstance(reference, dict) or not reference:
        raise MaskQCError(
            status_code=409,
            reason=missing_reason,
            message="the selected video frame has no Mask content",
            state=resolved.get("state"),
        )
    return (
        reference,
        str(resolved.get("source") or "unknown"),
        str(resolved.get("state") or "unknown"),
        frame_index,
    )


def _annotation_content_path(
    *,
    annotation_id: uuid.UUID,
    annotation_version: int,
    digest: str,
    frame_index: int | None,
) -> str:
    query = {
        "annotation_version": annotation_version,
        "digest": digest,
    }
    if frame_index is not None:
        query["frame_index"] = frame_index
    return f"/annotations/{annotation_id}/mask-compare/content?{urlencode(query)}"


async def resolve_annotation_side(
    db: AsyncSession,
    *,
    annotation: Annotation,
    annotation_version: int,
    frame_index: int | None,
    missing_reason: str,
) -> ResolvedCompareSide:
    geometry, version_source = await geometry_for_annotation_version(
        db,
        annotation=annotation,
        annotation_version=annotation_version,
        missing_reason=missing_reason,
    )
    reference, frame_source, state, resolved_frame = _reference_for_geometry(
        geometry,
        frame_index=frame_index,
        missing_reason=missing_reason,
    )
    digest = str(reference.get("sha256") or "")
    return ResolvedCompareSide(
        annotation_id=annotation.id,
        annotation_version=annotation_version,
        frame_index=resolved_frame,
        source=frame_source or version_source,
        state=state,
        reference=reference,
        content_path=_annotation_content_path(
            annotation_id=annotation.id,
            annotation_version=annotation_version,
            digest=digest,
            frame_index=resolved_frame,
        ),
    )


async def _resolve_neighbor_side(
    db: AsyncSession,
    *,
    annotation: Annotation,
    annotation_version: int,
    frame_index: int | None,
) -> ResolvedCompareSide:
    if frame_index is None:
        raise MaskQCError(
            status_code=422,
            reason="mask_compare_frame_required",
            message="neighbor comparison requires frame_index",
        )
    geometry, _source = await geometry_for_annotation_version(
        db,
        annotation=annotation,
        annotation_version=annotation_version,
        missing_reason="mask_compare_source_expired",
    )
    if geometry.get("type") != "video_track_mask":
        raise MaskQCError(
            status_code=422,
            reason="mask_compare_neighbor_not_applicable",
            message="neighbor keyframes are available only for video Masks",
        )
    current_resolution = resolve_mask_track_state_at_frame(geometry, frame_index)
    current_keyframe = current_resolution.get("resolved_from_frame")
    candidates = [
        item
        for item in geometry.get("keyframes") or []
        if int(item.get("frame_index", -1)) != current_keyframe
        and isinstance(item.get("mask"), dict)
    ]
    if not candidates:
        raise MaskQCError(
            status_code=409,
            reason="baseline_expired",
            message="no neighboring immutable Mask keyframe is available",
        )
    keyframe = min(
        candidates,
        key=lambda item: (
            abs(int(item.get("frame_index", 0)) - frame_index),
            int(item.get("frame_index", 0)),
        ),
    )
    neighbor_frame = int(keyframe["frame_index"])
    reference = keyframe["mask"]
    digest = str(reference.get("sha256") or "")
    return ResolvedCompareSide(
        annotation_id=annotation.id,
        annotation_version=annotation_version,
        frame_index=neighbor_frame,
        source=str(keyframe.get("source") or "manual"),
        state="exact",
        reference=reference,
        content_path=_annotation_content_path(
            annotation_id=annotation.id,
            annotation_version=annotation_version,
            digest=digest,
            frame_index=neighbor_frame,
        ),
    )


async def _resolve_tracker_candidate_side(
    db: AsyncSession,
    *,
    annotation: Annotation,
    annotation_version: int,
    frame_index: int | None,
    candidate_job_id: uuid.UUID | None,
    candidate_job_revision: int | None,
    candidate_digest: str | None,
    candidate_instance_id: str | None,
) -> ResolvedCompareSide:
    if (
        frame_index is None
        or candidate_job_id is None
        or candidate_job_revision is None
        or candidate_digest is None
    ):
        raise MaskQCError(
            status_code=422,
            reason="mask_compare_candidate_context_required",
            message="tracker comparison requires frame and job context",
        )
    job = await read_tracker_candidate_context(
        db, task_id=annotation.task_id, job_id=candidate_job_id
    )
    if job is None:
        raise MaskQCError(
            status_code=404,
            reason="mask_compare_candidate_not_found",
            message="tracker candidate is unavailable",
        )
    if job.revision != candidate_job_revision:
        raise MaskQCError(
            status_code=409,
            reason="mask_compare_candidate_revision_conflict",
            message="tracker candidate revision changed",
            expected=candidate_job_revision,
            actual=job.revision,
        )
    staged = dict(job.staged_result or {})
    source_ids = tracker_runner._review_source_map_ids(job, staged)
    review_state = tracker_runner._review_state(job)
    instance_annotations = dict(review_state.get("instance_annotations") or {})
    matches = []
    for raw_row in staged.get("results") or []:
        if not isinstance(raw_row, dict):
            continue
        row = tracker_runner._ensure_candidate_contract(raw_row)
        if int(row.get("frame_index", -1)) != frame_index:
            continue
        instance_id = tracker_runner._tracker_instance_key(row)
        if candidate_instance_id is not None and instance_id != candidate_instance_id:
            continue
        source_id = source_ids.get(instance_id)
        target_id = instance_annotations.get(instance_id) or source_id
        target_ids = {
            str(value)
            for value in (
                target_id,
                source_id,
                job.annotation_id,
            )
            if value is not None
        }
        geometry = row.get("geometry") or {}
        reference = geometry.get("mask")
        if str(annotation.id) not in target_ids or not isinstance(reference, dict):
            continue
        if row.get("geometry_digest") != candidate_digest:
            continue
        matches.append((row, reference))
    if len(matches) != 1:
        raise MaskQCError(
            status_code=409 if matches else 404,
            reason=(
                "mask_compare_candidate_ambiguous"
                if matches
                else "mask_compare_candidate_not_found"
            ),
            message="tracker candidate does not resolve to one immutable Mask",
        )
    row, reference = matches[0]
    digest = str(reference.get("sha256") or "")
    geometry_digest = str(row.get("geometry_digest") or "")
    return ResolvedCompareSide(
        annotation_id=annotation.id,
        annotation_version=annotation_version,
        frame_index=frame_index,
        source="tracker_candidate",
        state="candidate",
        reference=reference,
        content_path=f"/video-tracker-jobs/{job.id}/mask-content/{digest}",
        candidate_job_id=job.id,
        candidate_digest=geometry_digest,
        candidate_instance_id=tracker_runner._tracker_instance_key(row),
    )


async def build_mask_compare(
    db: AsyncSession,
    *,
    annotation: Annotation,
    annotation_version: int,
    baseline_kind: _MaskCompareBaseline,
    frame_index: int | None,
    candidate_job_id: uuid.UUID | None,
    candidate_job_revision: int | None,
    candidate_digest: str | None,
    candidate_instance_id: str | None,
) -> MaskCompareOut:
    current = await resolve_annotation_side(
        db,
        annotation=annotation,
        annotation_version=annotation_version,
        frame_index=frame_index,
        missing_reason="mask_compare_source_expired",
    )
    if baseline_kind == "previous_version":
        if annotation_version <= 1:
            raise MaskQCError(
                status_code=409,
                reason="baseline_expired",
                message="the Mask has no previous version",
            )
        baseline = await resolve_annotation_side(
            db,
            annotation=annotation,
            annotation_version=annotation_version - 1,
            frame_index=frame_index,
            missing_reason="baseline_expired",
        )
    elif baseline_kind == "neighbor_keyframe":
        baseline = await _resolve_neighbor_side(
            db,
            annotation=annotation,
            annotation_version=annotation_version,
            frame_index=frame_index,
        )
    elif baseline_kind == "tracker_candidate":
        baseline = await _resolve_tracker_candidate_side(
            db,
            annotation=annotation,
            annotation_version=annotation_version,
            frame_index=frame_index,
            candidate_job_id=candidate_job_id,
            candidate_job_revision=candidate_job_revision,
            candidate_digest=candidate_digest,
            candidate_instance_id=candidate_instance_id,
        )
    else:
        raise MaskQCError(
            status_code=409,
            reason="mask_compare_ai_candidate_not_persisted",
            message="interactive AI candidates are compared from their local immutable buffer",
        )

    if tuple(current.reference.get("size") or ()) != tuple(
        baseline.reference.get("size") or ()
    ):
        raise MaskQCError(
            status_code=409,
            reason="mask_compare_size_mismatch",
            message="Mask comparison sides have different dimensions",
        )
    try:
        current_rle = await load_coco_rle(current.reference)
        baseline_rle = await load_coco_rle(baseline.reference)
    except (KeyError, ValueError) as exc:
        raise MaskQCError(
            status_code=409,
            reason="mask_compare_content_invalid",
            message=f"Mask comparison content is invalid: {exc}",
        ) from exc
    except Exception as exc:
        raise MaskQCError(
            status_code=503,
            reason="mask_storage_unavailable",
            message="Mask comparison content is temporarily unavailable",
        ) from exc
    overlap = compare_rles(current_rle, baseline_rle)
    added = overlap.left_area_pixels - overlap.intersection_pixels
    removed = overlap.right_area_pixels - overlap.intersection_pixels
    return MaskCompareOut.model_validate(
        {
            "baseline_kind": baseline_kind,
            "current": current.api_payload(),
            "baseline": baseline.api_payload(),
            "metrics": {
                "current_area_pixels": overlap.left_area_pixels,
                "baseline_area_pixels": overlap.right_area_pixels,
                "intersection_pixels": overlap.intersection_pixels,
                "union_pixels": overlap.union_pixels,
                "changed_pixels": overlap.xor_pixels,
                "added_pixels": added,
                "removed_pixels": removed,
                "iou_numerator": overlap.intersection_pixels,
                "iou_denominator": overlap.union_pixels,
                "dice_numerator": overlap.intersection_pixels * 2,
                "dice_denominator": (
                    overlap.left_area_pixels + overlap.right_area_pixels
                ),
            },
            "loss": [],
        }
    )
