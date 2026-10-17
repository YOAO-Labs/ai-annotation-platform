"""Project-scoped annotation guide assets."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from ai_annotation._http import HttpTransport
from ai_annotation.guide_asset_models import (
    GuideAssetDeleteResult,
    GuideAssetEntry,
    GuideAssetSignedUrlResponse,
    GuideAssetUploadCompleteRequest,
    GuideAssetUploadInitRequest,
    GuideAssetUploadInitResponse,
)

ProjectIdLike = str | UUID


class GuideAssets:
    def __init__(self, http: HttpTransport):
        self._http = http

    def upload_init(
        self, project_id: ProjectIdLike, request: GuideAssetUploadInitRequest
    ) -> GuideAssetUploadInitResponse:
        response = self._http.request(
            "POST",
            f"/projects/{project_id}/guide-assets/upload-init",
            json=request.model_dump(mode="json", by_alias=True, exclude_none=True),
        )
        return GuideAssetUploadInitResponse.model_validate(response.json())

    def upload_bytes(
        self,
        upload_url: str,
        file_path: str | Path,
        content_type: str,
    ) -> None:
        """PUT bytes to the signed URL without API credentials or completion."""
        self._http.put_presigned(upload_url, Path(file_path), content_type)

    def upload_complete(
        self, project_id: ProjectIdLike, request: GuideAssetUploadCompleteRequest
    ) -> GuideAssetEntry:
        response = self._http.request(
            "POST",
            f"/projects/{project_id}/guide-assets/upload-complete",
            json=request.model_dump(mode="json", by_alias=True, exclude_none=True),
        )
        return GuideAssetEntry.model_validate(response.json())

    def delete(self, project_id: ProjectIdLike, key: str) -> GuideAssetDeleteResult:
        response = self._http.request(
            "DELETE",
            f"/projects/{project_id}/guide-assets",
            params={"key": key},
        )
        return GuideAssetDeleteResult.model_validate(response.json())

    def sign_url(
        self, project_id: ProjectIdLike, key: str
    ) -> GuideAssetSignedUrlResponse:
        response = self._http.request(
            "GET",
            f"/projects/{project_id}/guide-assets/sign-url",
            params={"key": key},
        )
        return GuideAssetSignedUrlResponse.model_validate(response.json())
