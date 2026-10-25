from __future__ import annotations

from copy import deepcopy

from aap_workspace.point_quality_configuration_values import PointCloudQualityConfig
from app.services.mask_qc.service import canonical_digest


DEFAULT_RULE_SEVERITIES = {
    "low_point_count": "warning",
    "size_outlier": "warning",
    "ground_clearance": "warning",
    "temporal_jump": "warning",
    "track_gap": "warning",
    "track_identity_drift": "blocker",
    "duplicate_track_member": "blocker",
    "projection_residual": "warning",
}


def load_point_cloud_quality_config(stored: dict | None) -> PointCloudQualityConfig:
    payload = deepcopy(stored or {})
    if payload.get("schema_version", 1) == 1:
        payload["schema_version"] = 2
    return PointCloudQualityConfig.model_validate(payload)


def point_cloud_quality_config_digest(config: PointCloudQualityConfig) -> str:
    payload = config.model_dump(mode="json")
    payload.pop("config_revision", None)
    return canonical_digest(payload)


def severity_for_rule(config: PointCloudQualityConfig, code: str) -> str | None:
    if code not in config.enabled_rules:
        return None
    override = config.severity_overrides.get(code)
    if override == "off":
        return None
    return override or DEFAULT_RULE_SEVERITIES[code]


def effective_thresholds(
    config: PointCloudQualityConfig, class_name: str | None = None
):
    payload = config.thresholds.model_dump()
    if class_name and class_name in config.class_thresholds:
        payload.update(
            config.class_thresholds[class_name].model_dump(exclude_none=True)
        )
    return type(config.thresholds).model_validate(payload)
