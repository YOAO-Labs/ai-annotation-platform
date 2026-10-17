"""Storage diagnostics and explicit administrator maintenance."""

from __future__ import annotations

from ai_annotation._http import HttpTransport
from ai_annotation.storage_models import (
    BucketsResponse,
    StorageHealth,
    VideoAssetFailuresResponse,
    VideoAssetRetryRequest,
    VideoAssetRetryResponse,
)


class Storage:
    def __init__(self, http: HttpTransport):
        self._http = http

    def health(self) -> StorageHealth:
        """Probe the annotation bucket; requires authentication."""
        response = self._http.request("GET", "/storage/health")
        return StorageHealth.model_validate(response.json())

    def buckets(self) -> BucketsResponse:
        """Summarize configured buckets; may perform expensive object listings."""
        response = self._http.request("GET", "/storage/buckets")
        return BucketsResponse.model_validate(response.json())

    def video_asset_failures(
        self, *, limit: int = 50, offset: int = 0
    ) -> VideoAssetFailuresResponse:
        """List failures in the authenticated administrator's permitted scope."""
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("limit must be an integer between 1 and 200")
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        response = self._http.request(
            "GET",
            "/storage/video-assets/failures",
            params={"limit": limit, "offset": offset},
        )
        return VideoAssetFailuresResponse.model_validate(response.json())

    def retry_video_asset(
        self, body: VideoAssetRetryRequest
    ) -> VideoAssetRetryResponse:
        """Queue metadata, chunk or frame generation once; does not await success."""
        response = self._http.request(
            "POST",
            "/storage/video-assets/retry",
            json=body.model_dump(mode="json", exclude_unset=True),
        )
        return VideoAssetRetryResponse.model_validate(response.json())
