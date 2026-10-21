from datetime import datetime

from aap_workspace.guide_asset_policy import MAX_GUIDE_ASSET_SIZE_BYTES
from pydantic import BaseModel, Field


class GuideAssetUploadInitRequest(BaseModel):
    filename: str = Field(..., min_length=1, max_length=255)
    content_type: str
    size: int = Field(..., ge=1, le=MAX_GUIDE_ASSET_SIZE_BYTES)


class GuideAssetUploadInitResponse(BaseModel):
    key: str
    upload_url: str
    expires_in: int


class GuideAssetEntry(BaseModel):
    key: str
    original_name: str
    content_type: str
    size: int
    uploaded_at: datetime


class GuideAssetSignedUrlResponse(BaseModel):
    url: str
    expires_in: int
