"""One authority boundary for directory sources and their retained attempts."""

from datetime import datetime
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select, text

from app.db.enums import UserRole
from app.db.models.dataset_source import (
    DatasetScanRun,
    DatasetRegistrationRun,
)
from app.services.assets_dataset_data import visible_sources
from app.services.identity_account_lock_data import IdentityAccountLockData

MANAGERS = {UserRole.SUPER_ADMIN, UserRole.PROJECT_ADMIN}


async def _dataset_authority(db, dataset_id, *, lock=False):
    if db.autoflush:
        await db.flush()
    row = (
        await db.execute(
            text(
                "SELECT * FROM yoao_assets_v1.read_dataset_source_authority("
                "CAST(:dataset_id AS uuid), CAST(:lock AS boolean))"
            ),
            {"dataset_id": dataset_id, "lock": lock},
        )
    ).one_or_none()
    if row is not None and (
        row.dataset_id != dataset_id
        or not isinstance(row.created_by, UUID)
        or (row.archived_at is not None and not isinstance(row.archived_at, datetime))
        or type(row.lifecycle_revision) is not int
        or row.lifecycle_revision < 1
        or type(row.directory_protected) is not bool
    ):
        raise RuntimeError("Invalid Dataset source authority facts")
    return row


async def _writable_dataset_authority(
    db, dataset_id, *, expected_revision=None, lock=False, allow_missing=False
):
    dataset = await _dataset_authority(db, dataset_id, lock=lock)
    if dataset is None:
        if allow_missing:
            return None
        raise HTTPException(404, "Dataset not found")
    if dataset.archived_at is not None:
        raise HTTPException(409, detail={"code": "dataset_archived"})
    if (
        expected_revision is not None
        and dataset.lifecycle_revision != expected_revision
    ):
        raise HTTPException(409, detail={"code": "dataset_lifecycle_changed"})
    return dataset


async def has_directory_source(db, dataset_id):
    dataset = await _dataset_authority(db, dataset_id)
    return dataset is not None and dataset.directory_protected


async def capture_dataset_write(db, dataset_id, actor, key_id, *, allow_missing=False):
    dataset = await _writable_dataset_authority(
        db, dataset_id, allow_missing=allow_missing
    )
    if dataset is None:
        return None
    lifecycle = {"dataset_lifecycle_revision": dataset.lifecycle_revision}
    if not await has_directory_source(db, dataset_id):
        return lifecycle
    from app.services.dataset_content import key_authority

    actor = await assert_dataset_manage(db, dataset_id, actor)
    return {
        **lifecycle,
        "actor_id": str(actor.id),
        "api_key_id": str(key_id) if key_id else None,
        "key_proof": await key_authority(db, actor, key_id, "datasets:write"),
    }


async def validate_dataset_write(
    db, dataset_id, authority, *, project_id=None, lock=False
):
    await _writable_dataset_authority(
        db,
        dataset_id,
        expected_revision=(authority or {}).get("dataset_lifecycle_revision", 1),
        lock=lock,
    )
    if not await has_directory_source(db, dataset_id):
        return
    if not authority or "actor_id" not in authority:
        raise HTTPException(403, detail={"code": "access_revoked"})
    import uuid
    from app.services.dataset_content import key_authority

    actor = await require_manager(db, uuid.UUID(authority["actor_id"]))
    await assert_dataset_manage(db, dataset_id, actor)
    if (
        await key_authority(db, actor, authority["api_key_id"], "datasets:write")
        != authority["key_proof"]
    ):
        raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
    if project_id is not None:
        from app.services.project_access import resolve_project_access_by_id

        _, access = await resolve_project_access_by_id(
            db, actor_id=actor.id, project_id=project_id
        )
        if not access.is_manager:
            raise HTTPException(403, detail={"code": "access_revoked"})


async def require_manager(db, actor_id):
    actor = await IdentityAccountLockData(db).read_actor_status(actor_id)
    if actor is None or not actor.is_active or actor.role not in MANAGERS:
        raise HTTPException(403, detail={"code": "access_revoked"})
    return actor


async def assert_dataset_manage(db, dataset_id, actor):
    actor = await require_manager(db, actor.id)
    dataset = await _dataset_authority(db, dataset_id)
    if dataset is None or (
        actor.role != UserRole.SUPER_ADMIN and dataset.created_by != actor.id
    ):
        raise HTTPException(404, "Dataset not found")
    return actor


async def require_source(db, source_id, actor):
    actor = await require_manager(db, actor.id)
    sources = visible_sources(actor.id, source_id=source_id)
    source = (await db.execute(select(sources))).one_or_none()
    if source is None:
        raise HTTPException(404, "Dataset source not found")
    return source


async def require_run(db, model, run_id, actor):
    if model is DatasetScanRun:
        from app.services.dataset_scan_data import read_scan_snapshot

        run = await read_scan_snapshot(db, run_id)
    elif model is DatasetRegistrationRun:
        from app.services.dataset_registration_data import read_run

        run = await read_run(db, run_id)
    else:
        raise ValueError("Unknown directory run type")
    if run is None:
        raise HTTPException(404, "Dataset source run not found")
    await require_source(db, run.source_id, actor)
    return run
