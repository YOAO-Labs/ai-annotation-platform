"""Typed requests and receipts for mask-format preflight and imports."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ai_annotation.export_models import VideoExportScopeRequest

MaskFormatDirection = Literal["import", "export"]
MaskFormatLossClass = Literal["lossless", "lossy", "unsupported"]


class _MaskFormatRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid", protected_namespaces=("model_validate", "model_dump")
    )


class _MaskFormatResponse(BaseModel):
    model_config = ConfigDict(
        extra="allow", protected_namespaces=("model_validate", "model_dump")
    )


class MaskFormatCapability(_MaskFormatResponse):
    supported: bool
    verified: bool = False
    enabled_for_ui: bool = False


class MaskFormatDescriptor(_MaskFormatResponse):
    format_id: str
    label: str
    adapter_version: str
    manifest_version: str
    media_types: list[str]
    import_capability: MaskFormatCapability
    export_capability: MaskFormatCapability
    option_schema: dict[str, Any] = Field(default_factory=dict)


class MaskFormatCode(_MaskFormatResponse):
    code: str
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)


class MaskFormatPlanItem(_MaskFormatResponse):
    item_id: str
    task_id: UUID | None = None
    media_path: str | None = None
    source_index: int | None = None
    loss_class: MaskFormatLossClass
    estimated_objects: int = Field(ge=0)
    estimated_files: int = Field(ge=0)
    estimated_bytes: int = Field(ge=0)
    losses: list[MaskFormatCode] = Field(default_factory=list)
    skips: list[MaskFormatCode] = Field(default_factory=list)
    warnings: list[MaskFormatCode] = Field(default_factory=list)


class MaskFormatPlan(_MaskFormatResponse):
    format_id: str
    direction: MaskFormatDirection
    adapter_version: str
    manifest_version: str
    media_type: str
    loss_class: MaskFormatLossClass
    staged_object_key: str | None = None
    staged_sha256: str | None = None
    mapping_digest: str
    options_digest: str
    items: list[MaskFormatPlanItem] = Field(default_factory=list)
    unknown_labels: list[str] = Field(default_factory=list)
    size_conflicts: list[dict[str, Any]] = Field(default_factory=list)
    overlap_conflicts: list[dict[str, Any]] = Field(default_factory=list)
    id_mapping: dict[str, Any] = Field(default_factory=dict)
    frame_mapping: dict[str, Any] = Field(default_factory=dict)
    estimated_objects: int = Field(ge=0)
    estimated_files: int = Field(ge=0)
    estimated_bytes: int = Field(ge=0)
    losses: list[MaskFormatCode] = Field(default_factory=list)
    skips: list[MaskFormatCode] = Field(default_factory=list)
    warnings: list[MaskFormatCode] = Field(default_factory=list)
    plan_digest: str


class MaskFormatExportPreflightRequest(_MaskFormatRequest):
    targets: list[str] = Field(min_length=1, max_length=20)
    include_attributes: bool = True
    video_frame_mode: Literal["keyframes", "all_frames"] = "keyframes"
    axis_frame: Literal["iso", "source"] = "iso"
    scope: VideoExportScopeRequest | None = None
    options: dict[str, Any] = Field(default_factory=dict)


class MaskFormatExportPreflightResult(_MaskFormatResponse):
    plans: list[MaskFormatPlan]
    loss_class: MaskFormatLossClass
    estimated_objects: int = Field(ge=0)
    estimated_files: int = Field(ge=0)
    estimated_bytes: int = Field(ge=0)
    losses: list[MaskFormatCode] = Field(default_factory=list)
    warnings: list[MaskFormatCode] = Field(default_factory=list)
    preflight_digest: str


class MaskFormatUploadInitRequest(_MaskFormatRequest):
    file_name: str = Field(min_length=1, max_length=255)
    content_type: str = Field(default="application/octet-stream", max_length=255)

    @field_validator("file_name")
    @classmethod
    def validate_file_name(cls, value: str) -> str:
        if value != value.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]:
            raise ValueError("file_name must not contain path components")
        if value in {".", ".."} or "\x00" in value:
            raise ValueError("invalid file_name")
        return value


class MaskFormatUploadInitResult(_MaskFormatResponse):
    object_key: str
    upload_url: str = Field(repr=False)
    expires_in: int


class MaskFormatImportPreflightRequest(_MaskFormatRequest):
    format_id: str = Field(min_length=1, max_length=80)
    staged_object_key: str = Field(min_length=1, max_length=500)
    staged_sha256: str = Field(pattern="^[0-9a-f]{64}$")
    mapping: dict[str, Any] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)


class MaskFormatImportPreflightResult(_MaskFormatResponse):
    import_id: UUID
    receipt: str = Field(min_length=16, max_length=256, repr=False)
    receipt_expires_at: datetime
    plan: MaskFormatPlan


class MaskFormatImportExecuteRequest(_MaskFormatRequest):
    receipt: str = Field(min_length=16, max_length=256, repr=False)
    plan_digest: str = Field(pattern="^[0-9a-f]{64}$")
    confirm_lossy: bool = False


class MaskFormatImportBatch(_MaskFormatResponse):
    id: UUID
    project_id: UUID
    async_job_id: UUID | None
    format_id: str
    adapter_version: str
    manifest_version: str
    staged_sha256: str
    plan_digest: str
    status: str
    result: dict[str, Any]
    receipt_expires_at: datetime
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
