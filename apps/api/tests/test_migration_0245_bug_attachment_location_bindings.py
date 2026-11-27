"""Bug attachment location upgrades preserve old rows and reject destructive rollback."""

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import inspect

from app.db.models.dataset_asset import StorageBinding
from app.services.dataset_assets import ensure_legacy_bindings
from tests.test_bug_attachment_location_records import owner
from tests.test_bug_attachment_storage import reference


def migration():
    path = (
        Path(__file__).parents[1]
        / "alembic/versions/0245_bug_attachment_location_bindings.py"
    )
    spec = importlib.util.spec_from_file_location(
        "bug_attachment_location_migration", path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("fact", ["schema2", "version", "binding"])
async def test_new_facts_prevent_destructive_downgrade(db_session, fact):
    if fact == "binding":
        await ensure_legacy_bindings(db_session)
        (await db_session.get(StorageBinding, "bug-reports")).generation = 2
    else:
        row = owner()
        if fact == "version":
            row.storage_ref = reference(upload_id=row.id, owner_id=row.owner_id)
            row.object_version_id = "actual-provider-version"
        db_session.add(row)
    await db_session.flush()
    connection = await db_session.connection()

    def refuse(conn):
        with Operations.context(MigrationContext.configure(conn)):
            with pytest.raises(RuntimeError, match="prevent destructive downgrade"):
                migration().downgrade()
        assert "object_version_id" in {
            column["name"]
            for column in inspect(conn).get_columns("bug_attachment_uploads")
        }

    await connection.run_sync(refuse)
