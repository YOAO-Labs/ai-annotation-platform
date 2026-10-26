"""Inference-backend IO for controlled trained-model deployments."""

from __future__ import annotations

import uuid

from fastapi import HTTPException

from app.db.models.project import Project
from app.db.models.trained_model import ModelDeployment, TrainedModelVersion
from app.db.models.training_dataset import TrainingDatasetVersion
from app.services import trained_models as models
from app.services.trained_model_reference_contract import (
    FrozenModelReference,
    FrozenModelReferenceFacts,
    build_frozen_model_reference,
)


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
    *,
    reference_facts: FrozenModelReferenceFacts,
):
    """Install the frozen artifact and run its fixed validation-image trial."""
    await models.enabled_backend(db, project.id, deployment.registry_id)
    await models.assert_routable_backend(db, project.id, deployment.registry_id)
    from app.services import inference_backend_control

    try:
        receipt = await inference_backend_control.backend_control_service(
            db
        ).install_trained_model(
            deployment.registry_id,
            {
                **models.expected_receipt(version),
                "size_bytes": version.artifact["size_bytes"],
                "download_url": models.download_url(version.artifact["key"]),
            },
        )
    except inference_backend_control.BackendControlOperationError as exc:
        raise models.backend_control_http_exception(exc) from exc
    models.check_receipt(version, receipt)
    dataset = await db.get(
        TrainingDatasetVersion, uuid.UUID(version.provenance["dataset_id"])
    )
    sample = next((s for s in dataset.manifest["samples"] if s["split"] == "val"), None)
    if not sample:
        raise HTTPException(status_code=409, detail="缺少固定验证样本")
    frozen_reference: FrozenModelReference = build_frozen_model_reference(
        reference_facts, models.expected_receipt(version)
    )
    from aap_inference_contract.execution import FrozenModelOperation, thaw_json
    from app.services.inference_model_execution import (
        installation_trial_model_execution_port,
    )

    request = FrozenModelOperation(
        request_id=uuid.uuid4(),
        operation="batch_predict",
        task_inputs=(
            {
                "id": sample["task_id"],
                "file_path": models.download_url(
                    f"projects/{project.id}/datasets/{dataset.id}/media/{sample['task_id']}"
                ),
            },
        ),
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
    return receipt, {
        "task_id": sample["task_id"],
        "validation_digest": version.provenance["validation_digest"],
        "inference_time_ms": result.inference_time_ms,
        "result": thaw_json(result.result),
        "score": result.score,
        "sample_scope": "one_fixed_validation_image",
    }
