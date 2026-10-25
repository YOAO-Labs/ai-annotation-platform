"""Shared bounded managed detection identity; native receipt wire remains stable."""

from __future__ import annotations

import base64
import uuid

from fastapi import HTTPException

from app.db.models.ml_backend_registry import MLBackendRegistry
from app.db.models.trained_model import ModelDeployment
from app.schemas.sample_evaluation import EvaluationProfile
from app.services import sample_evaluation_engine as engine, trained_models
from app.services.gpu_arbitration.dispatch import build_gpu_dispatch_context_factory
from aap_inference.backend_transport import PredictionResult
from app.services.ml_client import MLBackendClient
from app.services.ml_routing.client import RoutedMLBackendClient
from app.services.training_dataset_bridge import content_digest
from app.services.trained_model_reference_contract import (
    FreezeModelReferenceError,
    FreezeModelReferenceRequest,
)


class RemoteUncertain(Exception):
    """The fixed remote request has no confirmed final receipt."""


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
    backend = await trained_models.enabled_backend(
        db, project.id, deployment.registry_id
    )
    profile = await MLBackendClient(backend).managed_evaluation_request("profile")
    checked = EvaluationProfile.model_validate(profile).model_dump(
        mode="json", by_alias=True
    )
    if (
        checked["profile_digest"]
        != content_digest({k: v for k, v in checked.items() if k != "profile_digest"})
        or checked["ultralytics_version"] != "8.4.177"
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
    wire_ref = {
        k: spec["model_ref"][k]
        for k in ("model_version_id", "sha256", "classes_digest")
    }
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
    return {"evaluation": request, "preprocessing": preprocessing}


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
    if (
        not isinstance(inference, dict)
        or set(inference) != {*expected, "end2end"}
        or any(inference.get(k) != v for k, v in expected.items())
        or type(inference.get("end2end")) is not bool
    ):
        raise ValueError("Inference execution identity differs from the frozen request")


async def private_client(db, model_ref):
    # Disabled inference must still permit stop-confirmation of old requests.
    backend = await db.get(MLBackendRegistry, uuid.UUID(model_ref["registry_id"]))
    if backend is None:
        raise RemoteUncertain
    return MLBackendClient(backend)


def recovered_result(receipt, model_ref):
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
    result = PredictionResult(
        task_id=item.get("task"),
        result=item.get("result"),
        score=item.get("score"),
        model_version=item.get("model_version"),
        inference_time_ms=item.get("inference_time_ms"),
        meta=item.get("meta"),
    )
    trained_models.validate_and_map_results([result], model_ref)
    return result


async def remote_receipt(client, row, *, seal=False):
    request = row.request["evaluation"]
    path = f"requests/{row.request_id}"
    if seal:
        return await client.managed_evaluation_request(
            path + "/seal",
            payload={
                key: request[key]
                for key in (
                    "binding_digest",
                    "profile_digest",
                    "storage_identity",
                    "issued_at",
                    "expires_at",
                )
            },
        )
    return await client.managed_evaluation_request(
        path,
        params={
            "binding_digest": request["binding_digest"],
            "storage_identity": request["storage_identity"],
        },
    )


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
    routed = RoutedMLBackendClient(
        db,
        requested_registry=uuid.UUID(spec["model_ref"]["registry_id"]),
        project_id=project_id,
        owner=owner,
        operation=operation,
        shadow_session_factory=factory,
        dispatch_context_factory=build_gpu_dispatch_context_factory(factory),
    )
    results = await routed.predict(
        [
            {
                "id": spec["samples"][row.ordinal]["task_id"],
                "file_path": "data:image/png;base64," + base64.b64encode(png).decode(),
            }
        ],
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
        max_response_bytes=engine.MAX_RESPONSE_BYTES,
    )
    return results[0]
