"""Inference-backend IO for controlled trained-model deployments."""

from __future__ import annotations

import uuid

from fastapi import HTTPException

from app.db.models.project import Project
from app.db.models.trained_model import ModelDeployment, TrainedModelVersion
from app.db.models.training_dataset import TrainingDatasetVersion
from app.services import trained_models as models
from app.services.ml_client import MLBackendClient
from app.services.trained_model_reference_contract import (
    FrozenModelReference,
    FrozenModelReferenceFacts,
    build_frozen_model_reference,
)


async def live_receipt(backend, model_version_id: str) -> dict:
    """Read the immutable installation receipt reported by an inference backend."""
    return await MLBackendClient(backend).trained_model_receipt(model_version_id)


async def install_and_trial(
    db,
    project: Project,
    version: TrainedModelVersion,
    deployment: ModelDeployment,
    *,
    reference_facts: FrozenModelReferenceFacts,
):
    """Install the frozen artifact and run its fixed validation-image trial."""
    backend = await models.enabled_backend(db, project.id, deployment.registry_id)
    await models.assert_routable_backend(db, project.id, deployment.registry_id)
    client = MLBackendClient(backend)
    receipt = await client.install_trained_model(
        {
            **models.expected_receipt(version),
            "size_bytes": version.artifact["size_bytes"],
            "download_url": models.download_url(version.artifact["key"]),
        }
    )
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
    results = await client.predict(
        [
            {
                "id": sample["task_id"],
                "file_path": models.download_url(
                    f"projects/{project.id}/datasets/{dataset.id}/media/{sample['task_id']}"
                ),
            },
        ],
        context=models.wire_context(frozen_reference, {"conf": 0.01}),
    )
    if len(results) != 1 or str(results[0].task_id) != sample["task_id"]:
        raise HTTPException(status_code=422, detail="样本试用返回的任务身份不匹配")
    models.validate_and_map_results(results, frozen_reference)
    return receipt, {
        "task_id": sample["task_id"],
        "validation_digest": version.provenance["validation_digest"],
        "inference_time_ms": results[0].inference_time_ms,
        "result": results[0].result,
        "score": results[0].score,
        "sample_scope": "one_fixed_validation_image",
    }
