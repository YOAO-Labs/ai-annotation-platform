"""Preserve both export owners and reject destructive location rollback."""

from copy import deepcopy
from importlib import util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import text

from app.db.models.dataset_asset import StorageBinding
from app.services.dataset_assets import ensure_legacy_bindings
from tests.test_export_media_bundle_models import _new, _zip
from tests.test_export_location_bindings import external_db as external_db


def migration():
    path = (
        Path(__file__).parents[1] / "alembic/versions/0248_export_location_bindings.py"
    )
    spec = util.spec_from_file_location("export_location_migration", path)
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_legacy_roundtrip_preserves_both_owners_and_inventory(db_session):
    bundle = await _new(db_session)
    output = await _zip(db_session, bundle)
    saved = deepcopy((bundle.storage_ref, bundle.inventory, output.storage_ref))
    await db_session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    connection = await db_session.connection()

    def roundtrip(conn):
        with Operations.context(MigrationContext.configure(conn)):
            migration().downgrade()
            current = conn.execute(
                text(
                    "SELECT storage_ref,inventory FROM export_media_bundles WHERE id=:id"
                ),
                {"id": bundle.id},
            ).one()
            assert (current.storage_ref, current.inventory) == saved[:2]
            assert (
                conn.execute(
                    text("SELECT storage_ref FROM export_outputs WHERE id=:id"),
                    {"id": output.id},
                ).scalar_one()
                == saved[2]
            )
            migration().upgrade()

    await connection.run_sync(roundtrip)


async def test_new_binding_refuses_destructive_downgrade(db_session):
    await ensure_legacy_bindings(db_session)
    (await db_session.get(StorageBinding, "export")).generation = 2
    await db_session.flush()
    connection = await db_session.connection()

    def refuse(conn):
        with Operations.context(MigrationContext.configure(conn)):
            with pytest.raises(RuntimeError, match="prevent destructive downgrade"):
                migration().downgrade()

    await connection.run_sync(refuse)


@pytest.mark.parametrize("kind", ["zip", "media"])
async def test_saved_schema2_facts_refuse_downgrade_even_after_switch_back(
    external_db, kind
):
    from types import SimpleNamespace
    import uuid
    from app.db.models.export_output import ExportOutput
    from app.services import export_storage, export_media_bundles as bundles
    from app.services.dataset_assets import LEGACY_LOCATION_ID
    from tests.test_export_media_bundles import inputs, digest

    db, location, _, binding = external_db
    if kind == "zip":
        output = await _zip(db)
        # Allocate the schema2 owner before insertion in a separate new candidate.
        values = {
            column.name: deepcopy(getattr(output, column.name))
            for column in ExportOutput.__table__.columns
        }
        values["id"] = uuid.uuid4()
        values["storage_ref"] = await export_storage.allocate(
            db, SimpleNamespace(**values)
        )
        db.add(ExportOutput(**values))
    else:
        project_id, frozen, logical = inputs(tasks=1)
        await bundles.allocate(
            db,
            project_id=project_id,
            job_id=uuid.uuid4(),
            input_snapshot=frozen,
            input_digest=digest(frozen),
            logical_sources=logical,
        )
    binding.location_id, binding.bucket, binding.generation = (
        LEGACY_LOCATION_ID,
        export_storage.storage_service.export_bucket,
        1,
    )
    await db.flush()
    await db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    connection = await db.connection()

    def refuse(conn):
        with Operations.context(MigrationContext.configure(conn)):
            with pytest.raises(RuntimeError, match="prevent destructive downgrade"):
                migration().downgrade()

    await connection.run_sync(refuse)
