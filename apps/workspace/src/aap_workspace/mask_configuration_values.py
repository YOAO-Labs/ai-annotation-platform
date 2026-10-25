"""Portable Mask QC configuration value models."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MASK_QC_RULE_CODES = frozenset(
    {
        "empty_mask",
        "near_empty_mask",
        "touches_border",
        "small_island",
        "small_hole",
        "narrow_bridge",
        "boundary_noise",
        "derived_geometry_mismatch",
        "same_class_overlap",
        "cross_class_overlap",
        "flicker",
        "drift",
    }
)
MaskQCSeverity = Literal["info", "warning", "blocker"]
MaskQCSeverityOverride = Literal["info", "warning", "blocker", "off"]
MaskCompareBaseline = Literal[
    "previous_version",
    "tracker_candidate",
    "ai_candidate",
    "neighbor_keyframe",
]


class MaskQCSingleFrameConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    near_empty_pixels: int = Field(default=16, ge=1)
    small_component_pixels: int = Field(default=32, ge=1)
    small_component_ratio: float = Field(default=0.005, ge=0, le=1)
    small_hole_pixels: int = Field(default=32, ge=1)
    narrow_bridge_width: int = Field(default=2, ge=1, le=16)
    boundary_noise_ratio: float = Field(default=0.15, ge=0, le=1)
    bbox_iou_min: float = Field(default=0.98, ge=0, le=1)
    overlap_pixels: int = Field(default=1, ge=1)


class MaskQCTemporalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sample_step: int = Field(default=1, ge=1)
    iou_drop: float = Field(default=0.35, ge=0, le=1)
    centroid_shift_diagonal: float = Field(default=0.15, ge=0, le=1)
    area_change_ratio: float = Field(default=0.50, ge=0)
    component_delta: int = Field(default=2, ge=0)
    flicker_max_frames: int = Field(default=2, ge=1)
    drift_min_consecutive: int = Field(default=3, ge=1)


class MaskQCConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    config_revision: int = Field(default=1, ge=1)
    enabled: bool = True
    blocking: bool = False
    auto_run: Literal["off", "manual", "on_review_submit"] = "on_review_submit"
    single_frame: MaskQCSingleFrameConfig = Field(
        default_factory=MaskQCSingleFrameConfig
    )
    temporal: MaskQCTemporalConfig = Field(default_factory=MaskQCTemporalConfig)
    severity_overrides: dict[str, MaskQCSeverityOverride] = Field(default_factory=dict)

    @field_validator("severity_overrides")
    @classmethod
    def _known_rule_codes(
        cls, value: dict[str, MaskQCSeverityOverride]
    ) -> dict[str, MaskQCSeverityOverride]:
        unknown = sorted(set(value) - MASK_QC_RULE_CODES)
        if unknown:
            raise ValueError(f"unknown Mask QC rule code: {', '.join(unknown)}")
        return value
