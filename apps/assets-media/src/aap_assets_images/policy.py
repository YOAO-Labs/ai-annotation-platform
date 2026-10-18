"""Image policy captured when durable materialization is accepted."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    model_validator,
)

Positive = Annotated[StrictInt, Field(gt=0, le=9_223_372_036_854_775_807)]
IndexLimit = Annotated[StrictInt, Field(gt=0, le=2_147_483_647)]


class ImagePolicy(BaseModel):
    """Keep existing output semantics and freeze resource limits per operation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_version: Annotated[str, Field(min_length=1, max_length=64)] = "pyramid-v1"
    normalization_version: Literal["exif-autorotate-srgb-v1"] = (
        "exif-autorotate-srgb-v1"
    )
    auto_generate_pyramids: StrictBool = False
    optional_pixels: Positive = 16_777_216
    required_pixels: Positive = 50_000_000
    max_pixels: Positive = 300_000_000
    max_dimension: IndexLimit = 32_768
    max_tiles: IndexLimit = 20_000
    max_source_bytes: Positive = 8 * 1024**3
    max_derived_bytes: Positive = 8 * 1024**3
    max_temp_bytes: Positive = 12 * 1024**3
    job_timeout_seconds: IndexLimit = 1_800
    lease_seconds: IndexLimit = 2_100
    retry_cooldown_seconds: Annotated[StrictInt, Field(ge=0, le=2_147_483_647)] = 300
    gc_grace_hours: Annotated[StrictInt, Field(ge=0, le=2_147_483_647)] = 24
    url_expiry_seconds: IndexLimit = 900
    asset_url_batch_max: IndexLimit = 128
    vips_concurrency: IndexLimit = 4

    @model_validator(mode="after")
    def validate_resource_order(self):
        if not self.optional_pixels <= self.required_pixels <= self.max_pixels:
            raise ValueError("image thresholds must fit within the pixel limit")
        if self.lease_seconds <= self.job_timeout_seconds + 60:
            raise ValueError("image lease must outlast the writer and shutdown grace")
        return self

    def pyramid_eligible(self, width: int | None, height: int | None) -> bool:
        return (
            width is not None
            and height is not None
            and width > 0
            and height > 0
            and width * height >= self.optional_pixels
        )

    def pyramid_required(self, width: int | None, height: int | None) -> bool:
        return (
            width is not None
            and height is not None
            and width > 0
            and height > 0
            and width * height >= self.required_pixels
        )
