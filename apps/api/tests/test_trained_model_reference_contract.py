from __future__ import annotations

import uuid

from app.services.trained_model_reference_contract import (
    FrozenModelReferenceFacts,
    build_frozen_model_reference,
)
from app.services.training_dataset_bridge import content_digest


def test_frozen_reference_keeps_legacy_keys_nulls_and_canonical_digest():
    project_id, registry_id, deployment_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    receipt = {
        "model_version_id": str(uuid.uuid4()),
        "sha256": "a" * 64,
        "classes_digest": "b" * 64,
        "classes": ["car"],
        "framework": "ultralytics",
        "framework_version": "8.4.177",
        "task": "detection",
    }
    bindings = ["car"]
    provenance = {"dataset_id": str(uuid.uuid4())}
    facts = FrozenModelReferenceFacts(
        project_id=project_id,
        registry_id=registry_id,
        deployment_id=deployment_id,
        binding_digest="c" * 64,
        bindings=bindings,
        provenance=provenance,
    )

    reference = build_frozen_model_reference(facts, receipt)
    payload = {
        **receipt,
        "project_id": str(project_id),
        "registry_id": str(registry_id),
        "deployment_id": str(deployment_id),
        "binding_digest": "c" * 64,
        "bindings": bindings,
        "provenance": provenance,
        "release_id": None,
        "release_generation": None,
    }
    assert reference == {**payload, "reference_digest": content_digest(payload)}
