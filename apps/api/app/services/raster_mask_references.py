"""Authoritative persistent RLE liveness across business owners and retention windows."""

from sqlalchemy import text


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
