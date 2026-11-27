"""Authoritative persistent RLE liveness across business owners and retention windows."""

from typing import Any

from sqlalchemy import text

RLE_OBJECT_PREFIX = "raster-masks/sha256"


_REFERENCE_KEYS = text(
    "SELECT object_key FROM yoao_learning_v1.pending_dataset_mask_keys() "
    "UNION SELECT object_key FROM yoao_learning_v1.trial_mask_keys() "
    "UNION SELECT * FROM yoao_labeling_v1.raster_physical_reference_keys(NULL::text)"
)
_REFERENCE_EXISTS = text(
    "SELECT yoao_learning_v1.pending_dataset_mask_referenced(:key) "
    "OR yoao_learning_v1.trial_mask_referenced(:key) "
    "OR EXISTS (SELECT FROM yoao_labeling_v1.raster_physical_reference_keys(:key))"
)


async def referenced_keys(db) -> set[str]:
    return set(await db.scalars(_REFERENCE_KEYS))


async def is_referenced(db, key: str) -> bool:
    return bool(await db.scalar(_REFERENCE_EXISTS, {"key": key}))


def collect_mask_references(value: Any) -> list[dict[str, Any]]:
    """Collect every content-addressed RLE reference from a nested payload."""
    references: list[dict[str, Any]] = []
    if isinstance(value, dict):
        key = value.get("object_key")
        if isinstance(key, str) and key.startswith(f"{RLE_OBJECT_PREFIX}/"):
            references.append(value)
        for child in value.values():
            references.extend(collect_mask_references(child))
    elif isinstance(value, list):
        for child in value:
            references.extend(collect_mask_references(child))
    return references
