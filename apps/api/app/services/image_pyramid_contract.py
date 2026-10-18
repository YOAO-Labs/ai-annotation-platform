"""Public response adapter values for the independent image owner."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping, Protocol
from uuid import UUID

from app.schemas.image_pyramid import (
    ImagePyramidAssetUrlsRequest,
    ImagePyramidAssetUrlsResponse,
    ImagePyramidResponse,
    ImagePyramidSummary,
)

ImagePyramidOwnerKind = Literal["dataset_item", "task"]


@dataclass(frozen=True, slots=True)
class ImagePyramidTaskSource:
    """Select a Task without duplicating its private source binding."""

    task_id: UUID
    file_type: str


@dataclass(frozen=True, slots=True)
class ImagePyramidManifestResult:
    """Response facts; a missing body represents the existing 304 path."""

    body: ImagePyramidResponse | None
    etag: str | None
    cache_control: str
    outcome: str


@dataclass(frozen=True, slots=True)
class ImagePyramidRetryPlan:
    status: Literal["queued", "pending", "building"]
    owner_kind: ImagePyramidOwnerKind | None = None
    owner_id: UUID | None = None
    job_id: UUID | None = None


class ImagePyramidFailure(Exception):
    """Expected HTTP failure facts from the local image-pyramid owner."""

    def __init__(
        self,
        status_code: int,
        detail: object,
        headers: Mapping[str, str] | None = None,
        *,
        metric_outcome: str | None = None,
    ) -> None:
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail
        self.headers = headers
        self.metric_outcome = metric_outcome


class ImagePyramidApiPort(Protocol):
    """Task-scoped public response projection over the image owner contract."""

    async def task_summaries(
        self,
        tasks: tuple[ImagePyramidTaskSource, ...],
    ) -> dict[UUID, ImagePyramidSummary]: ...

    async def manifest(
        self, task: ImagePyramidTaskSource, *, if_none_match: str | None
    ) -> ImagePyramidManifestResult: ...

    async def asset_urls(
        self, task: ImagePyramidTaskSource, payload: ImagePyramidAssetUrlsRequest
    ) -> ImagePyramidAssetUrlsResponse: ...

    async def retry(
        self, task: ImagePyramidTaskSource, *, operation_id: UUID
    ) -> ImagePyramidRetryPlan: ...
