"""Frozen exact storage references owned by Assets cleanup."""

from typing import Annotated
import re
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
    def check_asset_prefix(self):
        if self.prefix != f"image-pyramids/{self.asset_id}/":
            raise ValueError("Storage deletion is outside its frozen asset prefix")
        return self
