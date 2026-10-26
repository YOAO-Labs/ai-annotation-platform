"""Legacy sources remain unknown; recorded responsibility cannot be dropped."""

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import text

from app.schemas.dataset_source import SourceExternalProtectionUpdate
from app.services import dataset_source_protection as protection
from tests.test_dataset_content import content_world as content_world


def module():
    spec = importlib.util.spec_from_file_location(
        "migration_0244",
        Path(__file__).parents[1]
        / "alembic/versions/0244_source_external_protection.py",
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


async def test_existing_source_roundtrip_remains_unrecorded(
    db_session, content_world, monkeypatch
):
    migration = module()
    source_id = content_world[5].id

    def verify(sync):
        monkeypatch.setattr(
            migration, "op", Operations(MigrationContext.configure(sync.connection()))
        )
        # Later migrations do not own these columns. Rolling the shared schema
        # back would be blocked by facts other tests legitimately retain, so
        # round-trip only this revision's own objects.
        migration.downgrade()
        migration.upgrade()
        row = sync.execute(
            text(
                "SELECT external_protection_revision,external_protection FROM dataset_sources WHERE id=:id"
            ),
            {"id": source_id},
        ).one()
        assert row == (0, None)

    await db_session.run_sync(verify)


async def test_recorded_responsibility_refuses_destructive_downgrade(
    db_session, content_world, monkeypatch
):
    source = await protection.update(
        db_session,
        content_world[5].id,
        SourceExternalProtectionUpdate(
            expected_revision=0,
            responsible_party="External owner",
            recovery_method="unrecoverable",
        ),
        content_world[2],
    )
    original = dict(source.external_protection)
    migration = module()

    def verify(sync):
        monkeypatch.setattr(
            migration, "op", Operations(MigrationContext.configure(sync.connection()))
        )
        with pytest.raises(RuntimeError, match="responsibility"):
            migration.downgrade()
        assert (
            sync.scalar(
                text("SELECT external_protection FROM dataset_sources WHERE id=:id"),
                {"id": source.id},
            )
            == original
        )

    await db_session.run_sync(verify)
