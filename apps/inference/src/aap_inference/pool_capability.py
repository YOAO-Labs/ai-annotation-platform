"""Canonical routing capabilities for interchangeable model service-pool members.

A service pool represents interchangeable logical capability. Members join only on
exact fingerprint match (ADR-0050 D3); mismatch → 409 ``ml_backend_pool_capability_mismatch``
with a structured diff. Active members whose health refresh later shows fingerprint
drift are atomically disabled (§7.3).

The fingerprint is a SHA-256 of a canonical JSON encoding of routing-relevant
capability fields ONLY — it deliberately excludes URL, instance name, auth, GPU UUID,
VRAM, residency, cache, and display-only metadata (so replicas of the same model are
interchangeable). Field order, list order, and missing-vs-default must NOT change the
fingerprint (golden fixture §C.1).

This module is the single source of truth for canonicalization; the frontend must not
re-implement it (ADR-0050 §7).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

# The protocol currently persists normalized ``models[]`` entries.  Keep the old
# flat fields as a compatibility branch for snapshots written by older versions;
# production snapshots use ``models`` and are no longer fingerprinted as an empty
# flat capability.
_DEFAULTS: dict[str, Any] = {
    "version": None,
    "protocol_version": "1",
    "compat_protocol_versions": [],
    "model_version": None,
    "weights_version": None,
    "models": [],
    "model_ids": [],
    "task": None,
    "modality": None,
    "infra": None,
    "supported_prompts": [],
    "supported_inputs": [],
    "supported_outputs": [],
    "supported_trackers": [],
    "parameter_schema": {},
    "variant_axes": [],
    "stateful": False,
    "batchable": False,
    "warmup": False,
}

_CANONICAL_FIELDS: tuple[str, ...] = tuple(_DEFAULTS)

# Fields that are always excluded from routing fingerprint (runtime/display-only).
# Listed for documentation; the canonicalization only reads _CANONICAL_FIELDS so
# anything not listed there is implicitly excluded.
_EXCLUDED_FIELDS = frozenset(
    {
        "url",
        "name",
        "instance_name",
        "auth_method",
        "auth_token",
        "last_checked_at",
        "gpu_resource_id",
        "vram_budget_mb",
        "gpu_uuid",
        "memory_used_mb",
        "temperature",
        "residency",
        "model_loaded",
        "cache",
        "description",
    }
)


def _sorted_unique(value: list[Any]) -> list[Any]:
    """Sort a list of comparable scalars and dedupe, preserving JSON-serializability."""
    if not value:
        return []
    # Capability lists are strings or dicts-with-stable-keys; sort by JSON repr for determinism.
    keyed = {json.dumps(v, sort_keys=True, ensure_ascii=False): v for v in value}
    return [keyed[key] for key in sorted(keyed)]


def _canonicalize_list_field(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return _sorted_unique(list(value))
    return [value]


_DISPLAY_ONLY_KEYS = {
    "label",
    "title",
    "description",
    "placeholder",
    "order",
    "group",
    "help_text",
    "note",
}


def _canonicalize_schema_value(value: Any) -> Any:
    """Recursively normalize request schemas while dropping presentation hints."""
    if isinstance(value, dict):
        return {
            key: _canonicalize_schema_value(value[key])
            for key in sorted(value)
            if key not in _DISPLAY_ONLY_KEYS
        }
    if isinstance(value, list):
        normalized = [_canonicalize_schema_value(item) for item in value]
        return _sorted_unique(normalized)
    return value


def _canonicalize_param_schema(value: Any) -> dict:
    """Parameter schema: keep only fields that affect request legality.

    The full backend ``/setup.params`` may include display hints, defaults, UI ordering.
    For routing equivalence we only need the schema *shape* (which params are required,
    their types, and enum constraints) — two backends whose params render differently
    but accept the same request payload are interchangeable.
    """
    if not isinstance(value, dict):
        return {}
    return _canonicalize_schema_value(value)


def _canonicalize_variant_axes(value: Any) -> list[dict]:
    if not isinstance(value, list):
        return []
    out = []
    for axis in value:
        if not isinstance(axis, dict):
            continue
        norm = {
            "key": axis.get("key"),
            "values": _canonicalize_list_field(axis.get("values")),
            "default": axis.get("default"),
        }
        out.append(norm)
    # Stable order by axis key.
    out.sort(key=lambda a: json.dumps(a.get("key"), sort_keys=True, ensure_ascii=False))
    return out


_MODEL_LIST_FIELDS = {
    "supported_prompts",
    "supported_inputs",
    "supported_geometric_outputs",
    "output_attribute_types",
    "supported_text_outputs",
    "supported_trackers",
    "text_driven_trackers",
}

_MODEL_SCALAR_FIELDS = (
    "id",
    "task",
    "model_family",
    "infra",
    "modality",
    "is_interactive",
    "default_input_type",
    "variants_shared_across_tasks",
    "composition",
)


def _canonicalize_supported_variants(value: Any) -> list[dict]:
    if not isinstance(value, list):
        return []
    axes: list[dict] = []
    for axis in value:
        if not isinstance(axis, dict):
            continue
        variants = []
        for variant in axis.get("variants") or axis.get("values") or []:
            if isinstance(variant, dict):
                variants.append(_canonicalize_schema_value(variant))
            else:
                variants.append({"value": variant})
        axes.append(
            {
                "key": axis.get("key"),
                "default": axis.get("default"),
                "variants": _sorted_unique(variants),
            }
        )
    return sorted(axes, key=lambda item: str(item.get("key") or ""))


def _canonicalize_models(value: Any) -> list[dict]:
    if not isinstance(value, list):
        return []
    models: list[dict] = []
    for raw_model in value:
        if not isinstance(raw_model, dict):
            continue
        model = {field: raw_model.get(field) for field in _MODEL_SCALAR_FIELDS}
        for field in _MODEL_LIST_FIELDS:
            model[field] = _canonicalize_list_field(raw_model.get(field))
        model["output_attribute_schema"] = _canonicalize_schema_value(
            raw_model.get("output_attribute_schema") or []
        )
        model["supported_variants"] = _canonicalize_supported_variants(
            raw_model.get("supported_variants")
        )
        combinations = raw_model.get("variant_combinations") or []
        model["variant_combinations"] = _sorted_unique(
            [
                list(combo) if isinstance(combo, (list, tuple)) else combo
                for combo in combinations
            ]
        )
        model["default_variants"] = _canonicalize_schema_value(
            raw_model.get("default_variants") or {}
        )
        model["default_thresholds"] = _canonicalize_schema_value(
            raw_model.get("default_thresholds") or {}
        )
        model["params"] = _canonicalize_param_schema(raw_model.get("params"))
        resource_profile = raw_model.get("resource_profile") or {}
        model["batchable"] = bool(
            resource_profile.get("batchable", False)
            if isinstance(resource_profile, dict)
            else False
        )
        model["exemplar_capabilities"] = _canonicalize_schema_value(
            raw_model.get("exemplar_capabilities") or {}
        )
        models.append(model)
    return sorted(models, key=lambda item: str(item.get("id") or ""))


def canonicalize_capability(raw: dict | None) -> dict:
    """Build the canonical routing-capability snapshot from a raw ``/setup`` / health_meta dict.

    Applies: defaults fill, list sort+dedupe, param-schema display-field strip,
    variant-axis normalization, fixed field order. Deterministic regardless of input
    field/list ordering or omitted-vs-default fields (golden fixture §C.1).
    """
    raw = raw or {}
    caps = raw.get("capabilities") if "capabilities" in raw else raw
    caps = caps if isinstance(caps, dict) else {}

    canonical: dict[str, Any] = {}
    for field_name in _CANONICAL_FIELDS:
        if field_name == "models":
            canonical[field_name] = _canonicalize_models(caps.get(field_name))
        elif field_name == "parameter_schema":
            canonical[field_name] = _canonicalize_param_schema(caps.get(field_name))
        elif field_name == "variant_axes":
            canonical[field_name] = _canonicalize_variant_axes(caps.get(field_name))
        elif field_name in {
            "compat_protocol_versions",
            "model_ids",
            "supported_prompts",
            "supported_inputs",
            "supported_outputs",
            "supported_trackers",
        }:
            canonical[field_name] = _canonicalize_list_field(caps.get(field_name))
        elif field_name in {"stateful", "batchable", "warmup"}:
            canonical[field_name] = bool(caps.get(field_name, _DEFAULTS[field_name]))
        else:
            canonical[field_name] = caps.get(field_name, _DEFAULTS[field_name])
    return canonical


def capability_fingerprint(raw_or_canonical: dict | None) -> str:
    """SHA-256 hex (64 chars) of the canonical capability snapshot.

    Accepts either a raw ``/setup`` dict (canonicalized internally) or an already-
    canonicalized dict. Deterministic across field/list order and missing defaults.
    """
    if raw_or_canonical is None:
        raw_or_canonical = {}
    # Detect already-canonical input: it has exactly the canonical field set.
    if set(raw_or_canonical.keys()) == set(_CANONICAL_FIELDS):
        canonical = raw_or_canonical
    else:
        canonical = canonicalize_capability(raw_or_canonical)
    encoded = json.dumps(
        canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CapabilityMismatch:
    """Structured diff between two canonical snapshots (returned with 409)."""

    pool_fingerprint: str
    candidate_fingerprint: str
    differing_fields: tuple[str, ...]


def diff_capabilities(
    pool_snapshot: dict | None, candidate_snapshot: dict | None
) -> CapabilityMismatch | None:
    """Compare two canonical snapshots field-by-field. Returns None if identical.

    Used when adding a member to a pool: exact match required (ADR-0050 D3 / §7.3).
    On mismatch returns the field names that differ for the 409 response body.
    """
    pool_canon = canonicalize_capability(pool_snapshot)
    cand_canon = canonicalize_capability(candidate_snapshot)
    differing = tuple(
        field
        for field in _CANONICAL_FIELDS
        if pool_canon.get(field) != cand_canon.get(field)
    )
    if not differing:
        return None
    return CapabilityMismatch(
        pool_fingerprint=capability_fingerprint(pool_canon),
        candidate_fingerprint=capability_fingerprint(cand_canon),
        differing_fields=differing,
    )
