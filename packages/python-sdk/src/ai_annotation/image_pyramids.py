"""Read versioned image-pyramid assets and explicitly request generation retries."""

from __future__ import annotations

from uuid import UUID, uuid4

from ai_annotation._http import HttpTransport, _idempotency_headers
from ai_annotation.task_workflow_models import (
    ImagePyramidAssetUrlsRequest,
    ImagePyramidAssetUrlsResponse,
    ImagePyramidResponse,
    ImagePyramidRetryResult,
)

IdLike = str | UUID


class ImagePyramids:
    def __init__(self, http: HttpTransport):
        self._http = http

    def manifest(self, task_id: IdLike) -> ImagePyramidResponse:
        resp = self._http.request("GET", f"/tasks/{task_id}/image-pyramid")
        return ImagePyramidResponse.model_validate(resp.json())

    def asset_urls(
        self, task_id: IdLike, body: ImagePyramidAssetUrlsRequest
    ) -> ImagePyramidAssetUrlsResponse:
        resp = self._http.request(
            "POST",
            f"/tasks/{task_id}/image-pyramid/asset-urls",
            json=body.model_dump(mode="json", by_alias=True, exclude_unset=True),
        )
        return ImagePyramidAssetUrlsResponse.model_validate(resp.json())

    def retry(
        self, task_id: IdLike, *, idempotency_key: IdLike | None = None
    ) -> ImagePyramidRetryResult:
        """Accept image work once; retain the key when repeating a lost response."""
        resp = self._http.request(
            "POST",
            f"/tasks/{task_id}/image-pyramid/retry",
            headers=_idempotency_headers(
                uuid4() if idempotency_key is None else idempotency_key
            ),
        )
        return ImagePyramidRetryResult.model_validate(resp.json())
