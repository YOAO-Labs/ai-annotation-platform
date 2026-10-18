"""Frozen, language-neutral row contract for the Assets cleanup capability."""

from __future__ import annotations

from datetime import datetime
import re
from typing import Annotated
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

BUCKET_PATTERN = (
    r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$"
)
IP_BUCKET_PATTERN = r"^[0-9]+(\.[0-9]+){3}$"


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
