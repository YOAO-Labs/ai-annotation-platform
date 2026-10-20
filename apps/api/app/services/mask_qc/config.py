from __future__ import annotations

import hashlib
import json

from aap_workspace.mask_configuration_values import MaskQCConfig

DEFAULT_RULE_SEVERITIES = {
    "empty_mask": "blocker",
    "near_empty_mask": "warning",
    "touches_border": "info",
    "small_island": "warning",
    "small_hole": "info",
    "narrow_bridge": "warning",
    "boundary_noise": "info",
    "derived_geometry_mismatch": "blocker",
    "same_class_overlap": "blocker",
    "cross_class_overlap": "warning",
    "flicker": "warning",
    "drift": "warning",
}


def load_mask_qc_config(stored: dict | None) -> MaskQCConfig:
    return MaskQCConfig.model_validate(stored or {})


def mask_qc_config_digest(config: MaskQCConfig | dict) -> str:
    parsed = config if isinstance(config, MaskQCConfig) else load_mask_qc_config(config)
    payload = parsed.model_dump(mode="json")
    payload.pop("config_revision", None)
    raw = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def severity_for_rule(config: MaskQCConfig, code: str) -> str | None:
    override = config.severity_overrides.get(code)
    if override == "off":
        return None
    return override or DEFAULT_RULE_SEVERITIES[code]
