from __future__ import annotations

import hashlib
import json
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from aap_protocol_v2 import native_mask_candidate_id
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.ai_mask_accept_decision import AiMaskAcceptDecision
from app.db.models.annotation import Annotation
from app.db.models.dataset import DatasetItem
from app.db.models.ml_backend_pool import MLBackendServicePool
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.db.models.prediction import INTERACTIVE_ACCEPT_PREDICTION_SOURCE
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.user import User
from app.schemas.ai_mask import AiMaskAcceptRequest, AiMaskAcceptResponse
from app.schemas.annotation import AnnotationOut
from app.schemas.prediction import PredictionOut
from app.services.ai_mask_receipt import AiMaskReceiptError, verify_ai_mask_receipt
from app.services.annotation import AnnotationService
from app.services.annotation_evidence import record_annotation_actor
from app.services.annotation_propagation import _new_track_id
from app.services.annotation_track_identity import prepare_compact_track_identity
from app.services.audit import AuditAction, AuditService
from app.services.prediction import PredictionService
from app.services.raster_mask_storage import (
    RasterMaskContractError,
    assert_raster_mask_write_enabled,
    build_rle_reference,
    store_mask_reference_objects,
    validate_mask_geometry_for_task,
)
from app.services.task_lock import TaskLockConflictError, TaskLockService
from app.services.video_tracks import (
    remove_frame_from_outside_ranges,
    resolve_track_at_frame,
)


class AiMaskAcceptError(ValueError):
    def __init__(
        self,
        *,
        status_code: int,
        reason: str,
        message: str,
        extra: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.detail = {"reason": reason, "message": message, **(extra or {})}


def ai_mask_accept_request_digest(data: AiMaskAcceptRequest) -> str:
    payload = data.model_dump(mode="json")
    payload.pop("idempotency_key", None)
    payload["candidate"].pop("receipt", None)
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def _replay_response(decision: AiMaskAcceptDecision) -> AiMaskAcceptResponse:
    snapshot = dict(decision.response_json or {})
    snapshot["replayed"] = True
    return AiMaskAcceptResponse.model_validate(snapshot)


async def _find_decision(
    db: AsyncSession, *, task_id: uuid.UUID, idempotency_key: str
) -> AiMaskAcceptDecision | None:
    return (
        await db.execute(
            select(AiMaskAcceptDecision).where(
                AiMaskAcceptDecision.task_id == task_id,
                AiMaskAcceptDecision.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()


def _validate_replay(
    decision: AiMaskAcceptDecision,
    request_digest: str,
    current_user: User,
    *,
    access: Any = None,
) -> AiMaskAcceptResponse:
    if decision.request_digest != request_digest:
        raise AiMaskAcceptError(
            status_code=409,
            reason="idempotency_conflict",
            message="idempotency key was already used for a different request",
        )
    if decision.expires_at <= datetime.now(timezone.utc):
        raise AiMaskAcceptError(
            status_code=409,
            reason="idempotency_expired",
            message="the previous idempotent result has expired",
        )
    if (
        decision.actor_id is not None
        and decision.actor_id != current_user.id
        and not (access is not None and access.is_manager)
    ):
        raise AiMaskAcceptError(
            status_code=403,
            reason="idempotency_owner_conflict",
            message="idempotency key belongs to another actor",
        )
    return _replay_response(decision)


def _validate_receipt(
    data: AiMaskAcceptRequest,
    *,
    task_id: uuid.UUID,
    content_digest: str,
) -> dict[str, Any]:
    try:
        claims = verify_ai_mask_receipt(data.candidate.receipt)
    except AiMaskReceiptError as exc:
        raise AiMaskAcceptError(
            status_code=409,
            reason=exc.reason,
            message=str(exc),
        ) from exc

    expected = {
        "task_id": str(task_id),
        "frame_index": data.target.frame_index,
        "candidate_id": data.candidate.candidate.candidate_id,
        "candidate_index": data.candidate.candidate_index,
        "content_digest": content_digest,
        "prompt_revision": data.candidate.prompt_revision,
        "score": data.candidate.candidate.score,
        "routing": data.routing.model_dump(mode="json"),
        "inference": data.inference.model_dump(mode="json"),
        "prompt_summary": data.prompt_summary.model_dump(mode="json"),
    }
    if any(claims.get(key) != value for key, value in expected.items()):
        raise AiMaskAcceptError(
            status_code=409,
            reason="candidate_receipt_mismatch",
            message="candidate receipt does not match the accept request",
        )
    accept_target = claims.get("accept_target")
    if accept_target is not None and accept_target != data.target.model_dump(
        mode="json"
    ):
        raise AiMaskAcceptError(
            status_code=409,
            reason="candidate_receipt_mismatch",
            message="candidate receipt does not match the accept target",
        )
    prompt_source = claims.get("prompt_source")
    if prompt_source is not None:
        expected_source = {
            "source_annotation_id": (
                str(data.target.source_annotation_id)
                if data.target.source_annotation_id is not None
                else None
            ),
            "source_version": data.target.source_version,
        }
        if (
            data.target.mode != "refine"
            or not isinstance(prompt_source, dict)
            or any(
                prompt_source.get(key) != value
                for key, value in expected_source.items()
            )
        ):
            raise AiMaskAcceptError(
                status_code=409,
                reason="candidate_receipt_mismatch",
                message="Mask prompt source does not match the accept target",
            )
    return claims


def _validate_prompt_source_digest(
    claims: dict[str, Any],
    source: Annotation | None,
    *,
    frame_index: int | None,
) -> None:
    prompt_source = claims.get("prompt_source")
    if prompt_source is None:
        return
    if source is None or not isinstance(prompt_source, dict):
        raise AiMaskAcceptError(
            status_code=409,
            reason="candidate_receipt_mismatch",
            message="Mask prompt source is no longer the accept target",
        )
    geometry = source.geometry or {}
    if geometry.get("type") == "raster_mask":
        mask_reference = geometry.get("mask")
    else:
        resolved = (
            resolve_track_at_frame(geometry, frame_index)
            if frame_index is not None
            else None
        )
        mask_reference = resolved.get("mask") if isinstance(resolved, dict) else None
    current_digest = (
        mask_reference.get("sha256") if isinstance(mask_reference, dict) else None
    )
    if current_digest != prompt_source.get("source_digest"):
        raise AiMaskAcceptError(
            status_code=409,
            reason="mask_prompt_source_changed",
            message="Mask prompt source content has changed",
        )


async def _resolve_routing_lineage(
    db: AsyncSession, data: AiMaskAcceptRequest
) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    actual = await db.get(MLBackendRegistry, data.routing.backend_instance_id)
    pool = (
        await db.get(MLBackendServicePool, data.routing.backend_pool_id)
        if data.routing.backend_pool_id is not None
        else None
    )
    # Receipt 签名的是推理当时的历史路由事实。实例在候选 TTL 内摘除或换池
    # 不得使合法候选失效；当前表只决定 nullable FK 能否保留，完整 ID 始终进 meta/audit。
    return (
        actual.id if actual is not None else None,
        pool.id if pool is not None else None,
    )


def _assert_task_still_editable(
    task: Task, current_user: User, *, access: Any = None
) -> None:
    if task.status not in {"review", "completed"}:
        return
    if task.status == "review" and (
        access is not None and (access.is_manager or access.project_role == "reviewer")
    ):
        return
    raise AiMaskAcceptError(
        status_code=409,
        reason="task_locked",
        message="task is no longer editable",
        extra={"status": task.status},
    )


def _upsert_video_mask_keyframe(
    geometry: dict[str, Any], *, frame_index: int, mask_reference: dict[str, Any]
) -> dict[str, Any]:
    updated = deepcopy(geometry)
    previous = next(
        (
            keyframe
            for keyframe in updated.get("keyframes") or []
            if int(keyframe.get("frame_index", -1)) == frame_index
        ),
        None,
    )
    keyframe: dict[str, Any] = {
        "frame_index": frame_index,
        "mask": mask_reference,
        "source": "prediction",
        "occluded": False,
    }
    if previous is not None and isinstance(previous.get("attributes"), dict):
        keyframe["attributes"] = deepcopy(previous["attributes"])
    updated["keyframes"] = sorted(
        [
            deepcopy(item)
            for item in updated.get("keyframes") or []
            if int(item.get("frame_index", -1)) != frame_index
        ]
        + [keyframe],
        key=lambda item: int(item["frame_index"]),
    )
    updated["outside"] = remove_frame_from_outside_ranges(
        updated.get("outside"), frame_index
    )
    return updated


async def _load_source_for_update(
    db: AsyncSession,
    data: AiMaskAcceptRequest,
    *,
    task_id: uuid.UUID,
    expected_version: int | None,
    expected_geometry_type: str,
    receipt_claims: dict[str, Any],
    frame_index: int | None,
) -> Annotation | None:
    if data.target.mode == "create":
        if expected_version is not None:
            raise AiMaskAcceptError(
                status_code=400,
                reason="if_match_unexpected",
                message="If-Match is only valid for refine targets",
            )
        return None
    source = (
        await db.execute(
            select(Annotation)
            .where(Annotation.id == data.target.source_annotation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if source is None or not source.is_active or source.task_id != task_id:
        raise AiMaskAcceptError(
            status_code=404,
            reason="source_annotation_not_found",
            message="source annotation was not found",
        )
    if source.video_segment_id != data.video_segment_id:
        raise AiMaskAcceptError(
            status_code=422,
            reason="video_segment_mismatch",
            message="source annotation belongs to another video segment",
        )
    if source.is_locked:
        raise AiMaskAcceptError(
            status_code=409,
            reason="annotation_locked",
            message="source annotation is locked",
        )
    if (source.geometry or {}).get("type") != expected_geometry_type:
        raise AiMaskAcceptError(
            status_code=422,
            reason="target_geometry_mismatch",
            message=f"source annotation must use {expected_geometry_type} geometry",
        )
    if expected_version is None:
        raise AiMaskAcceptError(
            status_code=428,
            reason="if_match_required",
            message="refine target requires If-Match",
        )
    if data.target.source_version != expected_version:
        raise AiMaskAcceptError(
            status_code=400,
            reason="source_version_mismatch",
            message="If-Match must equal target.source_version",
        )
    _validate_prompt_source_digest(
        receipt_claims,
        source,
        frame_index=frame_index,
    )
    if source.version != expected_version:
        raise AiMaskAcceptError(
            status_code=409,
            reason="version_mismatch",
            message="source annotation version has changed",
            extra={"current_version": source.version},
        )
    return source


def _prediction_out(prediction, *, inference_time_ms: int | None) -> PredictionOut:
    return PredictionOut.model_validate(
        {
            "id": prediction.id,
            "task_id": prediction.task_id,
            "project_id": prediction.project_id,
            "ml_backend_id": prediction.ml_backend_id,
            "model_version": prediction.model_version,
            "score": prediction.score,
            "source": prediction.source,
            "tool_unit_id": prediction.tool_unit_id,
            "result": prediction.result,
            "cluster": prediction.cluster,
            "created_at": prediction.created_at,
            "inference_time_ms": inference_time_ms,
            "total_cost": None,
        }
    )


async def accept_ai_mask_candidate(
    db: AsyncSession,
    *,
    task_id: uuid.UUID,
    data: AiMaskAcceptRequest,
    current_user: User,
    request,
    expected_version: int | None,
    access: Any = None,
) -> AiMaskAcceptResponse:
    request_digest = ai_mask_accept_request_digest(data)
    candidate = data.candidate.candidate
    rle_model = candidate.value.rle
    expected_candidate_id = native_mask_candidate_id(
        rle_model,
        prompt_revision=data.candidate.prompt_revision,
        candidate_index=data.candidate.candidate_index,
    )
    if candidate.candidate_id != expected_candidate_id:
        raise AiMaskAcceptError(
            status_code=422,
            reason="invalid_mask_candidate",
            message="candidate id does not match its pixels and prompt revision",
        )
    rle = rle_model.model_dump(mode="json")
    mask_reference = build_rle_reference(rle)
    content_digest = str(mask_reference["sha256"])
    task = (
        await db.execute(
            select(Task)
            .where(Task.id == task_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if task is None:
        raise AiMaskAcceptError(
            status_code=404, reason="task_not_found", message="task was not found"
        )
    _assert_task_still_editable(task, current_user, access=access)
    from app.api.v1.tasks._shared import (
        _assert_review_adjustment_evidence,
        _resolve_task_access,
        assert_annotation_write_allowed,
    )

    if access is None:
        access = await _resolve_task_access(
            db, task, current_user, lock_membership=True
        )
    assert_annotation_write_allowed(task, access)
    await _assert_review_adjustment_evidence(db, task, current_user, access)
    try:
        await TaskLockService(db).assert_write_allowed(task_id, current_user.id)
    except TaskLockConflictError as exc:
        raise AiMaskAcceptError(
            status_code=409,
            reason="task_lock_conflict",
            message="task is locked by another user",
        ) from exc

    project = await db.get(Project, task.project_id)
    if project is None:
        raise AiMaskAcceptError(
            status_code=404, reason="task_not_found", message="task was not found"
        )
    if project.ai_interactive_enabled is False:
        raise AiMaskAcceptError(
            status_code=403,
            reason="ai_interactive_disabled",
            message="AI interactive is disabled for this project",
        )
    if (
        access is not None
        and access.project_role == "annotator"
        and task.assignee_id is not None
        and task.assignee_id != current_user.id
    ):
        raise AiMaskAcceptError(
            status_code=403,
            reason="task_assignment_conflict",
            message="task belongs to another annotator",
        )

    existing = await _find_decision(
        db, task_id=task_id, idempotency_key=data.idempotency_key
    )
    if existing is not None:
        return _validate_replay(existing, request_digest, current_user, access=access)

    receipt_claims = _validate_receipt(
        data,
        task_id=task_id,
        content_digest=content_digest,
    )
    prediction_backend_id, prediction_pool_id = await _resolve_routing_lineage(db, data)

    item = (
        await db.get(DatasetItem, task.dataset_item_id)
        if task.dataset_item_id
        else None
    )
    media_type = item.file_type if item is not None else None
    if media_type not in {"image", "video"}:
        raise AiMaskAcceptError(
            status_code=422,
            reason="mask_media_unsupported",
            message="native Mask accept requires an image or video task",
        )
    frame_index = data.target.frame_index
    if media_type == "image" and frame_index is not None:
        raise AiMaskAcceptError(
            status_code=422,
            reason="image_mask_frame_unexpected",
            message="image Mask accept cannot include frame_index",
        )
    if media_type == "video" and frame_index is None:
        raise AiMaskAcceptError(
            status_code=422,
            reason="video_mask_frame_required",
            message="video Mask accept requires frame_index",
        )

    geometry_type = "raster_mask" if media_type == "image" else "video_track_mask"
    source = await _load_source_for_update(
        db,
        data,
        task_id=task_id,
        expected_version=expected_version,
        expected_geometry_type=geometry_type,
        receipt_claims=receipt_claims,
        frame_index=frame_index,
    )
    source_version = source.version if source is not None else None
    if media_type == "image":
        assert_raster_mask_write_enabled(project)
        annotation_geometry: dict[str, Any] = {
            "type": "raster_mask",
            "mask": mask_reference,
        }
        prediction_geometry = annotation_geometry
    else:
        assert frame_index is not None
        if source is None:
            annotation_geometry = {
                "type": "video_track_mask",
                "track_id": _new_track_id(),
                "keyframes": [],
                "outside": [],
            }
        else:
            annotation_geometry = deepcopy(source.geometry)
        annotation_geometry = _upsert_video_mask_keyframe(
            annotation_geometry,
            frame_index=frame_index,
            mask_reference=mask_reference,
        )
        prediction_geometry = {
            "type": "video_track_mask",
            "track_id": annotation_geometry["track_id"],
            "keyframes": [
                keyframe
                for keyframe in annotation_geometry["keyframes"]
                if int(keyframe["frame_index"]) == frame_index
            ],
            "outside": [],
        }

    annotation_service = AnnotationService(db)
    await annotation_service._validate_class_name(
        task.project_id, "region", data.class_name
    )
    try:
        await validate_mask_geometry_for_task(db, task, annotation_geometry)
        await store_mask_reference_objects(
            db,
            annotation_geometry,
            [(mask_reference, rle)],
            task_id=task.id,
        )
    except RasterMaskContractError:
        raise
    except ValueError as exc:
        raise AiMaskAcceptError(
            status_code=422,
            reason="mask_geometry_invalid",
            message=str(exc),
        ) from exc
    except Exception as exc:
        raise RasterMaskContractError(
            status_code=503,
            reason="mask_storage_unavailable",
            message="mask object storage is unavailable",
        ) from exc

    score = candidate.score
    confidence = float(score) if score is not None else 0.0
    prediction_shape = {
        "type": geometry_type,
        "class_name": data.class_name,
        "geometry": prediction_geometry,
        "confidence": confidence,
        "tool_unit_id": "region",
        "attributes": {},
    }
    inference_time_ms = (
        int(round(data.inference.inference_time_ms))
        if data.inference.inference_time_ms is not None
        else None
    )
    prediction = await PredictionService(db).create_from_ml_result(
        task_id=task.id,
        project_id=task.project_id,
        ml_backend_id=prediction_backend_id,
        ml_backend_pool_id=prediction_pool_id,
        result=[prediction_shape],
        score=score,
        model_version=data.inference.model_version,
        inference_time_ms=inference_time_ms,
        source=INTERACTIVE_ACCEPT_PREDICTION_SOURCE,
        pipeline_extra={
            "mask_ai_accept": {
                "candidate_id": candidate.candidate_id,
                "content_digest": content_digest,
                "prompt_revision": data.candidate.prompt_revision,
                "candidate_index": data.candidate.candidate_index,
                "prompt_summary": data.prompt_summary.model_dump(mode="json"),
                "routing": data.routing.model_dump(mode="json"),
                "inference": data.inference.model_dump(mode="json"),
                "target": data.target.model_dump(mode="json"),
                "prompt_source": receipt_claims.get("prompt_source"),
            }
        },
    )

    if source is None:
        annotation = await annotation_service.create(
            task_id=task.id,
            user_id=current_user.id,
            annotation_type=geometry_type,
            tool_unit_id="region",
            class_name=data.class_name,
            geometry=annotation_geometry,
            confidence=score,
            parent_prediction_id=prediction.id,
            video_segment_id=data.video_segment_id,
        )
    else:
        geometry, track_id = prepare_compact_track_identity(
            annotation_geometry,
            source.track_id,
            reject_identity_change=True,
        )
        source.annotation_type = geometry_type
        source.tool_unit_id = "region"
        source.class_name = data.class_name
        source.geometry = geometry
        source.track_id = track_id
        source.confidence = score
        source.parent_prediction_id = prediction.id
        source.version = int(source.version or 1) + 1
        annotation = source
        await db.flush()
        # The create branch records through AnnotationService.create; the refine
        # branch mutates the source directly and must record here.
        await record_annotation_actor(db, task, current_user.id)

    await TaskLockService(db).heartbeat(task.id, current_user.id)
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.AI_MASK_CANDIDATE_ACCEPT,
        target_type="annotation",
        target_id=str(annotation.id),
        request=request,
        status_code=200,
        detail={
            "task_id": str(task.id),
            "candidate_id": candidate.candidate_id,
            "content_digest": content_digest,
            "prediction_id": str(prediction.id),
            "source_annotation_id": (
                str(data.target.source_annotation_id)
                if data.target.source_annotation_id
                else None
            ),
            "source_version": source_version,
            "result_version": annotation.version,
            "frame_index": frame_index,
            "backend_instance_id": str(data.routing.backend_instance_id),
            "backend_pool_id": (
                str(data.routing.backend_pool_id)
                if data.routing.backend_pool_id
                else None
            ),
            "model_id": data.routing.model_id,
            "prompt_family": data.prompt_summary.family,
        },
    )
    await db.flush()
    await db.refresh(prediction)
    await db.refresh(annotation)
    response = AiMaskAcceptResponse(
        prediction=_prediction_out(prediction, inference_time_ms=inference_time_ms),
        annotation=AnnotationOut.model_validate(annotation),
        source_version=source_version,
        result_version=annotation.version,
        content_digest=content_digest,
        replayed=False,
    )
    decision = AiMaskAcceptDecision(
        task_id=task.id,
        idempotency_key=data.idempotency_key,
        request_digest=request_digest,
        candidate_id=candidate.candidate_id,
        content_digest=content_digest,
        prediction_id=prediction.id,
        prediction_created_at=prediction.created_at,
        annotation_id=annotation.id,
        source_annotation_id=data.target.source_annotation_id,
        source_version=source_version,
        result_version=annotation.version,
        actor_id=current_user.id,
        response_json=response.model_dump(mode="json"),
    )
    db.add(decision)
    await db.flush()
    return response


__all__ = [
    "AiMaskAcceptError",
    "accept_ai_mask_candidate",
    "ai_mask_accept_request_digest",
]
