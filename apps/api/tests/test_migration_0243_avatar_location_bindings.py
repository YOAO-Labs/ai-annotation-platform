"""Avatar location upgrades preserve old rows and reject destructive rollback."""

import importlib.util
from pathlib import Path
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import inspect, text

from app.db.models.avatar_upload import AvatarUpload
from app.db.models.dataset_asset import StorageBinding
from app.services.dataset_assets import ensure_legacy_bindings
from tests.test_avatar_location_records import owner
from tests.test_avatar_storage import reference


def migration():
    path = (
        Path(__file__).parents[1] / "alembic/versions/0243_avatar_location_bindings.py"
    )
    spec = importlib.util.spec_from_file_location("avatar_location_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_roundtrip_preserves_legacy_object_location_and_state(db_session):
    token = uuid.uuid4().hex
    saved = reference(token=token)
    db_session.add(
        AvatarUpload(
            token=token,
            owner_id=uuid.uuid4(),
            storage_ref=saved,
            sha256="a" * 64,
            size_bytes=11,
            state="pending",
        )
    )
    await db_session.flush()
    connection = await db_session.connection()

    def roundtrip(conn):
        module = migration()
        with Operations.context(MigrationContext.configure(conn)):
            module.downgrade()
            assert "object_version_id" not in {
                column["name"] for column in inspect(conn).get_columns("avatar_uploads")
            }
            result = conn.execute(
                text("SELECT storage_ref,state FROM avatar_uploads WHERE token=:token"),
                {"token": token},
            ).one()
            assert result.storage_ref == saved and result.state == "pending"
            module.upgrade()
            assert (
                conn.execute(
                    text(
                        "SELECT object_version_id FROM avatar_uploads WHERE token=:token"
                    ),
                    {"token": token},
                ).scalar_one()
                is None
            )

    await connection.run_sync(roundtrip)


@pytest.mark.parametrize("fact", ["schema2", "version", "binding"])
async def test_new_facts_prevent_destructive_downgrade(db_session, fact):
    if fact == "binding":
        await ensure_legacy_bindings(db_session)
        (await db_session.get(StorageBinding, "avatars")).generation = 2
    else:
        row = owner()
        if fact == "version":
            row.storage_ref = reference(token=row.token)
            row.object_version_id = "actual-provider-version"
        db_session.add(row)
    await db_session.flush()
    connection = await db_session.connection()

    def refuse(conn):
        with Operations.context(MigrationContext.configure(conn)):
            with pytest.raises(RuntimeError, match="prevent destructive downgrade"):
                migration().downgrade()
        assert "object_version_id" in {
            column["name"] for column in inspect(conn).get_columns("avatar_uploads")
        }

    await connection.run_sync(refuse)
