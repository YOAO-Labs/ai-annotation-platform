"""Plain frozen-reference fixtures for trained-model tests."""

from app.services.trained_model_reference_contract import (
    FrozenModelReferenceFacts,
    build_frozen_model_reference,
)
from app.services.trained_models import expected_receipt


def build_reference(version, deployment, release=None):
    return build_frozen_model_reference(
        FrozenModelReferenceFacts(
            project_id=version.project_id,
            registry_id=deployment.registry_id,
            deployment_id=deployment.id,
            binding_digest=version.binding_digest,
            bindings=version.bindings,
            provenance=version.provenance,
            release_id=release.id if release else None,
            release_generation=release.generation if release else None,
        ),
        expected_receipt(version),
    )
