"""Shared bounded managed detection identity; native receipt wire remains stable."""

from __future__ import annotations

import base64
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException
from pydantic import ValidationError
from training_contract.models import model_spec

from app.db.models.trained_model import ModelDeployment
from app.schemas.sample_evaluation import EvaluationProfile
from app.services import sample_evaluation_engine as engine, trained_models
from aap_inference.backend_transport import PredictionResult
from aap_task_contract import normalize_task
from app.services.training_dataset_bridge import content_digest
from app.services.trained_model_reference_contract import (
    FreezeModelReferenceError,
    FreezeModelReferenceRequest,
)


class RemoteUncertain(Exception):
    """The fixed remote request has no confirmed final receipt."""


def segmentation_spec(spec):
    task = spec.get("model_ref", {}).get("task") or spec.get("task")
    return (
        normalize_task(task or ("segment" if spec.get("schema") == 2 else "detect"))
        == "segment"
    )


def reject(code, message, status=409):
    return HTTPException(status_code=status, detail={"code": code, "message": message})


async def qualification(db, project, model_id, deployment_id):
    deployment = await db.get(ModelDeployment, deployment_id)
    if not deployment or deployment.model_version_id != model_id:
        raise reject("deployment_unavailable", "受控部署不存在", 404)
    try:
        ref = await trained_models.bind_frozen_model_reference(db).freeze(
            FreezeModelReferenceRequest(
                project_id=project.id,
                project_classes=trained_models.project_classes(project),
                model_id=f"trained:{model_id}",
                registry_id=deployment.registry_id,
            )
        )
    except FreezeModelReferenceError as exc:
        trained_models.raise_frozen_model_reference_error(exc)
    if ref["deployment_id"] != str(deployment_id):
        raise reject("deployment_changed", "受控部署身份已变化")
    if ref["framework"] != "ultralytics":
        raise reject(
            "framework_unqualified",
            "该框架尚未通过共享样本评估资格检查；模型试用与人工发布仍可使用",
            422,
        )
    await trained_models.enabled_backend(db, project.id, deployment.registry_id)
    segment = normalize_task(ref.get("task") or "detect") == "segment"
    framework_version = ref.get("framework_version")
    if framework_version not in {"8.4.177", "8.4.183"}:
        raise reject("runtime_unqualified", "模型缺少已核验的冻结训练器版本")
    runtime_version = trained_models.inference_version(ref)
    selected_model = None
    if runtime_version != "8.4.177":
        try:
            base_model = ref["provenance"]["base_model"]["id"]
            selected_model = model_spec("ultralytics", base_model)
            if selected_model.task != ("segment" if segment else "detect"):
                raise ValueError("Frozen model task differs")
        except (KeyError, TypeError, ValueError) as exc:
            raise reject(
                "runtime_unqualified", "模型缺少已核验的家族及规格身份"
            ) from exc
    from app.services import inference_backend_control

    try:
        profile = await inference_backend_control.backend_control_service(
            db
        ).managed_evaluation_profile(deployment.registry_id)
    except inference_backend_control.BackendControlOperationError as exc:
        raise trained_models.backend_control_http_exception(exc) from exc
    try:
        checked = EvaluationProfile.model_validate(profile).model_dump(
            mode="json", by_alias=True
        )
    except ValidationError as exc:
        raise reject(
            "evaluation_protocol_unqualified",
            "推理实例返回了不支持的评估协议，请检查推理服务版本",
            502,
        ) from exc
    if checked["ultralytics_version"] != runtime_version:
        raise reject(
            "deployment_runtime_changed",
            "推理环境已变化，请在模型详情重新加载并试用，再创建评估",
        )
    if (
        checked["profile_digest"]
        != content_digest({k: v for k, v in checked.items() if k != "profile_digest"})
        or checked["revision"] != {"8.4.177": 1, "8.4.183": 2}.get(runtime_version)
        or (
            selected_model is not None
            and selected_model.end2end
            and checked["options"].get("nms") is not False
        )
        or checked["implementation"]
        != ("aap-ultralytics-instance-mask" if segment else "aap-ultralytics-detection")
    ):
        raise reject("runtime_unqualified", "推理环境尚未通过逐样本评估资格检查")
    return ref, checked


def request_identity(spec, row, preprocessing):
    request = {
        "request_id": str(row.request_id),
        "profile_digest": spec["profile_digest"],
        "storage_identity": spec["profile"]["storage_identity"],
        "input_sha256": preprocessing["derived_sha256"],
        "issued_at": spec["issued_at"],
        "expires_at": spec["expires_at"],
    }
    wire_ref = trained_models.wire_reference(spec["model_ref"])
    request["binding_digest"] = content_digest(
        {
            "task_id": str(spec["samples"][row.ordinal]["task_id"]),
            "model_ref": wire_ref,
            "params": {
                "conf": spec["confidence"],
                "iou": spec["nms_iou"],
                "max_det": engine.MAX_PREDICTIONS,
            },
            "evaluation": request,
        }
    )
    return {
        "evaluation": request,
        "preprocessing": preprocessing,
        **({"task": "segment"} if segmentation_spec(spec) else {}),
    }


def verify_inference(spec, row, inference):
    request = row.request["evaluation"]
    frozen = spec["samples"][row.ordinal]
    expected = {
        **request,
        "width": frozen["width"],
        "height": frozen["height"],
        "effective_device": spec["profile"]["effective_device"],
        "options": spec["profile"]["options"],
        "conf": spec["confidence"],
        "nms_iou": spec["nms_iou"],
    }
    expected_end2end = None
    if spec["profile"]["ultralytics_version"] != "8.4.177":
        ref = spec["model_ref"]
        try:
            native = model_spec("ultralytics", ref["provenance"]["base_model"]["id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Frozen inference model identity is unavailable") from exc
        if (
            trained_models.inference_version(ref)
            != spec["profile"]["ultralytics_version"]
            or native.task != ("segment" if segmentation_spec(spec) else "detect")
            or (native.end2end and expected["options"].get("nms") is not False)
        ):
            raise ValueError("Inference profile differs from the frozen native head")
        expected_end2end = native.end2end
    if (
        not isinstance(inference, dict)
        or set(inference) != {*expected, "end2end"}
        or any(inference.get(k) != v for k, v in expected.items())
        or type(inference.get("end2end")) is not bool
        or (
            expected_end2end is not None
            and inference.get("end2end") != expected_end2end
        )
    ):
        raise ValueError("Inference execution identity differs from the frozen request")


@dataclass(frozen=True, slots=True)
class _BackendControlHandle:
    registry_id: uuid.UUID
    session_factory: Callable[[], Any] = field(repr=False)


async def backend_control_handle(db, model_ref, *, session_factory):
    # Disabled inference must still permit stop-confirmation of old requests.
    from app.services.ml_backend import MLBackendService

    registry_id = uuid.UUID(model_ref["registry_id"])
    if await MLBackendService(db).get_registry(registry_id) is None:
        raise RemoteUncertain
    return _BackendControlHandle(registry_id, session_factory)


def _prediction_result(result):
    from aap_inference_contract.execution import thaw_json

    return PredictionResult(
        task_id=result.task_id,
        result=thaw_json(result.result),
        score=result.score,
        model_version=result.model_version,
        inference_time_ms=result.inference_time_ms,
        cache_hit=result.cache_hit,
        model_load_ms=result.model_load_ms,
        meta=None if result.meta is None else thaw_json(result.meta),
        mask_input_next=result.mask_input_next,
        diagnostic=None if result.diagnostic is None else thaw_json(result.diagnostic),
    )


def recovered_result(receipt, model_ref):
    from aap_inference_contract.execution import ModelTaskResult

    response = receipt.get("response")
    if (
        receipt.get("response_digest") != content_digest(response)
        or not isinstance(response, dict)
        or not isinstance(response.get("results"), list)
        or len(response["results"]) != 1
    ):
        raise ValueError("Completed request receipt has invalid evidence")
    item = response["results"][0]
    if not isinstance(item, dict):
        raise ValueError("Completed request evidence is invalid")
    result = ModelTaskResult(
        task_id=item.get("task"),
        result=item.get("result"),
        score=item.get("score"),
        model_version=item.get("model_version"),
        inference_time_ms=item.get("inference_time_ms"),
        meta=item.get("meta"),
    )
    mapped = trained_models.validate_and_map_results((result,), model_ref)
    return _prediction_result(mapped[0])


async def remote_receipt(backend_control, row, *, seal=False):
    request = row.request["evaluation"]
    from app.services import inference_backend_control

    async with backend_control.session_factory() as db:
        service = inference_backend_control.backend_control_service(db)
        try:
            if seal:
                receipt = await service.managed_evaluation_seal(
                    backend_control.registry_id,
                    request_id=row.request_id,
                    binding_digest=request["binding_digest"],
                    profile_digest=request["profile_digest"],
                    storage_identity=request["storage_identity"],
                    issued_at=request["issued_at"],
                    expires_at=request["expires_at"],
                )
            else:
                receipt = await service.managed_evaluation_receipt(
                    backend_control.registry_id,
                    row.request_id,
                    request["binding_digest"],
                    request["storage_identity"],
                )
        except inference_backend_control.BackendControlOperationError as exc:
            raise trained_models.backend_control_http_exception(exc) from exc
        await db.commit()
    if receipt is None:
        raise RemoteUncertain
    return receipt


def checked_receipt(row, receipt):
    request = row.request["evaluation"]
    expected = {
        key: request[key]
        for key in ("request_id", "binding_digest", "storage_identity")
    }
    if not isinstance(receipt, dict) or any(
        receipt.get(key) != value for key, value in expected.items()
    ):
        raise ValueError("Remote receipt belongs to another frozen request")
    state = receipt.get("state")
    if state not in {"missing", "bound", "sealed", "completed"}:
        raise ValueError("Remote receipt state is invalid")
    if state != "missing" and (
        type(receipt.get("schema")) is not int
        or receipt.get("schema") != 1
        or type(receipt.get("issued_at")) is not int
        or type(receipt.get("expires_at")) is not int
        or any(
            receipt.get(key) != request[key]
            for key in ("profile_digest", "issued_at", "expires_at")
        )
    ):
        raise ValueError("Remote receipt changed its original profile or lifetime")
    if state in {"bound", "sealed"} and (
        receipt.get("response") is not None
        or receipt.get("response_digest") is not None
    ):
        raise ValueError("Incomplete receipt contains fabricated successful evidence")
    return receipt


async def predict(db, factory, *, project_id, spec, row, png, owner, operation):
    from aap_inference_contract.execution import FrozenModelOperation
    from app.services.inference_model_execution import model_execution_port

    registry_id = uuid.UUID(spec["model_ref"]["registry_id"])
    request = FrozenModelOperation(
        request_id=uuid.uuid4(),
        operation="batch_predict",
        task_inputs=(
            {
                "id": spec["samples"][row.ordinal]["task_id"],
                "file_path": "data:image/png;base64," + base64.b64encode(png).decode(),
            },
        ),
        context={
            "model_id": f"trained:{spec['model_ref']['model_version_id']}",
            "frozen_model_ref": spec["model_ref"],
            "params": {
                "conf": spec["confidence"],
                "iou": spec["nms_iou"],
                "max_det": engine.MAX_PREDICTIONS,
            },
            "sample_evaluation": row.request["evaluation"],
        },
        project_id=uuid.UUID(str(project_id)),
        requested_registry_id=registry_id,
        requested_pool_id=None,
        endpoint_registry_id=None,
        required_instance_id=None,
        route_owner=owner,
        route_operation=operation,
        max_response_bytes=engine.MAX_RESPONSE_BYTES,
    )
    reply = await model_execution_port(db, session_factory=factory).execute(request)
    return _prediction_result(reply.results[0])
