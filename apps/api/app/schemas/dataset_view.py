"""Bounded sample queries shared by dynamic personal views and item pagination."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.dataset import DatasetAvailabilityState


class DatasetItemFilters(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    search: str | None = Field(None, max_length=200)
    source_id: UUID | None = None
    scene_id: UUID | None = None
    availability: DatasetAvailabilityState | None = None
    media_type: Literal["image", "video", "point_cloud", "other"] | None = None
    extension: str | None = Field(None, max_length=20, pattern=r"^\.?[a-zA-Z0-9]+$")
    dimension_state: Literal["known", "unknown"] | None = None
    min_width: int | None = Field(None, ge=1, le=10_000_000)
    max_width: int | None = Field(None, ge=1, le=10_000_000)
    min_height: int | None = Field(None, ge=1, le=10_000_000)
    max_height: int | None = Field(None, ge=1, le=10_000_000)

    @field_validator("extension")
    @classmethod
    def normalize_extension(cls, value):
        if value is None:
            return value
        value = value.lower().lstrip(".")
        return "jpg" if value == "jpeg" else value

    @model_validator(mode="after")
    def validate_dimensions(self):
        for axis in ("width", "height"):
            lower, upper = getattr(self, f"min_{axis}"), getattr(self, f"max_{axis}")
            if lower is not None and upper is not None and lower > upper:
                raise ValueError(f"min_{axis} must not exceed max_{axis}")
        if self.dimension_state == "unknown" and any(
            getattr(self, key) is not None
            for key in ("min_width", "max_width", "min_height", "max_height")
        ):
            raise ValueError("Unknown dimensions cannot be combined with size ranges")
        return self


class DatasetItemQuery(DatasetItemFilters):
    # Existing item callers may include unrelated parameters; saved queries stay strict.
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    limit: int = Field(50, ge=1, le=200)
    offset: int = Field(0, ge=0)


class DatasetSavedViewWrite(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=120)
    query: DatasetItemFilters = Field(default_factory=DatasetItemFilters)
    query_schema_version: Literal[1] = 1


class DatasetSavedViewOut(DatasetSavedViewWrite):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    dataset_id: UUID
    created_at: datetime
    updated_at: datetime


class DatasetSavedViewList(BaseModel):
    items: list[DatasetSavedViewOut]
