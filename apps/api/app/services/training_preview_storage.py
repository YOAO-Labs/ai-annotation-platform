"""Pinned ownership of one preview's retained output namespace."""

from training_contract.preview import LIMITS

from app.services import training_dataset_storage as _shared

Owner = _shared.Owner
_PURPOSE = "training-preview"


def prefix_for(project_id, identifier) -> str:
    return _shared._prefix_for(project_id, identifier, purpose=_PURPOSE)


async def allocate(db, record) -> dict:
    """Explicit creation-only allocation within the caller's transaction."""
    return await _shared._allocate(db, record, purpose=_PURPOSE)


async def adoption_candidate(db, record) -> dict:
    """Discover legacy A only; never follow current allocation or adopt content."""
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


def expect_artifact(owner, name, key):
    """Require one canonical preview PNG or its report beneath this owner."""
    checked = _checked(owner)
    allowed = {"report.json"} | {
        f"preview-{index}.png" for index in range(LIMITS["outputs"])
    }
    if not isinstance(name, str) or name not in allowed or key != checked.key(name):
        _shared._reject("training_storage_invalid_key")
    return key
