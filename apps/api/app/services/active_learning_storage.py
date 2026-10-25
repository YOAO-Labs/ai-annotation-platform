"""Pinned ownership of one active-learning round's retained output namespace."""

from app.services import training_dataset_storage as _shared

Owner = _shared.Owner
_PURPOSE = "active-learning"


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


def expect_media(owner, ordinal, key):
    """Require the exact frozen candidate ordinal in this round's shared media."""
    checked = _checked(owner)
    if (
        type(ordinal) is not int
        or not 0 <= ordinal < 32
        or key != checked.key(f"media/{ordinal}.png")
    ):
        _shared._reject("training_storage_invalid_key")
    return key
