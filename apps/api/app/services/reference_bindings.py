"""Content identities and short lifecycle locks shared by reference consumers."""

import uuid

from sqlalchemy import text

from app.services.content_snapshot import ContentError
from app.services import storage_access


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
    return await read_reference_binding(db, task.dataset_item_id, task.file_path)


async def read_reference_binding(db, item_id, file_path, *, lock=False):
    if db.autoflush:
        await db.flush()
    row = (
        await db.execute(
            text(
                "SELECT * FROM yoao_assets_v1.directory_reference_binding("
                "CAST(:item AS uuid), :file_path, :lock)"
            ),
            {"item": item_id, "file_path": file_path, "lock": lock},
        )
    ).one()
    if row.error_code is not None:
        raise ContentError(row.error_code)
    return {
        "dataset_id": str(row.dataset_id),
        "asset_id": str(row.asset_id),
        "sha256": row.sha256,
        "size": row.size_bytes,
        "width": row.width,
        "height": row.height,
        "run_id": str(row.run_id),
        "accepted_revision_id": (
            str(row.accepted_revision_id) if row.accepted_revision_id else None
        ),
        "source_id": str(row.source_id),
        "source_revision": row.source_revision,
        "location_id": str(row.location_id),
        "location_revision": row.location_revision,
        "maintenance_revision": await storage_access.check(
            db, row.location_id, lock=lock
        ),
        "connection_id": str(row.connection_id) if row.connection_id else None,
        "connection_revision": row.connection_revision,
    }


async def lock_reference_bindings(db, bindings, node_id):
    """No original I/O. Call after actor, Key and batch/Task locks."""
    pinned = [row for row in bindings if "reference" in row]
    if not pinned:
        return
    if db.autoflush:
        await db.flush()
    await db.execute(
        text(
            "SELECT yoao_assets_v1.lock_directory_reference_bindings("
            "CAST(:connection_id AS uuid[]), CAST(:location_id AS uuid[]), "
            "CAST(:source_id AS uuid[]), CAST(:item_id AS uuid[]))"
        ),
        {
            **{
                field: list(
                    {
                        uuid.UUID(row["reference"][field])
                        for row in pinned
                        if row["reference"].get(field)
                    }
                )
                for field in ("connection_id", "location_id", "source_id")
            },
            "item_id": list({uuid.UUID(row["item_id"]) for row in pinned}),
        },
    )
    for row in sorted(pinned, key=lambda row: row["reference"]["location_id"]):
        reference = row["reference"]
        await storage_access.check(
            db,
            uuid.UUID(reference["location_id"]),
            expected_revision=reference.get("maintenance_revision", 1),
            lock=True,
        )
