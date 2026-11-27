"""Transaction-owned admissions and resource inventory for isolated fixtures.

Callers commit admission before starting work and seal it only after that work
has physically settled. An interrupted process leaves its admission active.
"""

import asyncio
import builtins
import hashlib
import json
import uuid
from datetime import datetime, timezone
from itertools import chain

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError

from app.db.models.fixture_run import (
    FixtureAuditEvent,
    FixtureMaintenanceWindow,
    FixtureRun,
    FixtureRunAdmission,
    FixtureRunResource,
)
from app.services.fixture_original_scope import (
    _reject as _reject,
)
from app.services.fixture_original_scope import (
    _require_settled_original_receipt as _require_settled_original_receipt,
)
from app.services.fixture_original_scope import (
    lock_original_scope as lock_original_scope,
)
from app.services.fixture_original_scope import (
    original_target as original_target,
)
from app.services.test_seed_access import assert_isolated_test_seed_database


async def _lock_run(db, run_id: uuid.UUID) -> FixtureRun:
    await assert_isolated_test_seed_database(db)
    run = await db.scalar(
        select(FixtureRun).where(FixtureRun.id == run_id).with_for_update()
    )
    if run is None:
        _reject("fixture_run_not_found")
    return run


async def start_owned_scope(
    db, *, suite_id: uuid.UUID, run_id: uuid.UUID, namespace: str
) -> None:
    """Register one independently sealable scope under a prepared suite."""
    await assert_isolated_test_seed_database(db)
    if suite_id == run_id or not namespace or len(namespace) > 100:
        _reject("fixture_scope_invalid")
    suite = await _lock_run(db, suite_id)
    if (
        suite.family != "owned"
        or suite.parent_run_id is not None
        or suite.state != "open"
    ):
        _reject("fixture_suite_closed")
    created = await db.scalar(
        insert(FixtureRun)
        .values(
            id=run_id,
            parent_run_id=suite_id,
            scope_key=namespace,
            family="owned",
            state="open",
        )
        .on_conflict_do_nothing()
        .returning(FixtureRun.id)
    )
    if created is None and await db.get(FixtureRun, run_id) is None:
        _reject("fixture_scope_busy")
    run = await _lock_run(db, run_id)
    if (
        run.family != "owned"
        or run.parent_run_id != suite_id
        or run.scope_key != namespace
        or run.state != "open"
    ):
        _reject("fixture_scope_changed")


async def admit(db, run_id: uuid.UUID, kind: str) -> uuid.UUID:
    run = await _lock_run(db, run_id)
    if run.state != "open":
        _reject("fixture_run_closed")
    admission = FixtureRunAdmission(id=uuid.uuid4(), run_id=run_id, kind=kind)
    db.add(admission)
    await db.flush()
    return admission.id


async def reserve(
    db,
    run_id: uuid.UUID,
    kind: str,
    resource_id: uuid.UUID,
    *,
    logical_key: str,
    published: bool = False,
    storage_identity: str | None = None,
) -> None:
    run = await _lock_run(db, run_id)
    if run.state != "open":
        _reject("fixture_run_closed")
    db.add(
        FixtureRunResource(
            run_id=run_id,
            kind=kind,
            resource_id=resource_id,
            logical_key=logical_key,
            state="published" if published else "reserved",
            storage_identity=storage_identity,
        )
    )
    await db.flush()


async def register_audit(
    db, run_id: uuid.UUID, audit_id: int, audit_created_at: datetime
) -> None:
    """Record the Audit owner's exact composite receipt in its producer transaction."""
    run = await _lock_run(db, run_id)
    if run.state != "open":
        _reject("fixture_run_closed")
    db.add(
        FixtureAuditEvent(
            run_id=run_id, audit_id=audit_id, audit_created_at=audit_created_at
        )
    )
    await db.flush()


async def reserve_original_dataset(
    db, run_id: uuid.UUID, dataset_id: uuid.UUID, *, logical_key: str
) -> None:
    """Reserve a dataset and its actual source store before exposing its keys."""
    _, root = await lock_original_scope(db, run_id)
    binding, _location = await original_target(db, root)
    await reserve(
        db,
        run_id,
        "dataset",
        dataset_id,
        logical_key=logical_key,
        storage_identity=binding.storage_identity,
    )


async def publish_resource(
    db, run_id: uuid.UUID, kind: str, resource_id: uuid.UUID
) -> None:
    run = await _lock_run(db, run_id)
    if run.state != "open":
        _reject("fixture_run_closed")
    resource = await db.scalar(
        select(FixtureRunResource)
        .where(
            FixtureRunResource.run_id == run_id,
            FixtureRunResource.kind == kind,
            FixtureRunResource.resource_id == resource_id,
        )
        .with_for_update()
    )
    if resource is None or resource.state != "reserved":
        _reject("fixture_resource_changed")
    resource.state = "published"
    await db.flush()


def _pending_item_fingerprint(item, sha256: str) -> str:
    """Bind recovery to the real item facts committed before the source write."""
    facts = {
        "id": str(item.id),
        "dataset_id": str(item.dataset_id),
        "file_name": item.file_name,
        "file_type": item.file_type,
        "file_size": item.file_size,
        "width": item.width,
        "height": item.height,
        "scene_id": str(item.scene_id) if item.scene_id is not None else None,
        "frame_index": item.frame_index,
        "metadata": item.metadata_ or {},
        "sha256": sha256,
    }
    return hashlib.sha256(
        json.dumps(
            facts, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


async def create_original(
    db,
    *,
    run_id: uuid.UUID,
    admission_id: uuid.UUID,
    item,
    logical_path: str,
    payload: bytes,
    content_type: str,
    requested_by: uuid.UUID,
):
    """Create one run-owned source with the existing managed-original writer."""
    from io import BytesIO

    from app.services import managed_original_creation

    allocation = await managed_original_creation.allocate_fixture(
        db,
        run_id=run_id,
        admission_id=admission_id,
        dataset_id=item.dataset_id,
        item_id=item.id,
        item_fingerprint=_pending_item_fingerprint(
            item, hashlib.sha256(payload).hexdigest()
        ),
        logical_path=logical_path,
        max_bytes=len(payload),
        requested_by=requested_by,
        content_type=content_type,
    )
    try:
        item.file_path = allocation["object_key"]
        db.add(item)
        await db.flush()
        await db.commit()
    except BaseException as failure:
        from app.services.storage_access import finish

        try:
            await finish(
                asyncio.create_task(
                    managed_original_creation.abandon_unstarted_fixture(
                        db,
                        run_id=run_id,
                        admission_id=admission_id,
                        creation_id=allocation["id"],
                    )
                )
            )
        except BaseException as abandon_failure:
            raise builtins.BaseExceptionGroup(
                "Fixture item preparation and no-write abandonment failed",
                [failure, abandon_failure],
            ) from None
        raise
    result = await managed_original_creation.create_stream(
        db, allocation, BytesIO(payload), content_type=content_type
    )
    return allocation, result


async def attach_original(db, item, allocation, result) -> None:
    """Publish the exact verified Creation as this item's saved location."""
    from app.services import managed_original_creation
    from app.services.dataset_assets import register_item_asset

    await register_item_asset(
        db,
        item,
        sha256=result.sha256,
        size_bytes=result.size_bytes,
        etag=result.etag,
        storage_ref=managed_original_creation.original_ref(allocation),
    )
    await managed_original_creation.attach(db, item, result.creation_id)


async def _original_owner_retired(db, creation, *, owner_type, owner_id) -> bool:
    """Recognize logical retirement, never physical reclaim completion."""
    from app.db.models.managed_original_retirement import ManagedOriginalRetirement

    retirements = list(
        await db.scalars(
            select(ManagedOriginalRetirement).where(
                ManagedOriginalRetirement.creation_id == creation.id
            )
        )
    )
    if not retirements:
        return False
    if len(retirements) != 1:
        _reject("fixture_original_retirement_changed")
    retirement = retirements[0]
    retired_owner_id = (
        retirement.dataset_item_id
        if owner_type == "dataset_asset"
        else retirement.auxiliary_original_id
    )
    if (
        retirement.owner_type != owner_type
        or retired_owner_id != owner_id
        or retirement.dataset_id != creation.dataset_id
        or retirement.location_id != creation.location_id
        or retirement.storage_identity != creation.storage_identity
        or retirement.bucket != creation.bucket
        or retirement.object_key != creation.object_key
        or retirement.sha256 != creation.sha256
        or retirement.size_bytes != creation.size_bytes
        or retirement.etag != creation.etag
    ):
        _reject("fixture_original_retirement_changed")
    return True


async def recover_verified_originals(db, run_id: uuid.UUID) -> None:
    """Attach settled Creations only to their unchanged, already-persisted items."""
    from app.db.models.dataset import DatasetItem
    from app.db.models.dataset_asset import DatasetAsset, DatasetAssetLocation
    from app.db.models.managed_original_creation import ManagedOriginalCreation
    from app.db.models.storage_maintenance import StorageOperation
    from app.services import managed_original_creation
    from app.services.dataset_assets import register_item_asset
    from app.services.managed_original_reclaim_fence import exclusive

    await lock_original_scope(db, run_id, sealed=True)
    unsettled = await db.scalar(
        select(FixtureRunAdmission.id)
        .where(
            FixtureRunAdmission.run_id == run_id, FixtureRunAdmission.state != "settled"
        )
        .limit(1)
    )
    if unsettled is not None:
        _reject("fixture_run_unsettled")
    rows = (
        await db.execute(
            select(FixtureRunResource, ManagedOriginalCreation)
            .join(
                ManagedOriginalCreation,
                ManagedOriginalCreation.id == FixtureRunResource.creation_id,
            )
            .where(
                FixtureRunResource.run_id == run_id,
                FixtureRunResource.kind == "creation",
            )
            .order_by(ManagedOriginalCreation.id)
        )
    ).all()
    for resource, creation in rows:
        if (
            creation.status in {"failed", "conflict"}
            and creation.write_started_at is None
        ):
            continue
        operation = (
            await db.get(StorageOperation, creation.operation_id)
            if creation.operation_id is not None
            else None
        )
        _require_settled_original_receipt(creation, operation)
        item = await db.scalar(
            select(DatasetItem)
            .where(DatasetItem.id == resource.item_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        item_owner = await db.get(
            FixtureRunResource, (run_id, "item", resource.item_id)
        )
        if item_owner is None:
            _reject("fixture_original_item_missing")
        await exclusive(db, creation.bucket, creation.object_key)
        classification = await db.scalar(
            text(
                "SELECT yoao_assets_v1.classify_fixture_original_reclaim("
                "CAST(:creation AS uuid), CAST(:identity AS text))"
            ),
            {"creation": creation.id, "identity": creation.storage_identity},
        )
        if classification != "qualified":
            _reject("fixture_original_store_unqualified")
        if item is None:
            if await _original_owner_retired(
                db, creation, owner_type="dataset_asset", owner_id=resource.item_id
            ):
                continue
            _reject("fixture_original_item_missing")
        locations = list(
            await db.scalars(
                select(DatasetAssetLocation).where(
                    DatasetAssetLocation.creation_id == creation.id
                )
            )
        )
        asset = await db.scalar(
            select(DatasetAsset).where(DatasetAsset.dataset_item_id == item.id)
        )
        if locations:
            if (
                len(locations) != 1
                or asset is None
                or locations[0].asset_id != asset.id
                or locations[0].object_key != creation.object_key
                or locations[0].bucket != creation.bucket
                or item.dataset_id != creation.dataset_id
                or item.file_path != creation.object_key
            ):
                _reject("fixture_original_attachment_changed")
            continue
        if (
            asset is not None
            or item.content_hash is not None
            or item.dataset_id != creation.dataset_id
            or item.file_path != creation.object_key
            or resource.item_fingerprint
            != _pending_item_fingerprint(item, creation.sha256)
        ):
            _reject("fixture_original_item_changed")
        saved = {
            column.name: getattr(creation, column.name)
            for column in creation.__table__.columns
        }
        await register_item_asset(
            db,
            item,
            sha256=creation.sha256,
            size_bytes=creation.size_bytes,
            etag=creation.etag,
            storage_ref=managed_original_creation.original_ref(saved),
        )
        await managed_original_creation.attach(db, item, creation.id)


async def recover_verified_auxiliary_originals(db, run_id: uuid.UUID) -> None:
    """Publish only this sealed run's already-verified real auxiliary owners."""
    from app.db.models.dataset_auxiliary_original import DatasetAuxiliaryOriginal
    from app.db.models.fixture_run import FixtureAuxiliaryIntent
    from app.db.models.managed_original_creation import ManagedOriginalCreation
    from app.db.models.storage_maintenance import StorageOperation
    from app.services.assets_operator import AssetsOperatorGateway
    from app.services.managed_original_reclaim_fence import exclusive

    await lock_original_scope(db, run_id, sealed=True)
    if (
        await db.scalar(
            select(FixtureRunAdmission.id)
            .where(
                FixtureRunAdmission.run_id == run_id,
                FixtureRunAdmission.state != "settled",
            )
            .limit(1)
        )
        is not None
    ):
        _reject("fixture_run_unsettled")
    rows = (
        await db.execute(
            select(FixtureAuxiliaryIntent, ManagedOriginalCreation)
            .join(
                ManagedOriginalCreation,
                ManagedOriginalCreation.id == FixtureAuxiliaryIntent.creation_id,
            )
            .where(FixtureAuxiliaryIntent.run_id == run_id)
            .order_by(ManagedOriginalCreation.id)
            .with_for_update()
        )
    ).all()
    gateway = AssetsOperatorGateway(db)
    for intent, creation in rows:
        if (
            creation.status in {"failed", "conflict"}
            and creation.write_started_at is None
        ):
            continue
        operation = (
            await db.get(StorageOperation, creation.operation_id)
            if creation.operation_id is not None
            else None
        )
        _require_settled_original_receipt(creation, operation)
        if (
            creation.owner_kind != "dataset_auxiliary"
            or creation.dataset_id != intent.dataset_id
            or creation.sha256 != intent.sha256
            or creation.size_bytes != intent.size_bytes
            or (creation.authority or {}).get("auxiliary")
            != {"kind": intent.kind, "source_contract": intent.source_contract}
        ):
            _reject("fixture_auxiliary_intent_changed")
        await exclusive(db, creation.bucket, creation.object_key)
        classification = await db.scalar(
            text(
                "SELECT yoao_assets_v1.classify_fixture_original_reclaim("
                "CAST(:creation AS uuid), CAST(:identity AS text))"
            ),
            {"creation": creation.id, "identity": creation.storage_identity},
        )
        if classification != "qualified":
            _reject("fixture_original_store_unqualified")
        owner = await db.get(
            DatasetAuxiliaryOriginal, creation.id, with_for_update=True
        )
        if owner is None:
            if await _original_owner_retired(
                db, creation, owner_type="dataset_auxiliary", owner_id=creation.id
            ):
                continue
            await gateway._managed_original_data(
                "yoao_assets_v1.publish_operator_auxiliary",
                {"creation_id": creation.id},
                {"creation_id": "uuid"},
            )
            owner = await db.get(
                DatasetAuxiliaryOriginal, creation.id, with_for_update=True
            )
        if owner is None or (
            owner.creation_id != creation.id
            or owner.dataset_id != intent.dataset_id
            or owner.kind != intent.kind
            or owner.source_contract != intent.source_contract
            or owner.location_id != creation.location_id
            or owner.storage_identity != creation.storage_identity
            or owner.bucket != creation.bucket
            or owner.object_key != creation.object_key
            or owner.sha256 != creation.sha256
            or owner.size_bytes != creation.size_bytes
            or owner.etag != creation.etag
            or owner.version_id != creation.version_id
            or owner.storage_ref != (creation.configuration or {}).get("original_ref")
        ):
            _reject("fixture_auxiliary_owner_changed")


async def settle(db, run_id: uuid.UUID, admission_id: uuid.UUID) -> None:
    await _lock_run(db, run_id)
    admission = await db.scalar(
        select(FixtureRunAdmission)
        .where(
            FixtureRunAdmission.id == admission_id, FixtureRunAdmission.run_id == run_id
        )
        .with_for_update()
    )
    if admission is None or admission.state != "active":
        _reject("fixture_admission_changed")
    from app.db.models.fixture_run import (
        FixtureAuxiliaryIntent,
        FixtureNativeMediaWorkIntent,
    )
    from app.db.models.managed_original_creation import ManagedOriginalCreation
    from app.db.models.storage_maintenance import StorageOperation

    native_work = await db.scalar(
        select(FixtureNativeMediaWorkIntent.operation_id)
        .where(
            FixtureNativeMediaWorkIntent.run_id == run_id,
            FixtureNativeMediaWorkIntent.admission_id == admission_id,
            FixtureNativeMediaWorkIntent.state != "settled",
        )
        .limit(1)
    )
    if native_work is not None:
        _reject("fixture_native_work_unsettled")
    creations = await db.execute(
        select(ManagedOriginalCreation, StorageOperation.id)
        .join(
            FixtureRunResource,
            FixtureRunResource.creation_id == ManagedOriginalCreation.id,
        )
        .outerjoin(
            StorageOperation,
            StorageOperation.id == ManagedOriginalCreation.operation_id,
        )
        .where(
            FixtureRunResource.run_id == run_id,
            FixtureRunResource.admission_id == admission_id,
        )
    )
    auxiliary_creations = await db.execute(
        select(ManagedOriginalCreation, StorageOperation.id)
        .join(
            FixtureAuxiliaryIntent,
            FixtureAuxiliaryIntent.creation_id == ManagedOriginalCreation.id,
        )
        .outerjoin(
            StorageOperation,
            StorageOperation.id == ManagedOriginalCreation.operation_id,
        )
        .where(
            FixtureAuxiliaryIntent.run_id == run_id,
            FixtureAuxiliaryIntent.admission_id == admission_id,
        )
    )
    for creation, operation_id in chain(creations, auxiliary_creations):
        if operation_id is not None:
            _reject("fixture_admission_unsettled")
        if creation.status == "verified":
            if (
                creation.settled_at is None
                or not creation.version_id
                or creation.version_id == "null"
            ):
                _reject("fixture_admission_unsettled")
        elif (
            creation.status not in {"failed", "conflict"}
            or creation.write_started_at is not None
        ):
            _reject("fixture_admission_unsettled")
    admission.state = "settled"
    admission.settled_at = datetime.now(timezone.utc)
    await db.flush()


async def seal(db, run_id: uuid.UUID, *, family: str) -> tuple[uuid.UUID, ...]:
    """Stop new admissions; caller commits before acting on returned blockers."""
    run = await _lock_run(db, run_id)
    if run.family != family:
        _reject("fixture_run_family_changed")
    if run.state == "retired":
        return ()
    if run.state == "open":
        run.state = "sealing"
        run.sealed_at = datetime.now(timezone.utc)
        await db.flush()
    return tuple(
        await db.scalars(
            select(FixtureRunAdmission.id)
            .where(
                FixtureRunAdmission.run_id == run_id,
                FixtureRunAdmission.state != "settled",
            )
            .order_by(FixtureRunAdmission.id)
        )
    )


async def _reclaim_window_target(db, run_id):
    """Lock the exact private target and its real Location maintenance gate."""
    _, root = await lock_original_scope(db, run_id, sealed=True)
    binding, _location = await original_target(db, root, lock_location=True)
    return binding, binding.storage_identity


async def _maintenance_transition(db, window, actor, operation):
    from app.schemas.storage_maintenance import (
        StorageMaintenanceApply,
        StorageMaintenanceRequest,
    )
    from app.services import storage_maintenance

    binding, identity = await _reclaim_window_target(db, window.run_id)
    if (
        binding.location_id != window.location_id
        or binding.generation != window.binding_generation
        or identity != window.storage_identity
        or actor.id != window.actor_id
    ):
        _reject("fixture_maintenance_window_changed")
    status = await storage_maintenance.status(db, window.location_id, actor)
    if status["revision"] != window.revision or status["state"] != window.state:
        _reject("fixture_maintenance_window_changed")
    request = StorageMaintenanceRequest(
        operation=operation, expected_revision=window.revision
    )
    preview = await storage_maintenance.preview(db, window.location_id, request, actor)
    applied = await storage_maintenance.apply(
        db,
        window.location_id,
        StorageMaintenanceApply(
            **request.model_dump(), impact_token=preview["impact_token"]
        ),
        actor,
    )
    window.revision = applied["revision"]
    window.state = applied["state"] if operation != "resume" else "resumed"
    if operation == "stop":
        window.stopped_revision = applied["revision"]
    else:
        window.resumed_revision = applied["revision"]
        window.resumed_at = datetime.now(timezone.utc)
    await db.flush()
    await db.commit()


async def begin_reclaim_window(db, run_id, actor):
    """Acquire or recover this run's exact, reviewed location-wide window."""
    from app.schemas.storage_maintenance import (
        StorageMaintenanceApply,
        StorageMaintenanceRequest,
    )
    from app.services import storage_maintenance
    from app.services.storage_access import finish

    binding, identity = await _reclaim_window_target(db, run_id)
    window = await db.scalar(
        select(FixtureMaintenanceWindow)
        .where(
            FixtureMaintenanceWindow.run_id == run_id,
            FixtureMaintenanceWindow.state != "resumed",
        )
        .with_for_update()
    )
    if window is None:
        other = await db.scalar(
            select(FixtureMaintenanceWindow.id).where(
                FixtureMaintenanceWindow.location_id == binding.location_id,
                FixtureMaintenanceWindow.state != "resumed",
            )
        )
        if other is not None:
            _reject("fixture_maintenance_window_busy")
        current = await storage_maintenance.status(db, binding.location_id, actor)
        if current["state"] != "active" or current["open_operations"]:
            _reject("fixture_maintenance_unsettled")
        request = StorageMaintenanceRequest(
            operation="drain", expected_revision=current["revision"]
        )
        preview = await storage_maintenance.preview(
            db, binding.location_id, request, actor
        )
        applied = await storage_maintenance.apply(
            db,
            binding.location_id,
            StorageMaintenanceApply(
                **request.model_dump(), impact_token=preview["impact_token"]
            ),
            actor,
        )
        window = FixtureMaintenanceWindow(
            id=uuid.uuid4(),
            run_id=run_id,
            location_id=binding.location_id,
            actor_id=actor.id,
            binding_generation=binding.generation,
            storage_identity=identity,
            revision=applied["revision"],
            state="draining",
            initial_revision=current["revision"],
            draining_revision=applied["revision"],
        )
        db.add(window)
        await db.commit()
    if (
        window.location_id != binding.location_id
        or window.binding_generation != binding.generation
        or window.storage_identity != identity
        or window.actor_id != actor.id
    ):
        _reject("fixture_maintenance_window_changed")
    if window.state == "draining":
        window_id, actor_id = window.id, actor.id
        try:
            await _maintenance_transition(db, window, actor, "stop")
        except BaseException as failure:
            try:
                await finish(asyncio.create_task(db.rollback()))
                from app.db.models.user import User

                saved_window = await db.get(FixtureMaintenanceWindow, window_id)
                saved_actor = await db.get(User, actor_id)
                if saved_window is None or saved_actor is None:
                    _reject("fixture_maintenance_window_changed")
                await finish(
                    asyncio.create_task(
                        _maintenance_transition(db, saved_window, saved_actor, "resume")
                    )
                )
            except BaseException as resume_failure:
                raise builtins.BaseExceptionGroup(
                    "Fixture maintenance stop and owned-window resume failed",
                    [failure, resume_failure],
                ) from None
            raise
    elif window.state != "stopped":
        _reject("fixture_maintenance_window_changed")
    else:
        current = await storage_maintenance.status(db, window.location_id, actor)
        if current["state"] != "stopped" or current["revision"] != window.revision:
            _reject("fixture_maintenance_window_changed")
        await db.commit()
    return window


async def end_reclaim_window(db, window, actor):
    """Resume only the location and revision recorded by this run's receipt."""
    await _maintenance_transition(db, window, actor, "resume")


async def _guard_jobs(
    db, operation: str, identifiers: list[uuid.UUID], code: str
) -> None:
    try:
        await db.execute(text(operation), {"ids": identifiers})
    except DBAPIError as exc:
        original = getattr(exc, "orig", exc)
        state = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
        if state == "23514" and code in str(original):
            raise HTTPException(status_code=409, detail={"code": code}) from exc
        raise


async def guard_project_jobs(db, project_ids: list[uuid.UUID]) -> None:
    """Ask Jobs to retain every unproved project execution before cleanup."""
    await _guard_jobs(
        db,
        "SELECT yoao_jobs_v1.guard_fixture_project_jobs(CAST(:ids AS uuid[]))",
        project_ids,
        "fixture_job_execution_unsettled",
    )


async def guard_dataset_jobs(db, dataset_ids: list[uuid.UUID]) -> None:
    """Reject owned or unscoped Dataset Jobs before physical cleanup."""
    await _guard_jobs(
        db,
        "SELECT yoao_jobs_v1.guard_fixture_dataset_jobs(CAST(:ids AS uuid[]))",
        dataset_ids,
        "fixture_dataset_job_unsettled",
    )
