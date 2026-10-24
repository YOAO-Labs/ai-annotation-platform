"""Pinned ownership of one evaluation's retained output namespace."""

import re
import uuid

from app.services import training_dataset_storage as _shared

Owner = _shared.Owner
_PURPOSE = "sample-evaluation"


def prefix_for(project_id, identifier) -> str:
    return _shared._prefix_for(project_id, identifier, purpose=_PURPOSE)


async def allocate(db, record) -> dict:
    """Explicit creation-only allocation within the caller's transaction."""
    return await _shared._allocate(db, record, purpose=_PURPOSE)


async def adoption_candidate(db, record) -> dict:
    """Read only the persisted binding; never bootstrap or adopt content."""
    return await _shared._adoption_candidate(db, record, purpose=_PURPOSE)


def resolve(record) -> Owner:
    return _shared._resolve(record, purpose=_PURPOSE)


def resolve_ref(ref, *, expected_prefix=None) -> Owner:
    return _shared._resolve_ref(ref, purpose=_PURPOSE, expected_prefix=expected_prefix)


def _checked(owner):
    if not isinstance(owner, Owner):
        _shared._reject("training_storage_invalid_key")
    checked = resolve_ref(owner.ref, expected_prefix=owner.prefix)
    if checked.bucket != owner.bucket:
        _shared._reject("training_storage_invalid_key")
    return checked


def expect_report(owner, attempt_id, key, sha256):
    """Require the canonical content-addressed report for this exact attempt."""
    checked = _checked(owner)
    try:
        if not isinstance(attempt_id, (str, uuid.UUID)):
            raise ValueError
        canonical = str(uuid.UUID(str(attempt_id)))
        if str(attempt_id) != canonical:
            raise ValueError
    except (TypeError, ValueError, AttributeError):
        _shared._reject("training_storage_invalid_key")
    if (
        not isinstance(sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", sha256) is None
        or key != checked.key(f"{canonical}/{sha256}.json")
    ):
        _shared._reject("training_storage_invalid_key")
    return key
