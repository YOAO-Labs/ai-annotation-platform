"""Shared strict request and additive readback models for point-cloud settings.

This module intentionally depends only on Pydantic and the standard library so
project response models can import it without creating a package-model cycle.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PointCloudQualityRuleCode = Literal[
    "low_point_count",
    "size_outlier",
    "ground_clearance",
    "temporal_jump",
    "track_gap",
    "track_identity_drift",
    "duplicate_track_member",
    "projection_residual",
]
_RULE_CODES = frozenset(PointCloudQualityRuleCode.__args__)


class _ThresholdConfigFields(BaseModel):
    minimum_points: int = Field(default=5, ge=0)
    ground_sample_min: int = Field(default=24, ge=3)
    ground_margin_m: float = Field(default=0.75, ge=0, le=10)
    ground_penetration_m: float = Field(default=0.2, gt=0, le=5)
    ground_float_m: float = Field(default=0.45, gt=0, le=10)
    size_min_samples: int = Field(default=8, ge=3)
    size_mad_z: float = Field(default=4.5, gt=0, le=20)
    temporal_center_jump_m: float = Field(default=4.0, gt=0, le=100)
    temporal_size_change_ratio: float = Field(default=0.6, gt=0, le=10)
    temporal_yaw_jump_rad: float = Field(default=0.8, gt=0, le=3.142)
    projection_min_iou: float = Field(default=0.5, ge=0, le=1)
    projection_max_edge_residual_ratio: float = Field(default=0.025, gt=0, le=1)


class PointCloudQualityThresholdConfig(_ThresholdConfigFields):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class PointCloudQualityThresholdConfigRead(_ThresholdConfigFields):
    model_config = ConfigDict(extra="allow", allow_inf_nan=False)


class _ThresholdOverrideFields(BaseModel):
    minimum_points: int | None = Field(default=None, ge=0)
    ground_sample_min: int | None = Field(default=None, ge=3)
    ground_margin_m: float | None = Field(default=None, ge=0, le=10)
    ground_penetration_m: float | None = Field(default=None, gt=0, le=5)
    ground_float_m: float | None = Field(default=None, gt=0, le=10)
    size_min_samples: int | None = Field(default=None, ge=3)
    size_mad_z: float | None = Field(default=None, gt=0, le=20)
    temporal_center_jump_m: float | None = Field(default=None, gt=0, le=100)
    temporal_size_change_ratio: float | None = Field(default=None, gt=0, le=10)
    temporal_yaw_jump_rad: float | None = Field(default=None, gt=0, le=3.142)
    projection_min_iou: float | None = Field(default=None, ge=0, le=1)
    projection_max_edge_residual_ratio: float | None = Field(default=None, gt=0, le=1)


class PointCloudQualityThresholdOverride(_ThresholdOverrideFields):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class PointCloudQualityThresholdOverrideRead(_ThresholdOverrideFields):
    model_config = ConfigDict(extra="allow", allow_inf_nan=False)


class _GovernanceConfigFields(BaseModel):
    minimum_reviewed_per_rule: int = Field(default=20, ge=1, le=10_000)
    maximum_false_positive_rate: float = Field(default=0.1, ge=0, le=1)
    minimum_confirmed_retention: float = Field(default=0.9, ge=0, le=1)


class PointCloudQualityGovernanceConfig(_GovernanceConfigFields):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class PointCloudQualityGovernanceConfigRead(_GovernanceConfigFields):
    model_config = ConfigDict(extra="allow", allow_inf_nan=False)


class _PointCloudQualityConfigFields(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    schema_version: int = 2
    config_revision: int = Field(default=1, ge=1)
    enabled: bool = True
    enabled_rules: list[str] = Field(default_factory=lambda: sorted(_RULE_CODES))
    severity_overrides: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _upgrade_schema_v1(cls, value: Any) -> Any:
        if isinstance(value, dict) and value.get("schema_version", 1) == 1:
            return {**value, "schema_version": 2}
        return value


class PointCloudQualityConfig(_PointCloudQualityConfigFields):
    """Strict candidate config for evaluation requests and project writes."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    schema_version: Literal[2] = 2
    enabled_rules: list[PointCloudQualityRuleCode] = Field(
        default_factory=lambda: sorted(_RULE_CODES)
    )
    severity_overrides: dict[
        PointCloudQualityRuleCode, Literal["info", "warning", "blocker", "off"]
    ] = Field(default_factory=dict)
    thresholds: PointCloudQualityThresholdConfig = Field(
        default_factory=PointCloudQualityThresholdConfig
    )
    class_thresholds: dict[str, PointCloudQualityThresholdOverride] = Field(
        default_factory=dict
    )
    governance: PointCloudQualityGovernanceConfig = Field(
        default_factory=PointCloudQualityGovernanceConfig
    )

    @field_validator("enabled_rules")
    @classmethod
    def _validate_enabled_rules(
        cls, value: list[PointCloudQualityRuleCode]
    ) -> list[PointCloudQualityRuleCode]:
        unknown = sorted(set(value) - _RULE_CODES)
        if unknown:
            raise ValueError(f"unknown point cloud quality rule: {', '.join(unknown)}")
        return sorted(set(value))

    @field_validator("severity_overrides")
    @classmethod
    def _validate_overrides(cls, value):
        unknown = sorted(set(value) - _RULE_CODES)
        if unknown:
            raise ValueError(f"unknown point cloud quality rule: {', '.join(unknown)}")
        return dict(sorted(value.items()))

    @field_validator("class_thresholds")
    @classmethod
    def _validate_class_thresholds(
        cls, value: dict[str, PointCloudQualityThresholdOverride]
    ) -> dict[str, PointCloudQualityThresholdOverride]:
        canonical: dict[str, PointCloudQualityThresholdOverride] = {}
        for raw_name, thresholds in value.items():
            name = raw_name.strip()
            if not name:
                raise ValueError("point cloud quality class name cannot be blank")
            if len(name) > 100:
                raise ValueError("point cloud quality class name is too long")
            if name in canonical:
                raise ValueError(f"duplicate point cloud quality class: {name}")
            canonical[name] = thresholds
        return dict(sorted(canonical.items()))


class PointCloudQualityConfigRead(_PointCloudQualityConfigFields):
    """Additive project readback config; unknown fields survive at every level."""

    model_config = ConfigDict(extra="allow", allow_inf_nan=False)

    thresholds: PointCloudQualityThresholdConfigRead = Field(
        default_factory=PointCloudQualityThresholdConfigRead
    )
    class_thresholds: dict[str, PointCloudQualityThresholdOverrideRead] = Field(
        default_factory=dict
    )
    governance: PointCloudQualityGovernanceConfigRead = Field(
        default_factory=PointCloudQualityGovernanceConfigRead
    )
