"""One authority boundary for directory sources and their retained attempts."""

from fastapi import HTTPException
from sqlalchemy import String, and_, cast, false, or_, select, true

from app.db.enums import UserRole
from app.db.models.async_job import AsyncJob
from app.db.models.dataset import Dataset
from app.db.models.dataset_asset import StorageLocation
from app.db.models.dataset_source import (
    DatasetSource,
    DatasetScanRun,
    DatasetRegistrationRun,
)
from app.db.models.user import User
from app.services.storage_location import visible_clause

MANAGERS = {UserRole.SUPER_ADMIN, UserRole.PROJECT_ADMIN}


async def has_directory_source(db, dataset_id):
    return bool(
        await db.scalar(
            select(DatasetSource.id)
            .where(DatasetSource.dataset_id == dataset_id)
            .limit(1)
        )
    )


async def capture_dataset_write(db, dataset_id, actor, key_id):
    from app.services.dataset_lifecycle import require_writable

    dataset = await require_writable(db, dataset_id)
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
    from app.services.dataset_lifecycle import require_writable

    if lock:
        # Existing managed writers serialize on Dataset. Source writers use the
        # separate NOWAIT lifecycle helper after their Location/Source locks.
        await db.scalar(
            select(Dataset.id)
            .where(Dataset.id == dataset_id)
            .with_for_update(key_share=True)
        )
    # Legacy queued work is valid only in the original active lifecycle.
    await require_writable(
        db,
        dataset_id,
        expected_revision=(authority or {}).get("dataset_lifecycle_revision", 1),
        lock=False,
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
            db, user=actor, project_id=project_id
        )
        if not access.is_manager:
            raise HTTPException(403, detail={"code": "access_revoked"})


async def require_manager(db, actor_id):
    actor = await db.get(User, actor_id, populate_existing=True)
    if actor is None or not actor.is_active or actor.role not in MANAGERS:
        raise HTTPException(403, detail={"code": "access_revoked"})
    return actor


def source_scope_clause(actor):
    if not actor.is_active:
        return false()
    if actor.role == UserRole.SUPER_ADMIN:
        return true()
    if actor.role != UserRole.PROJECT_ADMIN or not actor.is_active:
        return false()
    return and_(
        DatasetSource.dataset_id.in_(
            select(Dataset.id).where(Dataset.created_by == actor.id)
        ),
        DatasetSource.location_id.in_(
            select(StorageLocation.id).where(visible_clause(actor))
        ),
    )


async def assert_dataset_manage(db, dataset_id, actor):
    actor = await require_manager(db, actor.id)
    dataset = await db.get(Dataset, dataset_id, populate_existing=True)
    if dataset is None or (
        actor.role != UserRole.SUPER_ADMIN and dataset.created_by != actor.id
    ):
        raise HTTPException(404, "Dataset not found")
    return actor


async def require_source(db, source_id, actor):
    actor = await require_manager(db, actor.id)
    source = await db.scalar(
        select(DatasetSource)
        .where(DatasetSource.id == source_id, source_scope_clause(actor))
        .execution_options(populate_existing=True)
    )
    if source is None:
        raise HTTPException(404, "Dataset source not found")
    return source


async def require_run(db, model, run_id, actor):
    run = await db.get(model, run_id, populate_existing=True)
    if run is None:
        raise HTTPException(404, "Dataset source run not found")
    await require_source(db, run.source_id, actor)
    return run


def job_scope_clause(actor, source_ids=None):
    # Payload identifies retained attempts after a resume replaces async_job_id.
    # Compare strings; malformed legacy payloads must not raise UUID cast errors.
    scope = source_scope_clause(actor)
    if source_ids is not None:
        scope = and_(scope, DatasetSource.id.in_(source_ids))
    scan = (
        select(1)
        .select_from(DatasetScanRun)
        .join(DatasetSource, DatasetSource.id == DatasetScanRun.source_id)
        .where(
            scope,
            or_(
                DatasetScanRun.async_job_id == AsyncJob.id,
                cast(DatasetScanRun.id, String) == AsyncJob.payload["run_id"].astext,
            ),
        )
        .exists()
    )
    registration = (
        select(1)
        .select_from(DatasetRegistrationRun)
        .join(DatasetSource, DatasetSource.id == DatasetRegistrationRun.source_id)
        .where(
            scope,
            or_(
                DatasetRegistrationRun.async_job_id == AsyncJob.id,
                cast(DatasetRegistrationRun.id, String)
                == AsyncJob.payload["registration_id"].astext,
            ),
        )
        .exists()
    )
    return or_(
        and_(AsyncJob.kind == "dataset_scan", scan),
        and_(AsyncJob.kind == "dataset_registration", registration),
    )
