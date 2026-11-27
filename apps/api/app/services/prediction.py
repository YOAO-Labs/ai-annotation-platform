from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import make_transient_to_detached
from sqlalchemy.orm.attributes import set_committed_value

from yoao_assets_video_inputs.evidence import VideoInputEvidence

from app.db.enums import UserRole
from app.services.project_access import get_workspace_project
from app.db.models.prediction import (
    INTERACTIVE_ACCEPT_PREDICTION_SOURCE,
    Prediction,
    FailedPrediction,
)
from app.db.models.task import Task
from app.services.annotation_propagation import _new_track_id
from app.services.content_snapshot import ContentError
from app.services.labeling_write_scope import LabelingWriteScope
from app.services.labeling_write_guard import lock_labeling_write
from app.services.labeling_prediction_contract import (
    ApplyPredictionResultError,
    ApplyPredictionResultReceipt,
    ApplyPredictionResultRequest,
    BatchResultPredictionIdentity,
)
from app.services.prediction_shapes import (
    TOOL_UNIT_IDS as TOOL_UNIT_IDS,
    derive_tool_unit_from_ls_type as derive_tool_unit_from_ls_type,
    derive_tool_unit_from_result as derive_tool_unit_from_result,
    to_internal_shape as to_internal_shape,
    to_video_bbox_result as to_video_bbox_result,
)
from app.services.mask_gc import mask_fence_error
from app.services.test_seed_access import assert_isolated_test_seed_database
from app.services.project_access import resolve_project_access
from app.services.identity_account_lock_data import IdentityAccountLockData
from app.services.raster_mask_storage import RasterMaskContractError
from app.services.raster_mask_validation import (
    prepare_mask_payload_for_write,
    preflight_mask_payload_for_write,
)
from yoao_labeling.prediction_record_contract import (
    InsertPredictionResult,
    InsertPredictionResultData,
    InsertTestSeedPrediction,
    InsertTestSeedPredictionData,
    PREDICTION_RECORDS_PROTOCOL,
)
from app.services.labeling_prediction_records import (
    create_prediction_record_failed_state,
    delete_prediction_records_for_batch,
    delete_prediction_records_for_tasks,
    prediction_record_command,
    set_failed_prediction_dismissed,
)
from app.services.labeling_core_read_relations import (
    get_core_row,
    prediction_meta_row,
    prediction_row,
    task_row,
)

logger = logging.getLogger("app.services.prediction")


def _apply_prediction_error(
    exc: HTTPException | RasterMaskContractError,
) -> ApplyPredictionResultError:
    detail = exc.detail
    if isinstance(detail, dict) and isinstance(detail.get("reason"), str):
        code = detail["reason"]
    else:
        code = {
            401: "labeling_authentication_required",
            403: "labeling_permission_changed",
            404: "labeling_target_not_found",
        }.get(exc.status_code, "labeling_write_rejected")
    return ApplyPredictionResultError(
        code=code,
        status_code=exc.status_code,
        detail=detail,
        headers=getattr(exc, "headers", None),
    )


def _validate_video_write_request(
    request: ApplyPredictionResultRequest,
) -> VideoInputEvidence | None:
    """Keep retained video evidence typed and execution identity server-owned."""
    extra = request.pipeline_extra
    if extra is not None and not isinstance(extra, dict):
        raise ValueError("prediction pipeline metadata must be an object")
    reserved = {"accepted_video_input", "accepted_video_execution_job_id"}
    if request.accepted_video_input is None:
        if extra and reserved.intersection(extra):
            raise ValueError("retained video metadata requires typed evidence")
        return None
    evidence = request.accepted_video_input
    scope = request.write_scope
    if (
        type(evidence) is not VideoInputEvidence
        or evidence.task_id != request.task_id
        or evidence.project_id != request.project_id
        or evidence.task_id != scope.task_id
        or evidence.project_id != scope.project_id
        or scope.job_id is None
        or scope.accepted_input_id is not None
        or request.accepted_input_job_id is not None
        or scope.operation not in {"preannotation", "prediction_retry"}
        or (extra is not None and reserved.intersection(extra))
    ):
        raise ValueError("retained video evidence does not match prediction scope")
    if any(
        not isinstance(item, dict)
        or not isinstance(item.get("geometry"), dict)
        or item["geometry"].get("type") != "video_bbox"
        or type(item["geometry"].get("frame_index")) is not int
        or item["geometry"]["frame_index"] != evidence.entry.frame_index
        for item in request.result
    ):
        raise ValueError("retained video result changed its selected frame")
    return evidence


def _video_prediction_receipt(prediction: Prediction) -> ApplyPredictionResultReceipt:
    return ApplyPredictionResultReceipt(
        id=prediction.id,
        created_at=prediction.created_at,
        tool_unit_id=prediction.tool_unit_id,
    )


async def assert_can_manage_failed_prediction(
    db: AsyncSession, fp: FailedPrediction, actor_id: uuid.UUID
) -> None:
    """Authorize retry, dismissal and restoration against current project management."""
    actor = await IdentityAccountLockData(db).read_actor_status(actor_id)
    if actor is None:
        raise HTTPException(
            status_code=403,
            detail="仅项目负责人或超级管理员可管理失败预测",
        )
    if actor.role == UserRole.SUPER_ADMIN.value:
        actor = await IdentityAccountLockData(db).read_actor_status(actor_id)
        if actor is None:
            raise HTTPException(
                status_code=403,
                detail="仅项目负责人或超级管理员可管理失败预测",
            )
        if actor.is_active and actor.role == UserRole.SUPER_ADMIN.value:
            return
    project = await get_workspace_project(db, project_id=fp.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    access = await resolve_project_access(db, actor_id=actor.id, project=project)
    if access.is_manager:
        return
    raise HTTPException(
        status_code=403, detail="仅项目负责人或超级管理员可管理失败预测"
    )


def cancelled_batch_result(payload: dict) -> dict:
    total_tasks = _payload_int(payload, "total_tasks") or 0
    return {
        "success_count": 0,
        "failed_count": 0,
        "done_count": 0,
        "skipped_count": total_tasks,
        "cancelled_at_index": 0,
    }


def _payload_int(payload: dict, key: str) -> int | None:
    value = payload.get(key)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _remap_track_ids(result: list[dict]) -> list[dict]:
    """检测式追踪 result item 的原生 int ``track_id`` → ``trk_<uuid>`` (ingestion
    一次性映射)。

    必须在 **worker 存 ``prediction.result`` 之前**做, 而非读路径 ``to_internal_shape`` ——
    后者每次 read 都跑, 在其中 ``uuid4()`` 会导致轨迹身份逐次漂移。原 int 记进
    ``semantic_label`` (如 ``car_3``, 供跨 task Re-ID 心智)。非 ``video_track_bbox`` item 原样;
    已是 ``trk_`` 字符串 (幂等 / 重入) 不重映射。返回新列表, 不改传入对象。
    """
    out: list[dict] = []
    for item in result:
        if not isinstance(item, dict) or item.get("type") != "video_track_bbox":
            out.append(item)
            continue
        raw_tid = item.get("track_id")
        if isinstance(raw_tid, str) and raw_tid.startswith("trk_"):
            out.append(item)
            continue
        new_item = dict(item)
        new_item["track_id"] = _new_track_id()
        if item.get("semantic_label") is None and raw_tid is not None:
            cls = item.get("class_name") or "obj"
            new_item["semantic_label"] = f"{cls}_{raw_tid}"
        out.append(new_item)
    return out


def remap_prediction_track_ids(result: list[dict]) -> list[dict]:
    """Return the stable stored form so a retried result bundle stays identical."""
    return _remap_track_ids(result)


def _prediction_result_for_write(request: ApplyPredictionResultRequest) -> list[dict]:
    identity = request.batch_result_identity
    if identity is None:
        return _remap_track_ids(request.result)
    scope = request.write_scope
    if (
        type(identity) is not BatchResultPredictionIdentity
        or scope.operation not in {"preannotation", "prediction_retry"}
        or scope.job_id is None
        or not scope.job_id_known
        or (
            request.accepted_video_input is not None
            and scope.operation != "prediction_retry"
        )
    ):
        raise ValueError(
            "stable Prediction identity requires a durable batch or retry result"
        )
    return request.result


def _portable_mask_meta(request: ApplyPredictionResultRequest) -> dict | None:
    meta = request.token_meta
    return meta if (meta or {}).get("mask_objects") is not None else None


def prediction_write_facts(request: ApplyPredictionResultRequest, result: list[dict]):
    """Exactly the frozen write facts the native qualification digest covers."""
    return dict(
        task_id=request.task_id,
        project_id=request.project_id,
        ml_backend_id=request.ml_backend_id,
        result=result,
        score=request.score,
        model_version=request.model_version,
        inference_time_ms=request.inference_time_ms,
        token_meta=request.token_meta,
        source=request.source,
        pipeline_extra=request.pipeline_extra,
        ml_backend_pool_id=request.ml_backend_pool_id,
        model_provenance=request.model_provenance,
    )


async def _verify_input_provenance(
    db, request: ApplyPredictionResultRequest, result: list[dict], task
) -> tuple[dict, dict | None, dict | None]:
    """Validate the input owner's receipt in the already-locked write transaction."""
    original = request.original_receipt
    reference = request.reference_receipt
    native = request.background_native_reference_receipt
    video_input = request.video_input_receipt
    video_result = request.video_result_receipt
    from app.services.content_snapshot import ContentError

    from app.services.reference_keys import is_reference

    repair_original = request.repair_original_receipt
    repair_reference = request.repair_reference_receipt
    reference_task = is_reference(task.file_path)
    if task.file_type == "image" and not reference_task:
        if (
            request.source == INTERACTIVE_ACCEPT_PREDICTION_SOURCE
            and request.native_original_receipt is None
        ):
            raise ContentError("prediction_requires_original_provenance")
        if request.source == "mask_repair" and repair_original is None:
            raise ContentError("repair_original_requires_new_plan")
    if (
        task.file_type == "video"
        and request.source == "ml_backend"
        and result
        and video_input is None
    ):
        from types import SimpleNamespace
        from app.services.video_prediction_acceptance import requires_qualification

        if requires_qualification(
            SimpleNamespace(source=request.source, result=result)
        ):
            raise ContentError("prediction_video_requires_new_request")
    if repair_original is not None or repair_reference is not None:
        if (
            (repair_original is not None and repair_reference is not None)
            or any(
                receipt is not None
                for receipt in (
                    original,
                    reference,
                    native,
                    video_input,
                    video_result,
                    request.native_original_receipt,
                    request.native_reference_receipt,
                )
            )
            or request.source != "mask_repair"
            or request.accepted_video_input is not None
            or (request.token_meta or {}).get("mask_objects") is not None
            or len(result) != 1
            or (result[0].get("geometry") or {}).get("type") != "raster_mask"
        ):
            raise ContentError(
                "repair_original_receipt_invalid"
                if repair_original is not None
                else "repair_reference_receipt_invalid"
            )
        if repair_original is not None:
            from app.services.managed_mask_repair import validate_receipt

            receipt, evidence_key = repair_original, "original_input"
        else:
            from app.services.reference_mask_repair import validate_receipt

            receipt, evidence_key = repair_reference, "reference_input"
        evidence = await validate_receipt(
            db,
            request.task_id,
            request.write_scope.actor_id,
            receipt,
            "prediction",
            result,
            project_id=request.project_id,
        )
        return {evidence_key: evidence}, None, None
    interactive_original = request.native_original_receipt
    interactive_reference = request.native_reference_receipt
    if interactive_original is not None or interactive_reference is not None:
        if (
            (interactive_original is not None and interactive_reference is not None)
            or any(
                receipt is not None
                for receipt in (original, reference, native, video_input, video_result)
            )
            or request.source != INTERACTIVE_ACCEPT_PREDICTION_SOURCE
            or request.accepted_video_input is not None
            or (request.token_meta or {}).get("mask_objects") is not None
        ):
            raise ContentError("prediction_native_receipt_invalid")
        if interactive_original is not None:
            from app.services.managed_native_inference import validate_native_receipt

            receipt = interactive_original
            evidence_key = "original_input"
        else:
            from app.services.reference_inference import validate_native_receipt

            receipt = interactive_reference
            evidence_key = "reference_input"
        evidence = await validate_native_receipt(
            db,
            request.task_id,
            request.project_id,
            receipt,
            actor_id=request.write_scope.actor_id,
            result=result,
        )
        return {evidence_key: evidence}, None, None

    if video_input is not None or video_result is not None:
        if (
            video_input is None
            or video_result is None
            or getattr(video_result, "anchor", None) is not video_input
            or any(receipt is not None for receipt in (original, reference, native))
            or request.source != "ml_backend"
            or request.accepted_video_input is not None
        ):
            raise ContentError("prediction_video_receipt_invalid")
        from app.services.video_prediction_references import (
            public_provenance,
            validate_result_receipt,
        )

        evidence = await validate_result_receipt(
            db,
            request.task_id,
            request.project_id,
            video_result,
            prediction_write_facts(request, result),
        )
        return (
            {"original_video_input": public_provenance(evidence)},
            None,
            evidence,
        )
    if original is not None:
        if reference is not None or native is not None:
            raise ContentError("prediction_original_receipt_invalid")
        if request.source != "ml_backend" or request.accepted_video_input is not None:
            raise ContentError("prediction_original_receipt_invalid")
        from app.services.prediction_originals import validate_receipt

        evidence = await validate_receipt(
            db, request.task_id, request.project_id, original
        )
        return {"original_input": evidence}, None, None
    if native is not None:
        if reference is None or request.accepted_video_input is not None:
            raise ContentError("background_native_reference_receipt_invalid")
        if request.source != "ml_backend":
            raise ContentError("background_native_reference_receipt_invalid")
        from app.services.reference_native_predictions import validate_receipt

        evidence, qualification = await validate_receipt(
            db,
            request.task_id,
            request.project_id,
            native,
            prediction_write_facts(request, result),
        )
        return {"reference_input": evidence}, qualification, None
    if reference is not None:
        if request.source != "ml_backend" or request.accepted_video_input is not None:
            raise ContentError("prediction_reference_receipt_invalid")
        from app.services.prediction_references import validate_receipt

        from app.services.raster_mask_storage import collect_mask_geometries

        if (
            (request.token_meta or {}).get("mask_objects") is not None
            or collect_mask_geometries(result)
            or any(shape.get("type") == "brushlabels" for shape in result)
        ):
            raise ContentError("reference_prediction_geometry_not_supported")
        evidence = await validate_receipt(
            db, request.task_id, request.project_id, reference
        )
        return {"reference_input": evidence}, None, None
    if reference_task:
        raise ContentError("reference_operation_not_supported", 409)
    return {}, None, None


async def _preflight_background_native_receipt(
    db, request: ApplyPredictionResultRequest, result: list[dict]
) -> None:
    native = request.background_native_reference_receipt
    if native is None:
        return
    if (
        request.reference_receipt is None
        or request.accepted_video_input is not None
        or request.source != "ml_backend"
    ):
        raise ContentError("background_native_reference_receipt_invalid")
    from app.services.reference_native_predictions import validate_receipt

    await validate_receipt(
        db,
        request.task_id,
        request.project_id,
        native,
        prediction_write_facts(request, result),
    )


class PredictionService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _existing_video_prediction(
        self,
        *,
        task_id: uuid.UUID,
        execution_job_id: uuid.UUID,
        evidence: VideoInputEvidence,
    ) -> Prediction | None:
        """Find the completed frame result for this exact execution and plan entry."""
        statement = (
            select(prediction_row)
            .join(
                prediction_meta_row,
                prediction_meta_row.prediction_id == prediction_row.id,
            )
            .where(
                prediction_row.task_id == task_id,
                prediction_meta_row.extra["accepted_video_execution_job_id"].as_string()
                == str(execution_job_id),
                prediction_meta_row.extra["accepted_video_input"][
                    "input_id"
                ].as_string()
                == str(evidence.input_id),
                prediction_meta_row.extra["accepted_video_input"][
                    "plan_digest"
                ].as_string()
                == evidence.plan_digest,
                prediction_meta_row.extra["accepted_video_input"]["entry"][
                    "ordinal"
                ].as_string()
                == str(evidence.entry.ordinal),
            )
            .order_by(prediction_row.created_at.asc())
            .limit(1)
        )
        return (await self.db.execute(statement)).scalars().first()

    async def delete_for_tasks(self, task_ids: list[uuid.UUID]) -> dict[str, int]:
        data = await delete_prediction_records_for_tasks(self.db, task_ids=task_ids)
        return {
            "predictions": data.predictions,
            "failed_predictions": data.failed_predictions,
        }

    async def delete_for_batch(self, batch_id: uuid.UUID) -> dict[str, int]:
        data = await delete_prediction_records_for_batch(self.db, batch_id=batch_id)
        return {
            "predictions": data.predictions,
            "failed_predictions": data.failed_predictions,
        }

    async def apply_prediction_result(
        self, request: ApplyPredictionResultRequest
    ) -> ApplyPredictionResultReceipt:
        write_scope = request.write_scope
        accepted_video_input = _validate_video_write_request(request)
        if (
            write_scope.task_id != request.task_id
            or write_scope.project_id != request.project_id
        ):
            raise ValueError("prediction write scope does not match its target")
        result = _prediction_result_for_write(request)
        preflight_task = await get_core_row(
            self.db, task_row, request.task_id, populate_existing=True
        )
        try:
            await _preflight_background_native_receipt(self.db, request, result)
            verified_content = await preflight_mask_payload_for_write(
                self.db,
                preflight_task,
                result,
                portable_mask_meta=_portable_mask_meta(request),
                native_original_receipt=request.native_original_receipt,
                repair_original_receipt=request.repair_original_receipt,
                video_result_receipt=request.video_result_receipt,
            )
            task, _actor, _access = await lock_labeling_write(
                self.db,
                write_scope,
                operations=(
                    "interactive_frame_prediction",
                    "interactive_mask_accept",
                    "mask_repair",
                    "preannotation",
                    "prediction_import",
                    "prediction_retry",
                ),
                accepted_input_job_id=request.accepted_input_job_id,
                accepted_video_input=accepted_video_input,
            )
        except RasterMaskContractError as exc:
            raise _apply_prediction_error(exc) from exc
        except HTTPException as exc:
            raise _apply_prediction_error(exc) from exc
        except ContentError as exc:
            raise ApplyPredictionResultError(
                code=exc.code,
                status_code=exc.status,
                detail={"code": exc.code},
            ) from None
        if accepted_video_input is not None:
            assert write_scope.job_id is not None
            existing = await self._existing_video_prediction(
                task_id=task.id,
                execution_job_id=write_scope.job_id,
                evidence=accepted_video_input,
            )
            if existing is not None:
                return _video_prediction_receipt(existing)
        try:
            (
                input_evidence,
                native_qualification,
                video_evidence,
            ) = await _verify_input_provenance(self.db, request, result, task)
        except ContentError as exc:
            raise ApplyPredictionResultError(
                code=exc.code,
                status_code=exc.status,
                detail={"code": exc.code},
            ) from None
        try:
            await prepare_mask_payload_for_write(
                self.db,
                task,
                result,
                verified_content=verified_content,
                require_verified_content=True,
                portable_mask_meta=_portable_mask_meta(request),
                native_original_receipt=request.native_original_receipt,
                repair_original_receipt=request.repair_original_receipt,
                video_result_receipt=request.video_result_receipt,
            )
        except RasterMaskContractError as exc:
            raise _apply_prediction_error(exc) from exc
        except ContentError as exc:
            raise ApplyPredictionResultError(
                code=exc.code,
                status_code=exc.status,
                detail={"code": exc.code},
            ) from None
        return await self._persist_prediction_result(
            request,
            task,
            result,
            input_evidence=input_evidence,
            native_qualification=native_qualification,
            video_evidence=video_evidence,
        )

    async def apply_prediction_result_bundle(
        self, requests: tuple[ApplyPredictionResultRequest, ...]
    ) -> tuple[ApplyPredictionResultReceipt, ...]:
        """Verify mask content before locks, then persist the entire Task bundle atomically."""
        if not requests:
            return ()
        first = requests[0]
        scope = first.write_scope
        accepted_video_input = _validate_video_write_request(first)
        if any(
            request.task_id != first.task_id
            or request.project_id != first.project_id
            or request.write_scope != scope
            or request.accepted_input_job_id != first.accepted_input_job_id
            or _validate_video_write_request(request) != accepted_video_input
            for request in requests
        ):
            raise ValueError("prediction result bundle must share one Task write scope")
        if scope.task_id != first.task_id or scope.project_id != first.project_id:
            raise ValueError("prediction write scope does not match its target")
        if accepted_video_input is not None and len(requests) != 1:
            raise ValueError("retained video frames must be written individually")
        preflight_task = await get_core_row(
            self.db, task_row, first.task_id, populate_existing=True
        )
        normalized_results = [
            _prediction_result_for_write(request) for request in requests
        ]
        verified_contents = []
        try:
            for request, result in zip(requests, normalized_results, strict=True):
                await _preflight_background_native_receipt(self.db, request, result)
            for request, result in zip(requests, normalized_results, strict=True):
                verified_contents.append(
                    await preflight_mask_payload_for_write(
                        self.db,
                        preflight_task,
                        result,
                        portable_mask_meta=_portable_mask_meta(request),
                        native_original_receipt=request.native_original_receipt,
                        repair_original_receipt=request.repair_original_receipt,
                        video_result_receipt=request.video_result_receipt,
                    )
                )
            task, _actor, _access = await lock_labeling_write(
                self.db,
                scope,
                operations=(
                    "interactive_frame_prediction",
                    "interactive_mask_accept",
                    "mask_repair",
                    "preannotation",
                    "prediction_import",
                    "prediction_retry",
                ),
                accepted_input_job_id=first.accepted_input_job_id,
                accepted_video_input=accepted_video_input,
            )
        except RasterMaskContractError as exc:
            raise _apply_prediction_error(exc) from exc
        except HTTPException as exc:
            raise _apply_prediction_error(exc) from exc
        except ContentError as exc:
            raise ApplyPredictionResultError(
                code=exc.code,
                status_code=exc.status,
                detail={"code": exc.code},
            ) from None

        if accepted_video_input is not None:
            assert scope.job_id is not None
            existing = await self._existing_video_prediction(
                task_id=task.id,
                execution_job_id=scope.job_id,
                evidence=accepted_video_input,
            )
            if existing is not None:
                return (_video_prediction_receipt(existing),)
        provenance = []
        try:
            for request, result in zip(requests, normalized_results, strict=True):
                provenance.append(
                    await _verify_input_provenance(self.db, request, result, task)
                )
        except ContentError as exc:
            raise ApplyPredictionResultError(
                code=exc.code,
                status_code=exc.status,
                detail={"code": exc.code},
            ) from None
        try:
            for request, result, verified_content in zip(
                requests, normalized_results, verified_contents, strict=True
            ):
                await prepare_mask_payload_for_write(
                    self.db,
                    task,
                    result,
                    verified_content=verified_content,
                    require_verified_content=True,
                    portable_mask_meta=_portable_mask_meta(request),
                    native_original_receipt=request.native_original_receipt,
                    repair_original_receipt=request.repair_original_receipt,
                    video_result_receipt=request.video_result_receipt,
                )
        except RasterMaskContractError as exc:
            raise _apply_prediction_error(exc) from exc
        except ContentError as exc:
            raise ApplyPredictionResultError(
                code=exc.code,
                status_code=exc.status,
                detail={"code": exc.code},
            ) from None

        receipts = []
        for request, result, (
            input_evidence,
            native_qualification,
            video_evidence,
        ) in zip(requests, normalized_results, provenance, strict=True):
            receipts.append(
                await self._persist_prediction_result(
                    request,
                    task,
                    result,
                    input_evidence=input_evidence,
                    native_qualification=native_qualification,
                    video_evidence=video_evidence,
                )
            )
        return tuple(receipts)

    async def _persist_prediction_result(
        self,
        request: ApplyPredictionResultRequest,
        task: Task,
        result: list[dict],
        *,
        input_evidence: dict | None = None,
        native_qualification: dict | None = None,
        video_evidence: dict | None = None,
    ) -> ApplyPredictionResultReceipt:
        prediction_id = (
            request.batch_result_identity.prediction_id
            if request.batch_result_identity is not None
            else uuid.uuid4()
        )
        token_meta = request.token_meta or {}
        extra = {
            **{
                key: value
                for key, value in (request.pipeline_extra or {}).items()
                if key
                not in {
                    "reference_input",
                    "reference_execution",
                    "reference_native",
                    "original_input",
                    "original_video_input",
                    "video_prediction_proof",
                }
            },
            **(
                {"trained_model": request.model_provenance}
                if request.model_provenance
                else {}
            ),
            **(
                {"inference_runtime": token_meta["inference_runtime"]}
                if request.model_provenance and "inference_runtime" in token_meta
                else {}
            ),
            **(
                {"segmentation": token_meta["segmentation"]}
                if "segmentation" in token_meta
                else {}
            ),
            **(
                {
                    "accepted_video_input": request.accepted_video_input.model_dump(
                        mode="json", by_alias=True
                    ),
                    "accepted_video_execution_job_id": str(request.write_scope.job_id),
                }
                if request.accepted_video_input is not None
                else {}
            ),
        }
        if input_evidence:
            extra = {**extra, **input_evidence}
        if native_qualification is not None:
            if request.batch_result_identity is None:
                raise ValueError(
                    "native qualification requires a frozen prediction identity"
                )
            from app.services.reference_native_predictions import (
                PROOF_KEY,
                persisted_qualification,
            )

            pending = Prediction(
                id=prediction_id,
                created_at=request.batch_result_identity.prediction_created_at,
                task_id=request.task_id,
                project_id=request.project_id,
                ml_backend_id=request.ml_backend_id,
                ml_backend_pool_id=request.ml_backend_pool_id,
                model_version=request.model_version,
                score=request.score,
                tool_unit_id=derive_tool_unit_from_result(result),
                result=result,
                source=request.source,
                rejected_shape_indexes=[],
            )
            extra = {
                **extra,
                PROOF_KEY: persisted_qualification(
                    pending, extra, native_qualification
                ),
            }
        video_created_at = None
        if video_evidence is not None:
            from app.services.video_prediction_acceptance import (
                PROOF_KEY as VIDEO_PROOF_KEY,
                persisted_qualification as qualify_video_prediction,
            )

            video_created_at = (
                request.batch_result_identity.prediction_created_at
                if request.batch_result_identity is not None
                else datetime.now(timezone.utc)
            )
            pending = Prediction(
                id=prediction_id,
                created_at=video_created_at,
                task_id=request.task_id,
                project_id=request.project_id,
                ml_backend_id=request.ml_backend_id,
                ml_backend_pool_id=request.ml_backend_pool_id,
                model_version=request.model_version,
                score=request.score,
                tool_unit_id=derive_tool_unit_from_result(result),
                result=result,
                source=request.source,
                rejected_shape_indexes=[],
            )
            extra = {
                **extra,
                VIDEO_PROOF_KEY: qualify_video_prediction(
                    pending, extra, video_evidence
                ),
            }
        command = InsertPredictionResult(
            schema=PREDICTION_RECORDS_PROTOCOL,
            operation="insert_result",
            prediction_id=prediction_id,
            prediction_created_at=(
                request.batch_result_identity.prediction_created_at
                if request.batch_result_identity is not None
                else video_created_at
            ),
            prediction_meta_id=uuid.uuid4(),
            task_id=request.task_id,
            project_id=request.project_id,
            ml_backend_id=request.ml_backend_id,
            ml_backend_pool_id=request.ml_backend_pool_id,
            model_version=request.model_version,
            score=request.score,
            tool_unit_id=derive_tool_unit_from_result(result),
            result=result,
            source=request.source,
            inference_time_ms=request.inference_time_ms,
            prompt_tokens=token_meta.get("prompt_tokens"),
            completion_tokens=token_meta.get("completion_tokens"),
            total_tokens=token_meta.get("total_tokens"),
            prompt_cost=token_meta.get("prompt_cost"),
            completion_cost=token_meta.get("completion_cost"),
            total_cost=token_meta.get("total_cost"),
            extra=extra,
        )
        try:
            await self.db.flush()
            data = await prediction_record_command(self.db, command)
        except HTTPException as exc:
            raise _apply_prediction_error(exc) from exc
        except SQLAlchemyError as exc:
            rejection = mask_fence_error(exc)
            if rejection is None:
                raise
            raise _apply_prediction_error(rejection) from None
        assert isinstance(data, InsertPredictionResultData)
        prediction = Prediction(
            id=data.prediction_id,
            created_at=data.created_at,
            task_id=request.task_id,
            project_id=request.project_id,
            ml_backend_id=request.ml_backend_id,
            ml_backend_pool_id=request.ml_backend_pool_id,
            model_version=request.model_version,
            score=request.score,
            tool_unit_id=data.tool_unit_id,
            result=result,
            source=request.source,
            rejected_shape_indexes=[],
        )
        prediction.cluster = None
        prediction.mislabeling = None
        make_transient_to_detached(prediction)
        self.db.add(prediction)
        set_committed_value(task, "total_predictions", data.total_predictions)
        set_committed_value(task, "updated_at", data.task_updated_at)
        await self.db.flush()
        if request.repair_original_receipt is not None:
            from app.services.managed_mask_repair import recheck_credentials

            await recheck_credentials(self.db, request.repair_original_receipt)
        if request.video_input_receipt is not None:
            await request.video_input_receipt.owner.recheck_credentials()
        return ApplyPredictionResultReceipt(
            id=data.prediction_id,
            created_at=data.created_at,
            tool_unit_id=data.tool_unit_id,
        )

    async def create_test_seed_prediction(
        self,
        *,
        task_id: uuid.UUID,
        project_id: uuid.UUID,
        result: list[dict],
        score: float | None = None,
        model_version: str | None = None,
        rejected_shape_indexes: list[int] | None = None,
        tool_unit_id: str | None = None,
        write_scope: LabelingWriteScope,
    ) -> Prediction:
        """Persist a hidden E2E fixture candidate without normal inference projections."""
        if write_scope.operation != "test_seed":
            raise ValueError("test seed prediction requires a test_seed scope")
        if write_scope.actor_id is not None or not write_scope.actor_known:
            raise ValueError("test seed requires an explicitly actorless scope")
        if write_scope.task_id != task_id or write_scope.project_id != project_id:
            raise ValueError("prediction write scope does not match its target")
        await assert_isolated_test_seed_database(self.db)
        await self.db.flush()
        command = InsertTestSeedPrediction(
            schema=PREDICTION_RECORDS_PROTOCOL,
            operation="insert_test_seed",
            prediction_id=uuid.uuid4(),
            task_id=task_id,
            project_id=project_id,
            result=result,
            score=score,
            model_version=model_version,
            tool_unit_id=tool_unit_id,
            rejected_shape_indexes=rejected_shape_indexes,
        )
        data = await prediction_record_command(self.db, command)
        assert isinstance(data, InsertTestSeedPredictionData)
        prediction = Prediction(
            id=data.prediction_id,
            created_at=data.created_at,
            task_id=task_id,
            project_id=project_id,
            score=score,
            model_version=model_version,
            source="ml_backend",
            tool_unit_id=data.tool_unit_id,
            result=result,
            rejected_shape_indexes=rejected_shape_indexes or [],
        )
        prediction.cluster = None
        prediction.mislabeling = None
        make_transient_to_detached(prediction)
        self.db.add(prediction)
        await self.db.flush()
        return prediction

    async def create_failed(
        self,
        task_id: uuid.UUID | None,
        project_id: uuid.UUID,
        ml_backend_id: uuid.UUID | None,
        error_type: str,
        message: str,
        model_version: str | None = None,
        extra: dict | None = None,
        ml_backend_pool_id: uuid.UUID | None = None,
        failure_id: uuid.UUID | None = None,
        execution_snapshot_id: uuid.UUID | None = None,
    ) -> FailedPrediction:
        resolved_failure_id = failure_id if failure_id is not None else uuid.uuid4()
        await self.db.flush()
        created_at = await create_prediction_record_failed_state(
            self.db,
            failure_id=resolved_failure_id,
            task_id=task_id,
            project_id=project_id,
            ml_backend_id=ml_backend_id,
            ml_backend_pool_id=ml_backend_pool_id,
            model_version=model_version,
            error_type=error_type,
            message=message,
            extra=extra or {},
            execution_snapshot_id=execution_snapshot_id,
        )
        failed = FailedPrediction(
            id=resolved_failure_id,
            task_id=task_id,
            project_id=project_id,
            ml_backend_id=ml_backend_id,
            ml_backend_pool_id=ml_backend_pool_id,
            model_version=model_version,
            error_type=error_type,
            message=message,
            extra=extra or {},
            retry_count=0,
            dismissed_at=None,
            created_at=created_at,
        )
        failed.last_retry_at = None
        failed.execution_snapshot_id = execution_snapshot_id
        make_transient_to_detached(failed)
        self.db.add(failed)
        await self.db.flush()
        return failed

    async def dismiss_failed_prediction(self, failed: FailedPrediction) -> bool:
        """Dismiss one failed prediction without committing its caller's transaction."""
        if failed.dismissed_at is not None:
            return False
        return await set_failed_prediction_dismissed(
            self.db,
            failure_id=failed.id,
            dismissed_at=datetime.now(timezone.utc),
        )

    async def restore_failed_prediction(self, failed: FailedPrediction) -> bool:
        """Restore one dismissed failed prediction without committing."""
        if failed.dismissed_at is None:
            return False
        return await set_failed_prediction_dismissed(
            self.db,
            failure_id=failed.id,
            dismissed_at=None,
        )

    async def list_by_task(
        self, task_id: uuid.UUID, model_version: str | None = None
    ) -> list[Prediction]:
        q = select(prediction_row).where(prediction_row.task_id == task_id)
        if model_version:
            q = q.where(prediction_row.model_version == model_version)
        q = q.order_by(prediction_row.created_at.desc())
        result = await self.db.execute(q)
        return list(result.scalars().all())

    async def get_latest_for_task(self, task_id: uuid.UUID) -> Prediction | None:
        result = await self.db.execute(
            select(prediction_row)
            .where(prediction_row.task_id == task_id)
            .order_by(prediction_row.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()
