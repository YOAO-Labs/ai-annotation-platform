"""Inference-backend IO for controlled trained-model deployments."""

from __future__ import annotations

import uuid

from fastapi import HTTPException

from app.db.models.project import Project
from app.db.models.trained_model import ModelDeployment, TrainedModelVersion
from app.db.models.training_dataset import TrainingDatasetVersion
from app.services import trained_models as models
from app.services import training_storage_io as storage_io


async def live_receipt(
    db, registry_id: uuid.UUID, model_version_id: str
) -> dict | None:
    """Read the immutable installation receipt reported by an inference backend."""
    from app.services import inference_backend_control

    try:
        return await inference_backend_control.backend_control_service(
            db
        ).trained_model_receipt(registry_id, model_version_id)
    except inference_backend_control.BackendControlOperationError as exc:
        raise models.backend_control_http_exception(exc) from exc


async def install_and_trial(
    db,
    project: Project,
    version: TrainedModelVersion,
    deployment: ModelDeployment,
):
    """Install the frozen artifact and run its fixed validation-image trial."""
    await models.enabled_backend(db, project.id, deployment.registry_id)
    await models.assert_routable_backend(db, project.id, deployment.registry_id)
    weights_owner = await models.owner_for_model(db, version)
    dataset = await db.get(
        TrainingDatasetVersion, uuid.UUID(version.provenance["dataset_id"])
    )
    if dataset is None:
        raise HTTPException(status_code=409, detail="固定训练版本不可用")
    sample = next((s for s in dataset.manifest["samples"] if s["split"] == "val"), None)
    if not sample:
        raise HTTPException(status_code=409, detail="缺少固定验证样本")
    owner, sample_key = models.frozen_sample_location(
        project.id, dataset, sample["task_id"]
    )
    weights_url = await storage_io.read(
        db,
        weights_owner,
        "training_model_url",
        lambda transport: models.download_url(
            version.artifact["key"], bucket=weights_owner.bucket, transport=transport
        ),
        object_key=version.artifact["key"],
        sha256=version.artifact["sha256"],
        size_bytes=version.artifact["size_bytes"],
    )
    sample_url = await storage_io.read(
        db,
        owner,
        "training_model_url",
        lambda transport: models.download_url(
            sample_key, bucket=owner.bucket, transport=transport
        ),
        object_key=sample_key,
        sha256=sample.get("sha256"),
        size_bytes=sample.get("size_bytes"),
    )
    from app.services import inference_backend_control

    try:
        receipt = await inference_backend_control.backend_control_service(
            db
        ).install_trained_model(
            deployment.registry_id,
            {
                **models.expected_receipt(version),
                "size_bytes": version.artifact["size_bytes"],
                "download_url": weights_url,
            },
        )
    except inference_backend_control.BackendControlOperationError as exc:
        raise models.backend_control_http_exception(exc) from exc
    models.check_receipt(version, receipt)
    runtime = receipt.get("inference_runtime")
    frozen_reference = models.frozen_reference(
        version, deployment, include_runtime=runtime is not None, runtime=runtime
    )
    from aap_inference_contract.execution import FrozenModelOperation, thaw_json
    from app.services.inference_model_execution import (
        installation_trial_model_execution_port,
    )

    request = FrozenModelOperation(
        request_id=uuid.uuid4(),
        operation="batch_predict",
        task_inputs=({"id": sample["task_id"], "file_path": sample_url},),
        context={
            "model_id": f"trained:{frozen_reference['model_version_id']}",
            "frozen_model_ref": frozen_reference,
            "params": {"conf": 0.01},
        },
        project_id=frozen_reference["project_id"],
        requested_registry_id=frozen_reference["registry_id"],
        requested_pool_id=None,
        endpoint_registry_id=None,
        required_instance_id=None,
        route_owner=f"trained-model-install:{frozen_reference['deployment_id']}",
        route_operation="backend_http",
    )
    reply = await installation_trial_model_execution_port(
        db, reference=frozen_reference
    ).execute(request)
    results = reply.results
    if len(results) != 1 or results[0].task_id != sample["task_id"]:
        raise HTTPException(status_code=422, detail="样本试用返回的任务身份不匹配")
    result = results[0]
    trial = {
        "task_id": sample["task_id"],
        "validation_digest": version.provenance["validation_digest"],
        "inference_time_ms": result.inference_time_ms,
        "result": thaw_json(result.result),
        "score": result.score,
        "sample_scope": "one_fixed_validation_image",
    }
    if runtime is not None:
        trial["inference_runtime"] = runtime
    if models.model_task(version) == "segment":
        from app.services.controlled_segmentation import materialize_masks

        meta = thaw_json(result.meta)
        segmentation = meta["segmentation"]
        dimensions = [sample["height"], sample["width"]]
        if segmentation["dimensions"] != dimensions:
            raise ValueError("Trial masks differ from frozen source dimensions")
        await materialize_masks(db, thaw_json(result.result), meta)
        trial["segmentation"] = segmentation
    return receipt, trial
