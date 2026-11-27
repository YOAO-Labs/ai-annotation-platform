"""Pinned ownership of a run and the exact attempt namespace beneath it."""

import uuid

from fastapi import HTTPException

from app.services import training_dataset_storage as _shared
from app.services.training_dataset_bridge import TrainingDatasetError

Owner = _shared.Owner
_PURPOSE = "training-run"


def prefix_for(project_id, run_id) -> str:
    return _shared._prefix_for(project_id, run_id, purpose=_PURPOSE)


async def allocate(db, run) -> dict:
    """Explicit creation-only allocation; flush within the caller's transaction."""
    return await _shared._allocate(db, run, purpose=_PURPOSE)


async def adoption_candidate(db, run) -> dict:
    """Discover legacy A only; never follow current allocation or adopt content."""
    return await _shared._adoption_candidate(db, run, purpose=_PURPOSE)


def resolve(run) -> Owner:
    return _shared._resolve(run, purpose=_PURPOSE)


def resolve_ref(ref, *, expected_prefix=None) -> Owner:
    return _shared._resolve_ref(ref, purpose=_PURPOSE, expected_prefix=expected_prefix)


def expect_attempt(owner, attempt_id, key):
    """Require a safe nonempty descendant of this run's canonical attempt."""
    if not isinstance(owner, Owner):
        _shared._reject("training_storage_invalid_key")
    checked = resolve_ref(owner.ref, expected_prefix=owner.prefix)
    if checked.bucket != owner.bucket:
        _shared._reject("training_storage_invalid_key")
    try:
        if not isinstance(attempt_id, (str, uuid.UUID)):
            raise ValueError
        canonical = str(uuid.UUID(str(attempt_id)))
        if str(attempt_id) != canonical:
            raise ValueError
    except (TypeError, ValueError, AttributeError):
        _shared._reject("training_storage_invalid_key")
    owner.contains(key)
    prefix = f"{owner.prefix}attempts/{canonical}/"
    if not key.startswith(prefix) or len(key) <= len(prefix):
        _shared._reject("training_storage_invalid_key")
    return key


def storage_owner(run):
    """Translate a pinned run locator at HTTP-oriented service boundaries."""
    try:
        return resolve(run)
    except TrainingDatasetError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from None


def attempt_storage_owner(run, attempt, key=None):
    owner = storage_owner(run)
    try:
        if attempt is None or attempt.run_id != run.id:
            raise TrainingDatasetError(
                "training_storage_attempt_mismatch",
                "产物不属于该训练执行",
                status_code=409,
            )
        if key is not None:
            expect_attempt(owner, attempt.id, key)
    except TrainingDatasetError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from None
    return owner


def artifact_key(owner, attempt_id, relative_name):
    try:
        key = owner.key(f"attempts/{attempt_id}/{relative_name}")
        expect_attempt(owner, attempt_id, key)
        return key
    except TrainingDatasetError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from None
