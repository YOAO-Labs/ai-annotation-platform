"""Typed request and response models for project task files."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from ai_annotation.models import _AAPModel


class _RequestModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        allow_inf_nan=False,
        protected_namespaces=("model_validate", "model_dump"),
    )


class FileUploadInitRequest(_RequestModel):
    project_id: UUID
    file_name: str
    content_type: str = "image/jpeg"


class FileUploadInit(_AAPModel):
    task_id: UUID
    upload_url: str
    expires_in: int


class FileUploadComplete(_AAPModel):
    status: str
    task_id: UUID


class TaskFileUrl(_AAPModel):
    url: str
    expires_in: int


class ThumbnailBackfill(_AAPModel):
    project_id: UUID
    operation_id: UUID
    job_id: UUID
    purpose: Literal["thumbnail"]
    status: Literal["pending", "running", "ready", "failed", "canceled"]
    accepted_before: datetime
