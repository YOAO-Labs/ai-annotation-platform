"""Persistence-free contract for freezing and rechecking trained-model identity."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, NotRequired, Protocol, TypedDict

from yoao_task_contract import normalize_task

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


def validate_inference_runtime(ref, runtime):
    if not isinstance(runtime, Mapping):
        raise ValueError("Inference runtime must be an object")
    framework = runtime.get("framework")
    version = runtime.get("version")
    qualification = runtime.get("qualification")
    if (
        not isinstance(framework, str)
        or not framework
        or not isinstance(version, str)
        or not version
        or not isinstance(qualification, Mapping)
    ):
        raise ValueError("Inference runtime is missing required identity fields")
    expected_framework = ref.get("framework") if isinstance(ref, Mapping) else None
    if expected_framework and framework != expected_framework:
        raise ValueError("Inference runtime framework differs from frozen model")
    training_version = runtime.get("training_version")
    expected_version = (
        ref.get("framework_version") if isinstance(ref, Mapping) else None
    )
    if (
        training_version is not None
        and expected_version is not None
        and training_version != expected_version
    ):
        raise ValueError("Inference runtime training_version differs from frozen model")
    runtime_task = runtime.get("task")
    expected_task = ref.get("task") if isinstance(ref, Mapping) else None
    if (
        runtime_task is not None
        and expected_task is not None
        and normalize_task(runtime_task) != normalize_task(expected_task)
    ):
        raise ValueError("Inference runtime task differs from frozen model")
    device = qualification.get("device")
    if device is not None and device not in {"cpu", "cuda"}:
        raise ValueError("Inference runtime device is invalid")
    if "end2end" in qualification and type(qualification["end2end"]) is not bool:
        raise ValueError("Inference runtime end2end flag must be boolean")
    if qualification.get("end2end") is True and qualification.get("nms") is not False:
        raise ValueError("End-to-end inference runtime requires nms=False")
    pinned_qual = (
        ref.get("runtime_qualification")
        if isinstance(ref, Mapping)
        and isinstance(ref.get("runtime_qualification"), Mapping)
        else None
    )
    if pinned_qual is not None:
        if (
            "end2end" in pinned_qual
            and "end2end" in qualification
            and qualification["end2end"] != pinned_qual["end2end"]
        ):
            raise ValueError("Inference runtime end2end differs from frozen model")
    if expected_task is not None and normalize_task(expected_task) == "segment":
        if (
            not isinstance(qualification.get("decoder"), str)
            or not qualification["decoder"]
            or not isinstance(qualification.get("mask_processing"), str)
            or not qualification["mask_processing"]
        ):
            raise ValueError("Segmentation inference runtime missing decoder metadata")
    return dict(runtime)


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
