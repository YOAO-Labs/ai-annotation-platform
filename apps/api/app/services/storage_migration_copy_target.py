"""Registered migration target, with current secrets and frozen physical identity."""

from sqlalchemy import select

from app.services import connector_guard
from app.db.models.dataset_asset import StorageLocation
from app.db.models.storage_connection import StorageConnection
from app.services.source_readers.s3 import S3Access, directory_prefix
from app.services.storage_migration_inventories import reject


async def resolve(db, location_id, *, lock=False):
    # Connection precedes location, matching existing redirect/delete writers.
    location = await db.get(StorageLocation, location_id, populate_existing=True)
    if location is None or location.connection_id is None:
        reject("copy_target_unavailable")
    statement = (
        select(StorageConnection)
        .where(StorageConnection.id == location.connection_id)
        .execution_options(populate_existing=True)
    )
    if lock:
        statement = statement.with_for_update(read=True)
    connection = await db.scalar(statement)
    statement = (
        select(StorageLocation)
        .where(StorageLocation.id == location_id)
        .execution_options(populate_existing=True)
    )
    if lock:
        statement = statement.with_for_update(read=True)
    current = await db.scalar(statement)
    if (
        connection is None
        or current is None
        or current.connection_id != connection.id
        or not current.enabled
        or current.kind != "s3"
        or connection.kind != "s3"
        or connection.scope != "global"
        or connection.project_id is not None
        or not connection.secret_enc
    ):
        reject("copy_target_unavailable")
    access = S3Access(
        dict(connection.config),
        connection.secret_enc,
        tuple(await connector_guard.get_allowlist(db)),
    )
    prefix = directory_prefix(connection.config.get("base_prefix", ""))
    root = prefix + "_aap_migration/"
    if len(root.encode("utf-8")) > 800:
        reject("copy_target_prefix_budget")
    identity = {
        "location_id": str(current.id),
        "connection_id": str(connection.id),
        "endpoint_url": access.endpoint,
        "bucket": connection.config["bucket"],
        "base_prefix": prefix,
        "key_prefix": root,
    }
    revisions = {"location": current.config_revision, "connection": connection.revision}
    return identity, revisions, access, connection.name
