"""Content identities and short lifecycle locks shared by reference consumers."""

import uuid
from sqlalchemy import select

from app.db.models.dataset import DatasetItem
from app.db.models.dataset_asset import (
    DatasetAsset,
    StorageLocation,
)
from app.db.models.dataset_source import (
    DatasetReference,
    DatasetSource,
    DatasetSourceRevision,
)
from app.db.models.storage_connection import StorageConnection
from app.services.content_snapshot import ContentError
from app.services import storage_access
from app.services.task_scope import ids_clause


def task_binding(task):
    return {
        "task_id": str(task.id),
        "batch_id": str(task.batch_id) if task.batch_id else None,
        "item_id": str(task.dataset_item_id) if task.dataset_item_id else None,
        "file_path": task.file_path,
        "file_name": task.file_name,
        "version": task.version,
    }


def normalize_reference(reference):
    """Historical delegations belong to the initial maintenance epoch."""
    return {
        **reference,
        "maintenance_revision": reference.get("maintenance_revision", 1),
    }


def normalize_binding(binding):
    if "reference" not in binding:
        return binding
    return {**binding, "reference": normalize_reference(binding["reference"])}


async def reference_binding(db, task):
    item = await db.get(DatasetItem, task.dataset_item_id, populate_existing=True)
    ref = await db.get(DatasetReference, task.dataset_item_id, populate_existing=True)
    asset = await db.scalar(
        select(DatasetAsset)
        .where(DatasetAsset.dataset_item_id == task.dataset_item_id)
        .execution_options(populate_existing=True)
    )
    if (
        item is None
        or ref is None
        or asset is None
        or item.file_path != task.file_path
        or asset.verification_state != "verified"
        or asset.checksum_algorithm != "sha256"
    ):
        raise ContentError("content_unverified")
    source = await db.get(DatasetSource, ref.source_id, populate_existing=True)
    location = (
        await db.get(StorageLocation, source.location_id, populate_existing=True)
        if source
        else None
    )
    if not source or not source.enabled or source.unregistered_at:
        raise ContentError("source_unavailable")
    if not location or not location.enabled:
        raise ContentError("location_unavailable")
    connection = (
        await db.get(StorageConnection, location.connection_id, populate_existing=True)
        if location.connection_id
        else None
    )
    accepted_id = await db.scalar(
        select(DatasetSourceRevision.id)
        .where(
            DatasetSourceRevision.source_id == ref.source_id,
            DatasetSourceRevision.dataset_item_id == item.id,
            DatasetSourceRevision.asset_id == asset.id,
            DatasetSourceRevision.ownership_mode == "reference",
        )
        .order_by(DatasetSourceRevision.acceptance_order.desc())
        .limit(1)
    )
    return {
        "dataset_id": str(item.dataset_id),
        "asset_id": str(asset.id),
        "sha256": asset.checksum_value,
        "size": asset.size_bytes,
        "width": item.width,
        "height": item.height,
        "run_id": str(ref.run_id),
        "accepted_revision_id": str(accepted_id) if accepted_id else None,
        "source_id": str(source.id),
        "source_revision": source.config_revision,
        "location_id": str(location.id),
        "location_revision": location.config_revision,
        "maintenance_revision": await storage_access.check(db, location.id),
        "connection_id": str(connection.id) if connection else None,
        "connection_revision": connection.revision if connection else None,
    }


async def lock_reference_bindings(db, bindings, node_id):
    """No original I/O. Call after actor, Key and batch/Task locks."""
    for model, field in (
        (StorageConnection, "connection_id"),
        (StorageLocation, "location_id"),
        (DatasetSource, "source_id"),
    ):
        ids = sorted(
            {
                uuid.UUID(row["reference"][field])
                for row in bindings
                if row.get("reference", {}).get(field)
            },
            key=str,
        )
        if ids:
            await db.execute(
                select(model.id)
                .where(ids_clause(model.id, ids))
                .order_by(model.id)
                .with_for_update(read=True, nowait=True)
            )
    item_ids = sorted(
        {uuid.UUID(row["item_id"]) for row in bindings if "reference" in row},
        key=str,
    )
    for model, column in (
        (DatasetItem, DatasetItem.id),
        (DatasetAsset, DatasetAsset.dataset_item_id),
    ):
        await db.execute(
            select(model.id)
            .where(ids_clause(column, item_ids))
            .order_by(model.id)
            .with_for_update(read=True, nowait=True)
        )

    # Hold these SHARE locks in the caller's final transaction through publish.
    for row in sorted(
        bindings, key=lambda row: row.get("reference", {}).get("location_id", "")
    ):
        reference = row.get("reference")
        if reference:
            await storage_access.check(
                db,
                uuid.UUID(reference["location_id"]),
                expected_revision=reference.get("maintenance_revision", 1),
                lock=True,
            )
