"""Authorized discovery projections, never executable frozen credentials."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.ml_backend import ModelCapability


class CatalogOperation(BaseModel):
    allowed: bool
    reason_code: str | None = None
    reason: str | None = None


class CatalogProject(BaseModel):
    id: UUID
    name: str


class CatalogClassMetadata(BaseModel):
    class_source: Literal[
        "checkpoint", "training_config", "builtin_manifest", "request_prompt"
    ]
    verification_status: Literal[
        "verified",
        "declared",
        "uninspected",
        "unavailable",
        "mismatch",
        "request_defined",
    ]
    classes_digest: str | None = None
    artifact_sha256: str | None = None
    loader_version: str | None = None
    inspected_at: datetime | None = None


class CatalogPlacement(BaseModel):
    registry_id: UUID
    pool_id: UUID | None = None
    name: str
    deployment_id: UUID | None = None
    ready: bool
    params: dict = Field(default_factory=dict)
    reason_code: str | None = None
    reason: str | None = None


class CatalogEntry(BaseModel):
    id: str
    source: Literal["builtin", "project_trained"]
    name: str
    task: str
    media_type: str
    framework: str | None = None
    project: CatalogProject | None = None
    model_id: str
    model_variants: dict[str, str] = Field(default_factory=dict)
    version_id: UUID | None = None
    publication: Literal["builtin", "candidate", "current", "previous", "archived"]
    capabilities: ModelCapability
    output_classes: list[str] = Field(default_factory=list)
    class_metadata: CatalogClassMetadata
    placements: list[CatalogPlacement] = Field(default_factory=list)
    operations: dict[str, CatalogOperation]


class CatalogCurrentRelease(BaseModel):
    entry_id: str
    model_id: Literal["project-release", "project-release:segment"] = "project-release"
    task: Literal["detect", "segment"] = "detect"
    registry_id: UUID
    deployment_id: UUID
    generation: int


class ModelCatalogResponse(BaseModel):
    items: list[CatalogEntry]
    total: int
    page: int
    page_size: int
    projects: list[CatalogProject] = Field(default_factory=list)
    current_release: CatalogCurrentRelease | None = None
    current_releases: dict[str, CatalogCurrentRelease] = Field(default_factory=dict)
    task_counts: dict[str, int] = Field(default_factory=dict)
