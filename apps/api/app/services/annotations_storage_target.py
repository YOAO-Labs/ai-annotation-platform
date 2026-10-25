"""Registered annotation-file targets and the existing original-writer probe."""

from app.services.annotations_storage import (
    MANAGED_NAMESPACE as MANAGED_NAMESPACE,
    MAX_MANAGED_PREFIX_CHARS as MAX_MANAGED_PREFIX_CHARS,
    MAX_MANAGED_PREFIX_BYTES as MAX_MANAGED_PREFIX_BYTES,
    managed_prefix as managed_prefix,
    target_for as target_for,
)


def probe_target(target):
    from app.services.dataset_storage_target import probe_target as shared

    return shared(target, purpose="annotations")
