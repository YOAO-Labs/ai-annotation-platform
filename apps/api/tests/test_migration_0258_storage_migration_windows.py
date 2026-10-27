"""Additive window schema refuses lost bindings and destructive app rollback."""

from copy import deepcopy
import importlib.util
from pathlib import Path
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import delete, inspect
from sqlalchemy.exc import IntegrityError

from app.db.models.storage_migration_copy import StorageMigrationCopy as Copy
from app.db.models.storage_migration_inventory import (
    StorageMigrationInventory as Inventory,
)
from app.db.models.storage_migration_window import StorageMigrationWindow as Window
from app.schemas.storage_migration_window import StorageMigrationWindowRequest
from app.services import storage_migration_windows as windows
from tests.test_storage_migration_windows import prepared as prepared


def migration(connection):
    path = (
        Path(__file__).parents[1] / "alembic/versions/0258_storage_migration_windows.py"
    )
    spec = importlib.util.spec_from_file_location(
        "migration_window_" + uuid.uuid4().hex, path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(connection))
    return module


async def test_empty_0258_roundtrip_retains_legacy_nullable_owners(
    db_session, prepared
):
    connection = await db_session.connection()
    from tests.test_migration_0262_storage_migration_finalizations import (
        migration as finalization_migration,
    )
    from tests.test_migration_0263_storage_migration_read_publications import (
        migration as read_migration,
    )
    from tests.test_storage_migration_cutovers import migration as cutover_migration

    await connection.run_sync(lambda sync: cutover_migration(sync).downgrade())
    await connection.run_sync(lambda sync: read_migration(sync).downgrade())
    await connection.run_sync(lambda sync: finalization_migration(sync).downgrade())
    await connection.run_sync(lambda sync: migration(sync).downgrade())
    await connection.run_sync(lambda sync: migration(sync).upgrade())
    await connection.run_sync(lambda sync: finalization_migration(sync).upgrade())
    await connection.run_sync(lambda sync: read_migration(sync).upgrade())
    await connection.run_sync(lambda sync: cutover_migration(sync).upgrade())
    await db_session.refresh(prepared)
    assert prepared.window_id is prepared.window_revision is None
    foreign_keys = await connection.run_sync(
        lambda sync: inspect(sync).get_foreign_keys("storage_migration_copies")
    )
    assert (
        next(row for row in foreign_keys if row["name"] == "fk_migration_copy_window")[
            "options"
        ]["ondelete"]
        == "RESTRICT"
    )


@pytest.mark.parametrize("family", ["inventory", "copy"])
@pytest.mark.parametrize("pair", ["id_only", "revision_only", "zero", "negative"])
async def test_database_rejects_incomplete_or_invalid_window_binding(
    db_session, prepared, super_admin, family, pair
):
    value = await windows.open_window(
        db_session,
        super_admin[0],
        StorageMigrationWindowRequest(parent_copy_id=prepared.id),
    )
    fields = {"window_id": value.window_id, "window_revision": 2}
    if pair == "id_only":
        fields["window_revision"] = None
    elif pair == "revision_only":
        fields["window_id"] = None
    elif pair == "zero":
        fields["window_revision"] = 0
    else:
        fields["window_revision"] = -1
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            if family == "copy":
                row = Copy(
                    id=uuid.uuid4(),
                    input_fingerprint=uuid.uuid4().hex * 2,
                    source_report=b"x",
                    source_report_sha256="a" * 64,
                    source_complete=False,
                    source=prepared.source,
                    target_location_id=prepared.target_location_id,
                    target=prepared.target,
                    handling=prepared.handling,
                    limits=prepared.limits,
                    **fields,
                )
            else:
                from app.services import storage_migration_inventories as inventories

                job = await inventories.jobs.create_job(
                    db_session,
                    kind=inventories.KIND,
                    user_id=super_admin[0].id,
                    celery_task_id=str(uuid.uuid4()),
                )
                row = Inventory(
                    async_job_id=job.id,
                    requested_by=super_admin[0].id,
                    task_id=job.celery_task_id,
                    source_location_id=prepared.target_location_id,
                    source=prepared.source,
                    limits={},
                    handling={},
                    authority={},
                    input_fingerprint=uuid.uuid4().hex * 2,
                    maintenance_revision=1,
                    status="pending",
                    **fields,
                )
            db_session.add(row)
            await db_session.flush()


@pytest.mark.parametrize("family", ["inventory", "copy"])
async def test_existing_owner_cannot_be_adopted_into_different_window(
    db_session, prepared, super_admin, family
):
    value = await windows.open_window(
        db_session,
        super_admin[0],
        StorageMigrationWindowRequest(parent_copy_id=prepared.id),
    )
    row = (
        prepared
        if family == "copy"
        else await db_session.get(Inventory, prepared.source_inventory_job_id)
    )
    with pytest.raises(IntegrityError, match="window binding is immutable"):
        async with db_session.begin_nested():
            row.window_id, row.window_revision = value.window_id, 2
            await db_session.flush()


async def test_window_identity_parent_and_schema_facts_are_retained(
    db_session, prepared, super_admin
):
    value = await windows.open_window(
        db_session,
        super_admin[0],
        StorageMigrationWindowRequest(parent_copy_id=prepared.id),
    )
    row = await db_session.get(Window, value.window_id)
    with pytest.raises(IntegrityError, match="window identity is immutable"):
        async with db_session.begin_nested():
            row.source = {**deepcopy(row.source), "endpoint": "http://other.example"}
            await db_session.flush()
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(delete(Copy).where(Copy.id == prepared.id))
    connection = await db_session.connection()
    with pytest.raises(RuntimeError, match="retain access responsibility"):
        await connection.run_sync(lambda sync: migration(sync).downgrade())
    assert "storage_migration_windows" in await connection.run_sync(
        lambda sync: inspect(sync).get_table_names()
    )
