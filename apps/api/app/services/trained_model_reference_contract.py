"""Persistence-free contract for freezing and rechecking trained-model identity."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, NotRequired, Protocol, TypedDict

from app.services.training_dataset_bridge import content_digest


class FrozenModelReference(TypedDict):
    """The existing raw JSON identity; unknown keys are not normalized or discarded."""

    model_version_id: str
    sha256: str
    classes_digest: str
    classes: list[str]
    framework: str
    framework_version: str
    task: str
    project_id: str
    registry_id: str
    deployment_id: str
    binding_digest: str
    bindings: list[str]
    provenance: dict[str, Any]
    release_id: str | None
    release_generation: int | None
    reference_digest: str
    model_family: NotRequired[str]
    model_size: NotRequired[str]
    artifact_format: NotRequired[str]
    artifact_size_bytes: NotRequired[int]
    runtime_qualification: NotRequired[dict[str, Any]]
    inference_runtime: NotRequired[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class RegistryLearningUsage:
    """Dependencies that prevent removing an inference installation."""

    active_releases: int
    frozen_executions: int


@dataclass(frozen=True, slots=True)
class FreezeModelReferenceRequest:
    project_id: uuid.UUID
    project_classes: list[str]
    model_id: str | None
    registry_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class ValidateFrozenModelReferenceRequest:
    project_id: uuid.UUID
    reference: FrozenModelReference


@dataclass(frozen=True, slots=True)
class FrozenModelReferenceFacts:
    """Plain values used to preserve the established JSON digest and field set."""

    project_id: uuid.UUID
    registry_id: uuid.UUID
    deployment_id: uuid.UUID
    binding_digest: str
    bindings: list[str]
    provenance: dict[str, Any]
    release_id: uuid.UUID | None = None
    release_generation: int | None = None


def build_frozen_model_reference(
    facts: FrozenModelReferenceFacts,
    receipt: dict[str, Any],
) -> FrozenModelReference:
    """Build the existing JSON shape from plain facts without ORM coercion."""

    value = {
        **receipt,
        "project_id": str(facts.project_id),
        "registry_id": str(facts.registry_id),
        "deployment_id": str(facts.deployment_id),
        "binding_digest": facts.binding_digest,
        "bindings": facts.bindings,
        "provenance": facts.provenance,
        "release_id": str(facts.release_id) if facts.release_id else None,
        "release_generation": facts.release_generation,
    }
    return {**value, "reference_digest": content_digest(value)}


class FreezeModelReferenceError(Exception):
    """Stable failure facts with the legacy HTTP or ValueError projection."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int | None = None,
        detail: Any = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.code = code
        self.status_code = status_code
        self.detail = detail
        self.headers = headers
        super().__init__(message)


class FreezeModelReferencePort(Protocol):
    async def freeze(
        self, request: FreezeModelReferenceRequest
    ) -> FrozenModelReference | None: ...

    async def validate(
        self, request: ValidateFrozenModelReferenceRequest
    ) -> uuid.UUID: ...
