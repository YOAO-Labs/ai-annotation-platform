"""Typed request and response models for scenes and frame trajectories."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ai_annotation.models import _AAPModel


class _RequestModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        allow_inf_nan=False,
        protected_namespaces=("model_validate", "model_dump"),
    )


class SceneUpdate(_RequestModel):
    """Partial update; explicit null is sent, but the API currently treats it as no-op."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    source_format: str | None = Field(default=None, max_length=50)
    source_metadata: dict[str, Any] | None = None


class Scene(_AAPModel):
    id: UUID
    display_id: str
    dataset_id: UUID
    name: str
    source_format: str | None = None
    source_metadata: dict[str, Any] = Field(default_factory=dict)
    created_by: UUID | None = None
    created_at: datetime
    updated_at: datetime


class SceneFramePose(_AAPModel):
    frame_index: int = Field(ge=0)
    timestamp_us: int | None = None
    ego_translation: list[float] = Field(min_length=3, max_length=3)
    ego_rotation: list[float] = Field(min_length=4, max_length=4)
    source_metadata: dict[str, Any] = Field(default_factory=dict)


class SceneTrajectory(_AAPModel):
    scene_id: UUID
    poses: list[SceneFramePose] = Field(default_factory=list)
