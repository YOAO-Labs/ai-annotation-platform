"""Frozen, language-neutral row contract for the Assets cleanup capability."""

from __future__ import annotations

from datetime import datetime
import re
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    model_validator,
)

BUCKET_PATTERN = (
    r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$"
)
IP_BUCKET_PATTERN = r"^[0-9]+(\.[0-9]+){3}$"
_IMAGE_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
IMAGE_GARBAGE_PREFIX_PATTERN = (
    rf"^(?:image-pyramids/{_IMAGE_UUID}/|"
    rf"image-assets/{_IMAGE_UUID}/g[1-9][0-9]*/a{_IMAGE_UUID}/|"
    rf"image-pyramids/{_IMAGE_UUID}/g[1-9][0-9]*/|"
    rf"image-pyramids/{_IMAGE_UUID}/g[1-9][0-9]*/a{_IMAGE_UUID}/)$"
)


def _non_ip_bucket(value: str) -> str:
    if re.fullmatch(IP_BUCKET_PATTERN, value):
        raise ValueError("Storage bucket must not be an IP address")
    return value


CleanupBucket = Annotated[
    str,
    Field(
        min_length=3,
        max_length=63,
        pattern=BUCKET_PATTERN,
        json_schema_extra={"not": {"pattern": IP_BUCKET_PATTERN}},
    ),
    AfterValidator(_non_ip_bucket),
]


class AssetStorageDeletion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: UUID
    asset_id: UUID
    bucket: CleanupBucket
    prefix: str

    @model_validator(mode="after")
    def check_asset_prefix(self) -> AssetStorageDeletion:
        if self.prefix != f"image-pyramids/{self.asset_id}/":
            raise ValueError("Storage deletion is outside its frozen asset prefix")
        return self


class PendingAssetStorageDeletion(AssetStorageDeletion):
    created_at: datetime

    @model_validator(mode="after")
    def check_created_at(self) -> PendingAssetStorageDeletion:
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("Storage deletion cursor timestamp must be timezone-aware")
        return self


class DeletionCursor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    created_at: datetime
    operation_id: UUID
    asset_id: UUID

    @model_validator(mode="after")
    def check_created_at(self) -> DeletionCursor:
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("Storage deletion cursor timestamp must be timezone-aware")
        return self

    @classmethod
    def after(cls, row: PendingAssetStorageDeletion) -> DeletionCursor:
        return cls(
            created_at=row.created_at,
            operation_id=row.operation_id,
            asset_id=row.asset_id,
        )


class ImageGarbageClaim(BaseModel):
    """One exact image prefix and lease token returned by the image owner."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    garbage_id: UUID
    prefix: Annotated[
        StrictStr,
        Field(min_length=1, max_length=512, pattern=IMAGE_GARBAGE_PREFIX_PATTERN),
    ]
    lease_token: UUID


class ImageGarbageClaimData(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[ImageGarbageClaim] = Field(max_length=500)


class ImageGarbageCompleteData(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    garbage_id: UUID
    completed: StrictBool


class ImageGarbageError(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: Annotated[StrictStr, Field(min_length=1, max_length=64)]


class ImageGarbageEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_name: Literal["aap-assets-images/v1"] = Field(alias="schema")
    ok: StrictBool
    data: dict | None = None
    error: ImageGarbageError | None = None

    @model_validator(mode="after")
    def check_reply_shape(self) -> ImageGarbageEnvelope:
        if self.ok:
            if self.data is None or self.error is not None:
                raise ValueError("Successful image reply must contain only data")
        elif self.error is None or self.data is not None:
            raise ValueError("Failed image reply must contain only error")
        return self
