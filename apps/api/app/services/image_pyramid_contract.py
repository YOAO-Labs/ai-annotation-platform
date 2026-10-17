"""Value interface for task image-pyramid reads and retry decisions."""

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
    """The loaded Task fields needed to build an image-pyramid source."""

    task_id: UUID
    dataset_item_id: UUID | None
    file_path: str
    file_type: str


@dataclass(frozen=True, slots=True)
class ImagePyramidDispatchTarget:
    owner_kind: ImagePyramidOwnerKind
    owner_id: UUID
    force: bool


@dataclass(frozen=True, slots=True)
class ImagePyramidManifestResult:
    """Response facts; a missing body represents the existing 304 path."""

    body: ImagePyramidResponse | None
    etag: str | None
    cache_control: str
    outcome: str
    dispatch: ImagePyramidDispatchTarget | None = None


@dataclass(frozen=True, slots=True)
class ImagePyramidRetryPlan:
    status: Literal["queued", "pending", "building"]
    owner_kind: ImagePyramidOwnerKind | None = None
    owner_id: UUID | None = None


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
    """Task-scoped image-pyramid operations, bound to the caller's Session."""

    async def task_summaries(
        self,
        tasks: tuple[ImagePyramidTaskSource, ...],
        dimensions: Mapping[UUID, tuple[int | None, int | None]],
    ) -> dict[UUID, ImagePyramidSummary]: ...

    async def manifest(
        self, task: ImagePyramidTaskSource, *, if_none_match: str | None
    ) -> ImagePyramidManifestResult: ...

    async def asset_urls(
        self, task: ImagePyramidTaskSource, payload: ImagePyramidAssetUrlsRequest
    ) -> ImagePyramidAssetUrlsResponse: ...

    async def retry(self, task: ImagePyramidTaskSource) -> ImagePyramidRetryPlan: ...
