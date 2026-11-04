"""Response models for project guide assets."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

MAX_GUIDE_ASSET_SIZE_BYTES = 5 * 1024 * 1024
GuideAssetContentType = Literal[
    "image/png", "image/jpeg", "image/webp", "image/gif", "image/svg+xml"
]


class _GuideAssetResponse(BaseModel):
    model_config = ConfigDict(
        extra="allow",
        protected_namespaces=("model_validate", "model_dump"),
        hide_input_in_errors=True,
    )


class GuideAssetEntry(_GuideAssetResponse):
    key: str
    original_name: str
    content_type: str
    size: int
    uploaded_at: datetime


class GuideAssetDeleteResult(_GuideAssetResponse):
    deleted: str
