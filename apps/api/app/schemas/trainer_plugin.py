"""Schemas for registered trainer plugins and super-admin lifecycle operations."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr
from training_contract.trainer_plugin import (
    SHA256_PATTERN,
    TrainerCapabilitiesV1,
)

TrainerPluginStatus = Literal[
    "registered", "qualified", "enabled", "disabled", "retired"
]
TrainerExecutionMode = Literal["container", "local_process"]
TrainerSourceTier = Literal["official", "third_party"]


class TrainerPluginRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    plugin_id: str
    version: str
    extension_point: Literal["trainer"] = "trainer"
    framework_id: str
    display_name: str
    description: str | None = None
    homepage: str | None = None
    publisher: str | None = None
    license: str | None = None
    source_tier: TrainerSourceTier
    execution_mode: TrainerExecutionMode
    status: TrainerPluginStatus
    revision: int
    protocol_version: str
    artifact: str | None = None
    entrypoint: list[str]
    resource: dict[str, Any] = Field(default_factory=dict)
    capabilities: list[str]
    manifest: dict[str, Any]
    capabilities_snapshot: dict[str, Any]
    capabilities_digest: str
    qualification_report_sha256: str | None = None
    qualification_report: dict[str, Any] | None = None
    qualified_at: datetime | None = None
    enabled_at: datetime | None = None
    disabled_at: datetime | None = None
    retired_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class RegisterTrainerPluginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest: dict[str, Any]
    capabilities_snapshot: TrainerCapabilitiesV1
    execution_mode: TrainerExecutionMode = "container"
    source_tier: TrainerSourceTier = "third_party"
    artifact: StrictStr | None = None


class QualifyTrainerPluginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: StrictInt = Field(ge=1)
    qualification_report_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    qualification_report: dict[str, Any]


class TransitionTrainerPluginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: StrictInt = Field(ge=1)
