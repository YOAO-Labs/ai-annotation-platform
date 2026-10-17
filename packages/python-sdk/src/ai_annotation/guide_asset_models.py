"""Request and response models for project guide assets."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MAX_GUIDE_ASSET_SIZE_BYTES = 5 * 1024 * 1024
GuideAssetContentType = Literal[
    "image/png", "image/jpeg", "image/webp", "image/gif", "image/svg+xml"
]


class _GuideAssetRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        protected_namespaces=("model_validate", "model_dump"),
        hide_input_in_errors=True,
    )


class _GuideAssetResponse(BaseModel):
    model_config = ConfigDict(
        extra="allow",
        protected_namespaces=("model_validate", "model_dump"),
        hide_input_in_errors=True,
    )


class GuideAssetUploadInitRequest(_GuideAssetRequest):
    filename: str = Field(min_length=1, max_length=255)
    content_type: GuideAssetContentType
    size: int = Field(ge=1, le=MAX_GUIDE_ASSET_SIZE_BYTES)


class GuideAssetUploadInitResponse(_GuideAssetResponse):
    key: str
    upload_url: str = Field(repr=False)
    expires_in: int


class GuideAssetUploadBytesRequest(_GuideAssetRequest):
    upload_url: str = Field(repr=False)
    file_path: Path
    content_type: GuideAssetContentType


class GuideAssetUploadCompleteRequest(_GuideAssetRequest):
    key: str = Field(min_length=1)
    original_name: str = Field(min_length=1)
    content_type: GuideAssetContentType


class GuideAssetEntry(_GuideAssetResponse):
    key: str
    original_name: str
    content_type: str
    size: int
    uploaded_at: datetime


class GuideAssetDeleteResult(_GuideAssetResponse):
    deleted: str


class GuideAssetSignedUrlResponse(_GuideAssetResponse):
    url: str = Field(repr=False)
    expires_in: int
