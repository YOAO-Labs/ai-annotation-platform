"""Frozen image-media protocol, independent of the API, ORM and worker runtime."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Annotated, Any, Literal, TypeVar
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from .policy import ImagePolicy

IMAGE_SCHEMA = "aap-assets-images/v1"
PYRAMID_SCHEMA = "aap-image-pyramid/v1"
REQUEST_BYTES = 8 * 1024 * 1024
REPLY_BYTES = 16 * 1024 * 1024
PAGE_BYTES = 15 * 1024 * 1024
BATCH_SIZE = 500
TILE_REFERENCE_MAX = 128
PYRAMID_TILE_SIZE = 512
PYRAMID_OVERLAP = 1

OwnerKind = Literal["dataset_item", "task"]
BucketKind = Literal["datasets", "legacy"]
Purpose = Literal["dimensions", "thumbnail", "pyramid"]
WorkKind = Literal["dimensions", "thumbnail", "pyramid", "image_backfill"]
PyramidStatus = Literal[
    "not_available",
    "missing",
    "pending",
    "building",
    "ready",
    "failed",
    "stale",
    "inconsistent",
]
ErrorCode = Literal[
    "invalid_request",
    "invalid_response",
    "storage_unavailable",
    "account_not_active",
    "account_busy",
    "invalid_account_role",
    "source_missing",
    "source_changed",
    "source_busy",
    "source_not_ready",
    "task_not_visible",
    "task_busy",
    "permission_denied",
    "not_image",
    "asset_missing",
    "already_ready",
    "not_eligible",
    "retry_cooldown",
    "operation_conflict",
    "operation_busy",
    "lease_lost",
    "dependency_changed",
    "resource_budget_exceeded",
    "object_missing",
    "owner_changed",
    "unsupported_dimensions",
    "resource_limit",
    "invalid_coordinate",
    "timeout",
    "source_unavailable",
    "color_profile_unavailable",
    "multi_page_unsupported",
    "dimension_mismatch",
    "generator_unavailable",
    "decode_failed",
    "encode_failed",
    "tile_grid_mismatch",
    "upload_verification_failed",
    "inconsistent_ready",
]

Digest = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
PyramidDigest = Annotated[StrictStr, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
Count = Annotated[StrictInt, Field(ge=0, le=9_223_372_036_854_775_807)]
Revision = Annotated[StrictInt, Field(ge=1, le=9_223_372_036_854_775_807)]
PyramidGeneration = Annotated[StrictInt, Field(ge=1, le=2_147_483_647)]
Index = Annotated[StrictInt, Field(ge=0, le=2_147_483_647)]
Positive = Annotated[StrictInt, Field(gt=0, le=2_147_483_647)]
PageSize = Annotated[StrictInt, Field(ge=1, le=BATCH_SIZE)]
JsonObject = dict[str, JsonValue]


class Value(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, hide_input_in_errors=True
    )

    @field_validator("*", mode="after")
    @classmethod
    def require_json_safe_values(cls, value):
        def check(item):
            if isinstance(item, str) and "\x00" in item:
                raise ValueError("NUL is not supported by PostgreSQL values")
            if isinstance(item, dict):
                for key, nested in item.items():
                    check(key)
                    check(nested)
            elif isinstance(item, (list, tuple)):
                for nested in item:
                    check(nested)

        check(value)
        return value


class ObjectIdentity(Value):
    etag: Annotated[StrictStr, Field(min_length=1, max_length=255)]
    byte_size: Count
    version_id: Annotated[StrictStr, Field(max_length=255)] | None = None


class Artifact(Value):
    key: Annotated[StrictStr, Field(min_length=1, max_length=2048)]
    byte_size: Count
    sha256: Digest


class ImageOwner(Value):
    kind: OwnerKind
    id: UUID


class ImageSourceSelector(Value):
    task_id: UUID | None = None
    dataset_item_id: UUID | None = None

    @model_validator(mode="after")
    def exactly_one_source(self):
        if (self.task_id is None) == (self.dataset_item_id is None):
            raise ValueError("select exactly one image source")
        return self


class ActorImageSourceRequest(ImageSourceSelector):
    actor_id: UUID


class ImageSource(Value):
    """Current source facts plus separate freshness facts for derived media."""

    owner: ImageOwner
    task_id: UUID | None
    project_id: UUID | None
    dataset_id: UUID | None
    item_id: UUID | None
    source_generation: Revision
    source_snapshot: Digest
    object_identity: ObjectIdentity | None
    source_drifted: StrictBool
    upload_ready: StrictBool
    file_path: Annotated[StrictStr, Field(min_length=1, max_length=1024)]
    file_name: Annotated[StrictStr, Field(min_length=1, max_length=512)]
    file_type: Literal["image"]
    bucket_kind: BucketKind
    file_size: Count | None
    content_hash: Annotated[StrictStr, Field(max_length=255)] | None
    width: Positive | None
    height: Positive | None
    thumbnail_path: Annotated[StrictStr, Field(min_length=1, max_length=1024)] | None
    blurhash: Annotated[StrictStr, Field(max_length=128)] | None
    thumbnail_source_generation: Revision | None
    thumbnail_error: Annotated[StrictStr, Field(max_length=2000)] | None

    @field_validator("file_path", "thumbnail_path")
    @classmethod
    def relative_object_key(cls, value):
        if value is None:
            return value
        if (
            value.startswith("/")
            or "\\" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
        ):
            raise ValueError("image object path must be a normalized relative key")
        return value

    @model_validator(mode="after")
    def owner_projection_is_consistent(self):
        if self.owner.kind == "task":
            if (
                self.bucket_kind != "legacy"
                or self.owner.id != self.task_id
                or self.dataset_id is not None
                or self.item_id is not None
            ):
                raise ValueError("legacy image source has an invalid Task projection")
        elif (
            self.bucket_kind != "datasets"
            or self.owner.id != self.item_id
            or self.dataset_id is None
        ):
            raise ValueError("DatasetItem image source has an invalid owner projection")
        return self

    @property
    def thumbnail_current(self) -> bool:
        return (
            self.thumbnail_path is not None
            and self.thumbnail_source_generation == self.source_generation
        )


class TaskImageSourceFacts(Value):
    """Exact private source facts returned by the Labeling owner."""

    task_id: UUID
    project_id: UUID | None
    dataset_item_id: UUID | None
    file_type: StrictStr
    file_path: Annotated[StrictStr, Field(min_length=1, max_length=1024)]
    file_name: Annotated[StrictStr, Field(min_length=1, max_length=512)]
    created_at: AwareDatetime
    source_generation: Revision
    source_snapshot: Digest
    object_identity: ObjectIdentity | None
    source_drifted: StrictBool
    upload_ready: StrictBool
    thumbnail_path: Annotated[StrictStr, Field(min_length=1, max_length=1024)] | None
    blurhash: Annotated[StrictStr, Field(max_length=128)] | None
    thumbnail_source_generation: Revision | None

    @field_validator("file_path", "thumbnail_path")
    @classmethod
    def relative_object_key(cls, value):
        if value is None:
            return value
        if (
            value.startswith("/")
            or "\\" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
        ):
            raise ValueError("image object path must be a normalized relative key")
        return value


class ReadImageSourceRequest(ActorImageSourceRequest):
    pass


class ObserveImageSourceRequest(ActorImageSourceRequest):
    source_snapshot: Digest
    object_identity: ObjectIdentity


class InternalImageSourceRequest(Value):
    owner: ImageOwner
    expected_source_snapshot: Digest | None = None
    object_identity: ObjectIdentity | None = None

    @model_validator(mode="after")
    def paired_observation(self):
        if (self.expected_source_snapshot is None) != (self.object_identity is None):
            raise ValueError("source observation requires identity and snapshot")
        return self


class ReadImageSummariesRequest(Value):
    actor_id: UUID
    selectors: Annotated[list[ImageSourceSelector], Field(max_length=BATCH_SIZE)]

    @model_validator(mode="after")
    def unique_selectors(self):
        keys = [(item.task_id, item.dataset_item_id) for item in self.selectors]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate image source selectors")
        return self


class ImagePyramidLevel(Value):
    level: Index
    scale_factor: Positive = Field(alias="scaleFactor")
    width: Positive
    height: Positive
    columns: Positive
    rows: Positive

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
        populate_by_name=True,
    )


class ImagePyramidOverview(Value):
    width: Positive
    height: Positive
    content_digest: PyramidDigest = Field(alias="contentDigest")

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
        populate_by_name=True,
    )


class ImagePyramidManifest(Value):
    schema_: Literal[PYRAMID_SCHEMA] = Field(alias="schema")
    generation: PyramidGeneration
    source_fingerprint: PyramidDigest = Field(alias="sourceFingerprint")
    normalization_version: Literal["exif-autorotate-srgb-v1"] = Field(
        alias="normalizationVersion"
    )
    width: Positive
    height: Positive
    tile_size: Literal[PYRAMID_TILE_SIZE] = Field(alias="tileSize")
    overlap: Literal[PYRAMID_OVERLAP]
    format: Literal["webp"]
    levels: Annotated[list[ImagePyramidLevel], Field(min_length=1, max_length=32)]
    overview: ImagePyramidOverview

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
        populate_by_name=True,
    )

    @model_validator(mode="after")
    def exact_full_resolution_first_grid(self):
        expected = []
        level = 0
        while True:
            scale = 1 << level
            width = max(1, (self.width + scale - 1) // scale)
            height = max(1, (self.height + scale - 1) // scale)
            expected.append(
                (
                    level,
                    scale,
                    width,
                    height,
                    (width + 511) // 512,
                    (height + 511) // 512,
                )
            )
            if width == height == 1:
                break
            level += 1
        actual = [
            (row.level, row.scale_factor, row.width, row.height, row.columns, row.rows)
            for row in self.levels
        ]
        if actual != expected:
            raise ValueError("pyramid manifest grid is not canonical")
        if self.overview.width > self.width or self.overview.height > self.height:
            raise ValueError("pyramid overview exceeds the normalized source")
        return self


class ImagePyramidSummary(Value):
    status: PyramidStatus
    required: StrictBool
    generation: PyramidGeneration | None
    building_generation: PyramidGeneration | None
    building_status: Literal["pending", "building"] | None
    width: Positive | None
    height: Positive | None
    tile_size: Literal[PYRAMID_TILE_SIZE] = PYRAMID_TILE_SIZE
    format: Literal["webp"] | None
    retryable: StrictBool = False
    retry_after_ms: Count | None = None
    error_code: Annotated[StrictStr, Field(max_length=64)] | None = None


class ImagePyramidGeneration(Value):
    """Trusted native projection. Object keys never leave the control boundary."""

    asset_id: UUID | None
    status: PyramidStatus
    required: StrictBool
    generation: PyramidGeneration | None
    building_generation: PyramidGeneration | None
    building_status: Literal["pending", "building"] | None
    source_generation: Revision | None = None
    source_snapshot: Digest | None = None
    width: Positive | None
    height: Positive | None
    tile_size: Literal[PYRAMID_TILE_SIZE] = PYRAMID_TILE_SIZE
    format: Literal["webp"] | None
    retryable: StrictBool = False
    retry_after_ms: Count | None = None
    error_code: Annotated[StrictStr, Field(max_length=64)] | None = None
    manifest: ImagePyramidManifest | None
    manifest_key: Annotated[StrictStr, Field(min_length=1, max_length=2048)] | None
    manifest_digest: PyramidDigest | None
    overview_key: Annotated[StrictStr, Field(min_length=1, max_length=2048)] | None
    overview_digest: PyramidDigest | None
    tile_count: Count
    retained_bytes: Count

    @model_validator(mode="after")
    def ready_generation_is_complete(self):
        if self.status == "ready" and (
            self.asset_id is None
            or self.generation is None
            or self.source_generation is None
            or self.source_snapshot is None
            or self.manifest is None
            or self.manifest.generation != self.generation
            or self.manifest_key is None
            or self.manifest_digest is None
            or self.overview_key is None
            or self.overview_digest is None
        ):
            raise ValueError("ready pyramid generation is incomplete")
        if self.manifest is not None and (
            self.width != self.manifest.width or self.height != self.manifest.height
        ):
            raise ValueError("pyramid dimensions disagree with its manifest")
        if self.manifest is not None and (
            self.overview_digest != self.manifest.overview.content_digest
        ):
            raise ValueError("pyramid overview digest disagrees with its manifest")
        if (self.source_generation is None) != (self.source_snapshot is None):
            raise ValueError("pyramid source facts must be paired")
        if self.generation is None and (
            self.source_generation is not None or self.source_snapshot is not None
        ):
            raise ValueError("pyramid source facts require a generation")
        return self

    def summary(self) -> ImagePyramidSummary:
        return ImagePyramidSummary(
            status=self.status,
            required=self.required,
            generation=self.generation,
            building_generation=self.building_generation,
            building_status=self.building_status,
            width=self.width,
            height=self.height,
            tile_size=self.tile_size,
            format=self.format,
            retryable=self.retryable,
            retry_after_ms=self.retry_after_ms,
            error_code=self.error_code,
        )


class ImageSummaryItem(Value):
    selector: ImageSourceSelector
    source: ImageSource
    thumbnail_current: StrictBool
    pyramid: ImagePyramidSummary

    @model_validator(mode="after")
    def matches_selector(self):
        if not image_source_matches_selector(self.source, self.selector):
            raise ValueError("summary source escaped its selector")
        if self.thumbnail_current != self.source.thumbnail_current:
            raise ValueError("summary thumbnail freshness disagrees with source")
        return self


class ImageSummaries(Value):
    items: Annotated[list[ImageSummaryItem], Field(max_length=BATCH_SIZE)]


class ImageSourceRequest(ActorImageSourceRequest):
    pass


class ImageAcceptanceRequest(ActorImageSourceRequest):
    operation_id: UUID
    purpose: Purpose
    expected_source_snapshot: Digest | None = None
    policy: ImagePolicy = Field(default_factory=ImagePolicy)
    force: StrictBool = False


class PrepareImageMediaRequest(ImageAcceptanceRequest):
    pass


class RetryImageMediaRequest(ImageAcceptanceRequest):
    pass


class ImageInvalidation(Value):
    source_snapshot: Digest
    kind: Literal["thumbnail", "overview", "manifest", "tile"]
    generation: PyramidGeneration | None = None
    level: Index | None = None
    x: Index | None = None
    y: Index | None = None

    @model_validator(mode="after")
    def logical_target_only(self):
        tile_coordinate_present = any(
            value is not None for value in (self.level, self.x, self.y)
        )
        if self.kind == "thumbnail":
            if self.generation is not None or tile_coordinate_present:
                raise ValueError("thumbnail invalidation has a pyramid coordinate")
        elif self.generation is None:
            raise ValueError("pyramid invalidation requires a generation")
        elif self.kind == "tile":
            if any(value is None for value in (self.level, self.x, self.y)):
                raise ValueError("tile invalidation requires all logical coordinates")
        elif tile_coordinate_present:
            raise ValueError("non-tile invalidation has tile coordinates")
        return self


class InvalidateImageMediaRequest(ActorImageSourceRequest):
    invalidation: ImageInvalidation


class InvalidationResult(Value):
    source: ImageSource
    changed: StrictBool


class ReadImageGenerationRequest(ActorImageSourceRequest):
    generation: PyramidGeneration | None = None


class ImageGenerationRead(Value):
    source: ImageSource
    pyramid: ImagePyramidGeneration

    @model_validator(mode="after")
    def same_owner_source_generation(self):
        stale = (
            self.pyramid.source_generation is not None
            and self.pyramid.source_generation != self.source.source_generation
        ) or (
            self.pyramid.source_snapshot is not None
            and self.pyramid.source_snapshot != self.source.source_snapshot
        )
        if (stale or self.source.source_drifted) and self.pyramid.status == "ready":
            raise ValueError("stale pyramid generation was reported as current")
        return self


class ReadImageWorkRequest(Value):
    actor_id: UUID
    job_id: UUID


class ReadInternalImageWorkRequest(Value):
    job_id: UUID


class ImageBackfillSelection(Value):
    owner_kind: OwnerKind
    dataset_id: UUID | None = None
    project_id: UUID | None = None
    owner_ids: Annotated[list[UUID], Field(max_length=BATCH_SIZE)] = Field(
        default_factory=list
    )
    all_owners: StrictBool = False
    purpose: Purpose
    only_missing: StrictBool = True
    accepted_before: AwareDatetime | None = None

    @model_validator(mode="after")
    def one_selector(self):
        if (
            sum(
                (
                    self.dataset_id is not None,
                    self.project_id is not None,
                    bool(self.owner_ids),
                    self.all_owners,
                )
            )
            != 1
        ):
            raise ValueError("image backfill requires exactly one owner selector")
        if len(set(self.owner_ids)) != len(self.owner_ids):
            raise ValueError("duplicate image backfill owner IDs")
        if self.owner_kind == "task" and self.dataset_id is not None:
            raise ValueError("legacy Task backfill cannot select by Dataset ID")
        return self


class DimensionsWorkSpec(Value):
    kind: Literal["dimensions"] = "dimensions"
    expected_source_snapshot: Digest
    policy: ImagePolicy = Field(default_factory=ImagePolicy)


class ThumbnailWorkSpec(Value):
    kind: Literal["thumbnail"] = "thumbnail"
    expected_source_snapshot: Digest
    policy: ImagePolicy = Field(default_factory=ImagePolicy)


class PyramidWorkSpec(Value):
    kind: Literal["pyramid"] = "pyramid"
    expected_source_snapshot: Digest
    policy: ImagePolicy = Field(default_factory=ImagePolicy)


class ImageBackfillSpec(Value):
    kind: Literal["image_backfill"] = "image_backfill"
    selection: ImageBackfillSelection
    policy: ImagePolicy = Field(default_factory=ImagePolicy)


ImageWorkSpec = Annotated[
    DimensionsWorkSpec | ThumbnailWorkSpec | PyramidWorkSpec | ImageBackfillSpec,
    Field(discriminator="kind"),
]


class ImageWorkReference(Value):
    job_id: UUID
    generation: Revision
    owner: ImageOwner | None
    source_generation: Count
    kind: WorkKind
    status: Literal["pending", "running", "ready", "failed", "canceled"]


class AcceptedImageWork(Value):
    operation_id: UUID | None
    source: ImageSource | None
    work: ImageWorkReference
    spec: ImageWorkSpec


class ImageWorkStatusRequest(Value):
    job_id: UUID
    actor_id: UUID | None = None


class ImageWorkStatus(Value):
    work: ImageWorkReference
    spec: ImageWorkSpec
    progress: JsonObject
    error_code: Annotated[StrictStr, Field(max_length=64)] | None
    error_detail: Annotated[StrictStr, Field(max_length=2000)] | None
    lease_expires_at: AwareDatetime | None


class CancelImageMediaRequest(Value):
    operation_id: UUID
    actor_id: UUID
    job_id: UUID


class CancelImageMediaResult(Value):
    operation_id: UUID
    job_id: UUID
    canceled: StrictBool


class ScheduleImageMediaRequest(Value):
    operation_id: UUID | None
    owner_kind: OwnerKind | None
    owner_id: UUID | None
    spec: ImageWorkSpec
    force: StrictBool = False

    @model_validator(mode="after")
    def source_or_backfill_scope(self):
        if isinstance(self.spec, ImageBackfillSpec):
            if self.owner_kind is not None or self.owner_id is not None:
                raise ValueError("backfill work cannot be bound to one owner")
        elif self.owner_kind is None or self.owner_id is None:
            raise ValueError("source work requires an explicit owner")
        return self


class ResolveInternalImageSourceRequest(Value):
    owner_kind: OwnerKind
    owner_id: UUID
    expected_source_snapshot: Digest | None = None
    object_identity: ObjectIdentity | None = None

    @model_validator(mode="after")
    def paired_observation(self):
        if (self.expected_source_snapshot is None) != (self.object_identity is None):
            raise ValueError("source observation requires identity and snapshot")
        return self

    @property
    def owner(self) -> ImageOwner:
        return ImageOwner(kind=self.owner_kind, id=self.owner_id)


class ImageCursor(Value):
    owner_kind: OwnerKind
    owner_id: UUID


class ScanImageBackfillRequest(Value):
    selection: ImageBackfillSelection
    after_owner_kind: OwnerKind | None = None
    after_owner_id: UUID | None = None
    page_size: PageSize = BATCH_SIZE

    @model_validator(mode="after")
    def cursor_is_paired_and_scoped(self):
        if (self.after_owner_kind is None) != (self.after_owner_id is None):
            raise ValueError("backfill cursor requires owner kind and ID")
        if (
            self.after_owner_kind is not None
            and self.after_owner_kind != self.selection.owner_kind
        ):
            raise ValueError("backfill cursor changed owner kind")
        return self


class ImageBackfillPage(Value):
    items: Annotated[list[ImageSource], Field(max_length=BATCH_SIZE)]
    snapshot_digest: Digest
    total: Count
    accepted_before: AwareDatetime
    after_owner_kind: OwnerKind | None
    after_owner_id: UUID | None
    next_owner_kind: OwnerKind | None
    next_owner_id: UUID | None
    complete: StrictBool


class ImageUsageRequest(Value):
    pass


class ImageUsage(Value):
    source_bytes: Count
    thumbnail_bytes: Count
    pyramid_bytes: Count
    pending: Count
    running: Count
    failed: Count
    garbage_pending: Count


class ClaimImageMediaRequest(Value):
    worker_id: UUID
    job_id: UUID | None = None
    lease_seconds: Annotated[StrictInt, Field(ge=30, le=2_147_483_647)] = 1200
    kinds: Annotated[list[WorkKind], Field(min_length=1, max_length=4)] = Field(
        default_factory=lambda: ["dimensions", "thumbnail", "pyramid", "image_backfill"]
    )


class ImageClaim(Value):
    job_id: UUID
    generation: Revision
    lease_token: UUID
    lease_expires_at: AwareDatetime
    owner: ImageOwner | None
    source: ImageSource | None
    spec: ImageWorkSpec
    output_prefix: Annotated[StrictStr, Field(min_length=1, max_length=2048)] | None
    pyramid_asset_id: UUID | None
    pyramid_generation: PyramidGeneration | None

    @model_validator(mode="after")
    def require_exact_claim_scope(self):
        if isinstance(self.spec, ImageBackfillSpec):
            if any(
                (
                    self.owner,
                    self.source,
                    self.output_prefix,
                    self.pyramid_asset_id,
                    self.pyramid_generation,
                )
            ):
                raise ValueError("backfill claim has source or output scope")
            return self
        if self.owner is None or self.source is None:
            raise ValueError("image claim requires a source and owner")
        if (
            self.source.owner != self.owner
            or self.source.source_drifted
            or not self.source.upload_ready
        ):
            raise ValueError("image claim has stale or unready source facts")
        if (
            self.source.source_snapshot != self.spec.expected_source_snapshot
            or self.source.object_identity is None
        ):
            raise ValueError("image claim is not bound to an observed source")
        if isinstance(self.spec, DimensionsWorkSpec):
            if any(
                (self.output_prefix, self.pyramid_asset_id, self.pyramid_generation)
            ):
                raise ValueError("dimensions claim has output scope")
        elif isinstance(self.spec, ThumbnailWorkSpec):
            expected = (
                f"image-assets/{self.job_id}/g{self.generation}/a{self.lease_token}/"
            )
            if (
                self.output_prefix != expected
                or self.pyramid_asset_id is not None
                or self.pyramid_generation is not None
            ):
                raise ValueError("thumbnail claim escaped its attempt prefix")
        else:
            if self.pyramid_asset_id is None or self.pyramid_generation is None:
                raise ValueError(
                    "pyramid claim requires an asset and public generation"
                )
            expected = f"image-pyramids/{self.pyramid_asset_id}/g{self.pyramid_generation}/a{self.lease_token}/"
            if self.output_prefix != expected:
                raise ValueError("pyramid claim escaped its attempt prefix")
        return self


class ClaimedImageWork(Value):
    claim: ImageClaim | None


class ImageLeaseRequest(Value):
    job_id: UUID
    generation: Revision
    lease_token: UUID


class HeartbeatImageMediaRequest(ImageLeaseRequest):
    lease_seconds: Annotated[StrictInt, Field(ge=30, le=2_147_483_647)] = 1200


class ImageLeaseState(Value):
    job_id: UUID
    generation: Revision
    lease_token: UUID
    active: StrictBool
    lease_expires_at: AwareDatetime | None


class DimensionsResult(Value):
    kind: Literal["dimensions"] = "dimensions"
    source_identity: ObjectIdentity
    source_sha256: Digest | None = None
    width: Positive
    height: Positive


class ThumbnailResult(Value):
    kind: Literal["thumbnail"] = "thumbnail"
    source_identity: ObjectIdentity
    source_sha256: Digest
    width: Positive
    height: Positive
    blurhash: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    artifact: Artifact


class ImageTileArtifact(Value):
    level: Index
    x: Index
    y: Index
    artifact: Artifact


class PyramidResult(Value):
    kind: Literal["pyramid"] = "pyramid"
    source_identity: ObjectIdentity
    source_sha256: Digest
    manifest: ImagePyramidManifest
    manifest_artifact: Artifact
    overview_artifact: Artifact
    tile_artifacts: Annotated[list[ImageTileArtifact], Field(max_length=20_000)]
    tile_count: Count
    retained_bytes: Count

    @model_validator(mode="after")
    def manifest_and_artifact_totals_match(self):
        expected_count = sum(
            level.columns * level.rows for level in self.manifest.levels
        )
        if self.manifest.source_fingerprint != "sha256:" + self.source_sha256:
            raise ValueError("pyramid manifest belongs to another source digest")
        if (
            self.manifest.overview.content_digest
            != "sha256:" + self.overview_artifact.sha256
        ):
            raise ValueError("pyramid overview digest disagrees with manifest")
        if (
            self.tile_count != expected_count
            or len(self.tile_artifacts) != expected_count
        ):
            raise ValueError("pyramid tile count disagrees with canonical grid")
        if self.retained_bytes != sum(
            artifact.byte_size
            for artifact in [self.manifest_artifact, self.overview_artifact]
            + [tile.artifact for tile in self.tile_artifacts]
        ):
            raise ValueError("pyramid retained byte count disagrees with artifacts")
        coords = {(tile.level, tile.x, tile.y) for tile in self.tile_artifacts}
        if len(coords) != len(self.tile_artifacts):
            raise ValueError("duplicate pyramid tile coordinates")
        expected = {
            (level.level, x, y)
            for level in self.manifest.levels
            for y in range(level.rows)
            for x in range(level.columns)
        }
        if coords != expected:
            raise ValueError("pyramid tile coordinates do not cover the manifest")
        return self


ImageWorkResult = Annotated[
    DimensionsResult | ThumbnailResult | PyramidResult, Field(discriminator="kind")
]


class FinishImageMediaRequest(ImageLeaseRequest):
    result: ImageWorkResult


class ImageWorkOutcome(Value):
    job_id: UUID
    generation: Revision
    outcome: Literal[
        "published",
        "replayed",
        "failed",
        "lease_lost",
        "source_changed",
        "dependency_changed",
    ]


class FailImageMediaRequest(ImageLeaseRequest):
    error_code: ErrorCode
    error_detail: Annotated[StrictStr, Field(max_length=2000)]


class ImageBackfillSourceObservation(Value):
    owner: ImageOwner
    source_snapshot: Digest
    object_identity: ObjectIdentity | None
    error_code: Literal["source_missing", "source_changed"] | None

    @model_validator(mode="after")
    def exactly_one_observation_outcome(self):
        if (self.object_identity is None) == (self.error_code is None):
            raise ValueError(
                "backfill source observation requires exactly one identity or error"
            )
        return self


class ImageBackfillObservations(Value):
    batch_token: UUID
    items: Annotated[
        list[ImageBackfillSourceObservation], Field(min_length=1, max_length=BATCH_SIZE)
    ]

    @model_validator(mode="after")
    def unique_owners(self):
        owners = [item.owner for item in self.items]
        if len(owners) != len(set(owners)):
            raise ValueError("duplicate observed backfill owners")
        return self


class AdvanceImageBackfillRequest(ImageLeaseRequest):
    batch_size: PageSize = BATCH_SIZE
    observations: ImageBackfillObservations | None = None

    @model_validator(mode="after")
    def observation_batch_fits_request(self):
        if (
            self.observations is not None
            and len(self.observations.items) > self.batch_size
        ):
            raise ValueError("observed backfill batch exceeds requested batch size")
        return self


class ImageBackfillProgress(Value):
    job_id: UUID
    generation: Revision
    lease_token: UUID
    scheduled: Count
    skipped: Count
    complete: StrictBool
    next_owner_kind: OwnerKind | None
    next_owner_id: UUID | None
    batch_token: UUID | None = None
    candidates: Annotated[list[ImageSource], Field(max_length=BATCH_SIZE)] = Field(
        default_factory=list
    )
    needs_observation: StrictBool = False

    @model_validator(mode="after")
    def cursor_is_paired(self):
        if (self.next_owner_kind is None) != (self.next_owner_id is None):
            raise ValueError("backfill progress has an incomplete cursor")
        owners = [item.owner for item in self.candidates]
        if len(owners) != len(set(owners)):
            raise ValueError("duplicate backfill observation candidates")
        if self.needs_observation and (
            self.batch_token is None
            or not self.candidates
            or self.complete
            or self.scheduled != 0
            or self.skipped != 0
        ):
            raise ValueError("backfill observation preparation is incomplete")
        if not self.needs_observation and self.candidates:
            raise ValueError("backfill candidates require an observation phase")
        return self


class ImageOrphanCandidate(Value):
    asset_id: UUID
    generation: PyramidGeneration
    last_modified: AwareDatetime


class ReconcileImageMediaRequest(Value):
    candidates: Annotated[list[ImageOrphanCandidate], Field(max_length=100)] = Field(
        default_factory=list
    )
    batch_size: Annotated[StrictInt, Field(ge=1, le=BATCH_SIZE)] = 100

    @model_validator(mode="after")
    def unique_orphans(self):
        keys = [
            (candidate.asset_id, candidate.generation) for candidate in self.candidates
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate image orphan candidates")
        return self


class ImageReconcileResult(Value):
    expired_jobs: Count
    requeued_jobs: Count
    failed_jobs: Count
    canceled_jobs: Count
    retired_generations: Count
    queued_orphans: Count
    ignored_candidates: Count
    more: StrictBool


class ClaimImageGarbageRequest(Value):
    worker_id: UUID
    lease_seconds: Annotated[StrictInt, Field(ge=30, le=3600)] = 120
    batch_size: Annotated[StrictInt, Field(ge=1, le=100)] = 20


class ImageGarbageItem(Value):
    garbage_id: UUID
    lease_token: UUID
    prefix: Annotated[StrictStr, Field(min_length=1, max_length=2048)]

    @model_validator(mode="after")
    def exact_canonical_prefix(self):
        asset = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
        attempt = r"a[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
        pyramid_asset = re.fullmatch(rf"image-pyramids/({asset})/", self.prefix)
        pyramid_generation = re.fullmatch(
            rf"image-pyramids/({asset})/g([1-9][0-9]*)/", self.prefix
        )
        pyramid_attempt = re.fullmatch(
            rf"image-pyramids/({asset})/g([1-9][0-9]*)/({attempt})/", self.prefix
        )
        thumbnail_attempt = re.fullmatch(
            rf"image-assets/({asset})/g([1-9][0-9]*)/({attempt})/", self.prefix
        )
        valid = any(
            (pyramid_asset, pyramid_generation, pyramid_attempt, thumbnail_attempt)
        )
        if not valid:
            raise ValueError("image garbage prefix is not an exact canonical scope")
        return self


class ImageGarbageBatch(Value):
    items: Annotated[list[ImageGarbageItem], Field(max_length=100)]


class CompleteImageGarbageRequest(Value):
    garbage_id: UUID
    lease_token: UUID


class ImageGarbageAck(Value):
    garbage_id: UUID
    completed: StrictBool


@dataclass(frozen=True)
class NativeOperation:
    request: type[BaseModel]
    result: type[BaseModel]
    arguments: tuple[str, ...]
    sql_types: tuple[str, ...]
    role: Literal["control", "system", "worker", "cleanup"]


OPERATIONS = {
    "read_image_source": NativeOperation(
        ReadImageSourceRequest,
        ImageSource,
        ("actor_id", "task_id", "dataset_item_id"),
        ("uuid", "uuid", "uuid"),
        "control",
    ),
    "observe_image_source": NativeOperation(
        ObserveImageSourceRequest,
        ImageSource,
        (
            "actor_id",
            "task_id",
            "dataset_item_id",
            "source_snapshot",
            "object_identity",
        ),
        ("uuid", "uuid", "uuid", "text", "jsonb"),
        "control",
    ),
    "read_image_summaries": NativeOperation(
        ReadImageSummariesRequest,
        ImageSummaries,
        ("actor_id", "selectors"),
        ("uuid", "jsonb"),
        "control",
    ),
    "prepare_image_media": NativeOperation(
        PrepareImageMediaRequest,
        AcceptedImageWork,
        (
            "operation_id",
            "actor_id",
            "task_id",
            "dataset_item_id",
            "purpose",
            "expected_source_snapshot",
            "policy",
            "force",
        ),
        ("uuid", "uuid", "uuid", "uuid", "text", "text", "jsonb", "boolean"),
        "control",
    ),
    "read_image_generation": NativeOperation(
        ReadImageGenerationRequest,
        ImageGenerationRead,
        ("actor_id", "task_id", "dataset_item_id", "generation"),
        ("uuid", "uuid", "uuid", "integer"),
        "control",
    ),
    "read_image_work": NativeOperation(
        ReadImageWorkRequest,
        ImageWorkStatus,
        ("actor_id", "job_id"),
        ("uuid", "uuid"),
        "control",
    ),
    "retry_image_media": NativeOperation(
        RetryImageMediaRequest,
        AcceptedImageWork,
        (
            "operation_id",
            "actor_id",
            "task_id",
            "dataset_item_id",
            "purpose",
            "expected_source_snapshot",
            "policy",
            "force",
        ),
        ("uuid", "uuid", "uuid", "uuid", "text", "text", "jsonb", "boolean"),
        "control",
    ),
    "invalidate_image_media": NativeOperation(
        InvalidateImageMediaRequest,
        InvalidationResult,
        ("actor_id", "task_id", "dataset_item_id", "invalidation"),
        ("uuid", "uuid", "uuid", "jsonb"),
        "control",
    ),
    "cancel_image_media": NativeOperation(
        CancelImageMediaRequest,
        CancelImageMediaResult,
        ("operation_id", "actor_id", "job_id"),
        ("uuid", "uuid", "uuid"),
        "control",
    ),
    "resolve_internal_image_source": NativeOperation(
        ResolveInternalImageSourceRequest,
        ImageSource,
        ("owner_kind", "owner_id", "expected_source_snapshot", "object_identity"),
        ("text", "uuid", "text", "jsonb"),
        "system",
    ),
    "schedule_image_media": NativeOperation(
        ScheduleImageMediaRequest,
        AcceptedImageWork,
        ("operation_id", "owner_kind", "owner_id", "spec", "force"),
        ("uuid", "text", "uuid", "jsonb", "boolean"),
        "system",
    ),
    "read_internal_image_work": NativeOperation(
        ReadInternalImageWorkRequest, ImageWorkStatus, ("job_id",), ("uuid",), "system"
    ),
    "scan_image_backfill": NativeOperation(
        ScanImageBackfillRequest,
        ImageBackfillPage,
        ("selection", "after_owner_kind", "after_owner_id", "page_size"),
        ("jsonb", "text", "uuid", "integer"),
        "system",
    ),
    "read_image_usage": NativeOperation(
        ImageUsageRequest, ImageUsage, (), (), "system"
    ),
    "claim_image_media": NativeOperation(
        ClaimImageMediaRequest,
        ClaimedImageWork,
        ("worker_id", "job_id", "lease_seconds", "kinds"),
        ("uuid", "uuid", "integer", "text[]"),
        "worker",
    ),
    "heartbeat_image_media": NativeOperation(
        HeartbeatImageMediaRequest,
        ImageLeaseState,
        ("job_id", "generation", "lease_token", "lease_seconds"),
        ("uuid", "bigint", "uuid", "integer"),
        "worker",
    ),
    "finish_image_media": NativeOperation(
        FinishImageMediaRequest,
        ImageWorkOutcome,
        ("job_id", "generation", "lease_token", "result"),
        ("uuid", "bigint", "uuid", "jsonb"),
        "worker",
    ),
    "fail_image_media": NativeOperation(
        FailImageMediaRequest,
        ImageWorkOutcome,
        ("job_id", "generation", "lease_token", "error_code", "error_detail"),
        ("uuid", "bigint", "uuid", "text", "text"),
        "worker",
    ),
    "advance_image_backfill": NativeOperation(
        AdvanceImageBackfillRequest,
        ImageBackfillProgress,
        ("job_id", "generation", "lease_token", "batch_size", "observations"),
        ("uuid", "bigint", "uuid", "integer", "jsonb"),
        "worker",
    ),
    "reconcile_image_media": NativeOperation(
        ReconcileImageMediaRequest,
        ImageReconcileResult,
        ("candidates", "batch_size"),
        ("jsonb", "integer"),
        "worker",
    ),
    "claim_image_garbage": NativeOperation(
        ClaimImageGarbageRequest,
        ImageGarbageBatch,
        ("worker_id", "lease_seconds", "batch_size"),
        ("uuid", "integer", "integer"),
        "cleanup",
    ),
    "complete_image_garbage": NativeOperation(
        CompleteImageGarbageRequest,
        ImageGarbageAck,
        ("garbage_id", "lease_token"),
        ("uuid", "uuid"),
        "cleanup",
    ),
}


class ImageFailure(Exception):
    def __init__(self, code: ErrorCode):
        self.code = code
        super().__init__(code)


class _Error(Value):
    code: ErrorCode


_Result = TypeVar("_Result", bound=BaseModel)


def json_bytes(value: Any) -> bytes:
    def default(item):
        if isinstance(item, BaseModel):
            return item.model_dump(mode="json", by_alias=True)
        if isinstance(item, UUID):
            return str(item)
        raise TypeError("not an image JSON value")

    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, default=default
    ).encode("utf-8")


def _reject_nonfinite(_value):
    raise ValueError("nonfinite JSON")


def decode_reply(raw: Any, result_type: type[_Result]) -> _Result:
    try:
        if isinstance(raw, (str, bytes)):
            if len(raw.encode("utf-8") if isinstance(raw, str) else raw) > REPLY_BYTES:
                raise ValueError("oversized reply")
            raw = json.loads(raw, parse_constant=_reject_nonfinite)
        elif len(json_bytes(raw)) > REPLY_BYTES:
            raise ValueError("oversized reply")
        if (
            not isinstance(raw, dict)
            or raw.get("schema") != IMAGE_SCHEMA
            or type(raw.get("ok")) is not bool
        ):
            raise ValueError("invalid image envelope")
        if not raw["ok"]:
            if set(raw) != {"schema", "ok", "error"}:
                raise ValueError("invalid image failure")
            raise ImageFailure(_Error.model_validate(raw["error"]).code)
        if set(raw) != {"schema", "ok", "data"}:
            raise ValueError("invalid image success")
        return result_type.model_validate(raw["data"])
    except ImageFailure:
        raise
    except (ValueError, TypeError, UnicodeError, AttributeError, RecursionError):
        raise ImageFailure("invalid_response") from None


def image_source_matches_selector(
    source: ImageSource, selector: ImageSourceSelector
) -> bool:
    if selector.dataset_item_id is not None:
        return (
            source.owner.kind == "dataset_item"
            and source.owner.id == selector.dataset_item_id
            and source.item_id == selector.dataset_item_id
            and source.task_id is None
        )
    return source.task_id == selector.task_id and (
        (source.owner.kind == "task" and source.owner.id == selector.task_id)
        or (source.owner.kind == "dataset_item" and source.item_id is not None)
    )


def _source_for(result: BaseModel) -> ImageSource | None:
    if isinstance(result, ImageSource):
        return result
    source = getattr(result, "source", None)
    return source if isinstance(source, ImageSource) else None


def require_scope(request: BaseModel, result: BaseModel) -> None:
    if isinstance(request, ImageSourceSelector):
        source = _source_for(result)
        if source is None or not image_source_matches_selector(source, request):
            raise ValueError("image source escaped its selector")
    if isinstance(
        request, (InternalImageSourceRequest, ResolveInternalImageSourceRequest)
    ):
        source = _source_for(result)
        if source is None or source.owner != request.owner:
            raise ValueError("internal image source escaped its owner")
    if isinstance(request, ReadImageSummariesRequest) and isinstance(
        result, ImageSummaries
    ):
        requested = {(row.task_id, row.dataset_item_id) for row in request.selectors}
        actual = [
            (row.selector.task_id, row.selector.dataset_item_id) for row in result.items
        ]
        if len(actual) != len(set(actual)) or not set(actual).issubset(requested):
            raise ValueError("image summaries escaped requested selectors")
        by_key = {
            (row.selector.task_id, row.selector.dataset_item_id): row
            for row in result.items
        }
        if any(
            not image_source_matches_selector(by_key[key].source, by_key[key].selector)
            for key in actual
        ):
            raise ValueError("image summary source escaped its selector")
    for field in ("operation_id", "job_id", "generation", "lease_token", "garbage_id"):
        if (
            hasattr(request, field)
            and hasattr(result, field)
            and getattr(request, field) != getattr(result, field)
        ):
            raise ValueError("image result escaped its request")
    if isinstance(request, ImageAcceptanceRequest) and isinstance(
        result, AcceptedImageWork
    ):
        if result.operation_id != request.operation_id:
            raise ValueError("accepted image work changed its operation ID")
        if result.source is None or not image_source_matches_selector(
            result.source, request
        ):
            raise ValueError("accepted image work changed its source")
        if (
            not result.source.upload_ready
            or result.source.source_drifted
            or result.work.owner != result.source.owner
            or result.work.source_generation != result.source.source_generation
        ):
            raise ValueError("accepted image work is not bound to its ready owner")
        if (
            request.expected_source_snapshot is not None
            and result.source.source_snapshot != request.expected_source_snapshot
        ):
            raise ValueError("accepted image work changed its source snapshot")
        if result.work.kind != request.purpose or result.spec.kind != request.purpose:
            raise ValueError("accepted image work changed its purpose")
        if result.spec.policy != request.policy:
            raise ValueError("accepted image work changed its captured policy")
        expected = getattr(result.spec, "expected_source_snapshot", None)
        if expected != result.source.source_snapshot:
            raise ValueError("accepted image work did not capture its source snapshot")
    if isinstance(request, ScheduleImageMediaRequest) and isinstance(
        result, AcceptedImageWork
    ):
        if result.operation_id != request.operation_id:
            raise ValueError("scheduled image work changed its operation ID")
        if request.owner_kind is None:
            if (
                result.source is not None
                or result.work.owner is not None
                or result.work.kind != "image_backfill"
            ):
                raise ValueError("scheduled image backfill acquired one source")
        elif (
            result.source is None
            or result.source.owner
            != ImageOwner(kind=request.owner_kind, id=request.owner_id)
            or result.work.owner != result.source.owner
            or result.work.source_generation != result.source.source_generation
            or not result.source.upload_ready
            or result.source.source_drifted
            or result.source.object_identity is None
            or result.source.source_snapshot != request.spec.expected_source_snapshot
        ):
            raise ValueError("scheduled image work changed its ready source")
        expected_spec = request.spec.model_dump(mode="json")
        accepted_spec = result.spec.model_dump(mode="json")
        if isinstance(request.spec, ImageBackfillSpec):
            if request.spec.selection.accepted_before is None:
                if result.spec.selection.accepted_before is None:
                    raise ValueError("image backfill acceptance omitted its cutoff")
                expected_spec["selection"]["accepted_before"] = accepted_spec[
                    "selection"
                ]["accepted_before"]
        if expected_spec != accepted_spec or result.work.kind != result.spec.kind:
            raise ValueError("scheduled image work changed its accepted spec")
    if isinstance(
        request, (ReadImageWorkRequest, ReadInternalImageWorkRequest)
    ) and isinstance(result, ImageWorkStatus):
        if result.work.job_id != request.job_id:
            raise ValueError("image work status substituted another job")
    if (
        isinstance(request, ClaimImageMediaRequest)
        and isinstance(result, ClaimedImageWork)
        and result.claim is not None
    ):
        if request.job_id is not None and request.job_id != result.claim.job_id:
            raise ValueError("image claim substituted another job")
        if result.claim.spec.kind not in request.kinds:
            raise ValueError("image claim exceeded worker capabilities")
    if isinstance(result, ImageGenerationRead) and isinstance(
        request, ReadImageGenerationRequest
    ):
        if (
            request.generation is not None
            and result.pyramid.generation != request.generation
        ):
            raise ValueError("image generation read substituted another generation")
    if isinstance(request, ScanImageBackfillRequest) and isinstance(
        result, ImageBackfillPage
    ):
        keys = [(item.owner.kind, item.owner.id.int) for item in result.items]
        cursor = (
            None
            if request.after_owner_id is None
            else (request.after_owner_kind, request.after_owner_id.int)
        )
        selection = request.selection
        if selection.dataset_id is not None:
            escaped = any(
                item.dataset_id != selection.dataset_id for item in result.items
            )
        elif selection.project_id is not None:
            escaped = any(
                item.project_id != selection.project_id for item in result.items
            )
        elif selection.owner_ids:
            wanted_ids = set(selection.owner_ids)
            escaped = any(item.owner.id not in wanted_ids for item in result.items)
        else:
            escaped = False
        if (
            result.after_owner_kind != request.after_owner_kind
            or result.after_owner_id != request.after_owner_id
            or len(result.items) > request.page_size
            or result.total < len(result.items)
            or (
                selection.accepted_before is not None
                and result.accepted_before != selection.accepted_before
            )
            or keys != sorted(set(keys))
            or any(kind != request.selection.owner_kind for kind, _ in keys)
            or (cursor is not None and any(key <= cursor for key in keys))
            or escaped
            or result.complete != (result.next_owner_id is None)
            or (result.next_owner_id is None) != (result.next_owner_kind is None)
            or (
                not result.complete
                and (
                    not keys
                    or (result.next_owner_kind, result.next_owner_id.int) != keys[-1]
                )
            )
        ):
            raise ValueError("invalid image backfill page order or progress")
    if isinstance(request, AdvanceImageBackfillRequest) and isinstance(
        result, ImageBackfillProgress
    ):
        if (result.job_id, result.generation, result.lease_token) != (
            request.job_id,
            request.generation,
            request.lease_token,
        ):
            raise ValueError("image backfill progress escaped its lease")
        if len(result.candidates) > request.batch_size:
            raise ValueError("image backfill candidates exceeded the requested batch")
        if request.observations is None:
            if result.needs_observation:
                if result.scheduled != 0 or result.skipped != 0:
                    raise ValueError("backfill advanced before source observation")
            elif result.batch_token is not None or result.complete != (
                result.next_owner_id is None
            ):
                raise ValueError("unobserved backfill progress has invalid state")
        elif (
            result.needs_observation
            or result.candidates
            or result.batch_token != request.observations.batch_token
            or result.scheduled + result.skipped != len(request.observations.items)
            or result.scheduled + result.skipped > request.batch_size
            or result.complete != (result.next_owner_id is None)
        ):
            raise ValueError("backfill observation commit has invalid progress")
    if isinstance(request, ReconcileImageMediaRequest) and isinstance(
        result, ImageReconcileResult
    ):
        if result.queued_orphans + result.ignored_candidates > len(request.candidates):
            raise ValueError("image reconcile exceeded candidate batch")
    if isinstance(request, CompleteImageGarbageRequest) and isinstance(
        result, ImageGarbageAck
    ):
        if result.garbage_id != request.garbage_id:
            raise ValueError("image garbage ack substituted another row")


def require_result_scope(claim: ImageClaim, result: ImageWorkResult) -> None:
    if claim.spec.kind != result.kind:
        raise ValueError("image worker result changed the accepted operation")
    if claim.source is None or claim.source.object_identity is None:
        raise ValueError("image claim has no observed source identity")
    if result.source_identity != claim.source.object_identity:
        raise ValueError("image result changed its source identity")
    if isinstance(result, DimensionsResult):
        return
    if claim.output_prefix is None:
        raise ValueError("image result has no leased output scope")
    if isinstance(result, ThumbnailResult):
        if (
            result.source_sha256 == ""
            or result.artifact.key != claim.output_prefix + "thumbnail.webp"
        ):
            raise ValueError("thumbnail artifact escaped its attempt prefix")
        return
    if (
        claim.pyramid_generation is None
        or result.manifest.generation != claim.pyramid_generation
    ):
        raise ValueError("pyramid result changed its public generation")
    expected_prefix = claim.output_prefix
    if (
        result.manifest_artifact.key != expected_prefix + "manifest.json"
        or result.overview_artifact.key != expected_prefix + "overview.webp"
        or any(
            tile.artifact.key
            != expected_prefix + f"tiles/{tile.level}/{tile.x}/{tile.y}.webp"
            for tile in result.tile_artifacts
        )
    ):
        raise ValueError("pyramid artifact escaped its attempt prefix")
    if "sha256:" + result.source_sha256 != result.manifest.source_fingerprint:
        raise ValueError("pyramid result changed its source digest")


def contract_document() -> dict[str, Any]:
    definitions: dict[str, Any] = {}
    models = [
        model
        for operation in OPERATIONS.values()
        for model in (operation.request, operation.result)
    ]
    models.append(TaskImageSourceFacts)
    for model in models:
        schema = model.model_json_schema()
        for key, value in schema.pop("$defs", {}).items():
            if key in definitions and definitions[key] != value:
                raise ValueError("conflicting image schema definitions")
            definitions[key] = value
        if model.__name__ in definitions and definitions[model.__name__] != schema:
            raise ValueError("conflicting image schema definition")
        definitions[model.__name__] = schema
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": IMAGE_SCHEMA,
        "$defs": definitions,
        "x-budgets": {
            "request_bytes": REQUEST_BYTES,
            "reply_bytes": REPLY_BYTES,
            "page_bytes": PAGE_BYTES,
            "batch_size": BATCH_SIZE,
            "tile_reference_max": TILE_REFERENCE_MAX,
        },
        "x-native": {
            name: {
                "signature": "aap_assets_v1."
                + name
                + "("
                + ",".join(operation.sql_types)
                + ")",
                "arguments": list(operation.arguments),
                "request": operation.request.__name__,
                "result": operation.result.__name__,
                "role": operation.role,
            }
            for name, operation in OPERATIONS.items()
        },
    }
