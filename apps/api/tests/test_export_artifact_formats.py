"""Real DB metadata and lossless migration for multi-target export artifacts."""

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import select, text

from app.db.models.export_artifact import ExportArtifact
from tests.factory import create_project


async def test_multi_target_metadata_survives_guarded_format_migration(
    db_session, super_admin
):
    project = await create_project(db_session, owner_id=super_admin[0].id)
    targets = "aap_json,binary-png,coco,indexed-png,label-studio-brush,yolo-det,yolo-obb,yolo-seg"
    # This migration predates independent output ownership. Retain a legacy
    # NULL-owner index to exercise its original metadata preservation contract.
    artifact = ExportArtifact(
        cache_key=uuid.uuid4().hex,
        project_id=project.id,
        batch_id=None,
        format=targets,
        object_key="test/export.zip",
        file_count=1,
        size_bytes=1,
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
    )
    db_session.add(artifact)
    await db_session.flush()
    await db_session.refresh(artifact)
    assert artifact.format == targets
    migration_path = (
        Path(__file__).parents[1] / "alembic/versions/0211_export_artifact_formats.py"
    )
    spec = importlib.util.spec_from_file_location(
        "export_formats_migration", migration_path
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    connection = await db_session.connection()

    def migrate(sync_connection, action):
        with Operations.context(MigrationContext.configure(sync_connection)):
            action()

    with pytest.raises(RuntimeError, match="multi-target artifacts"):
        await connection.run_sync(lambda conn: migrate(conn, migration.downgrade))
    artifact.format = "coco"
    await db_session.flush()
    await connection.run_sync(lambda conn: migrate(conn, migration.downgrade))
    column = await db_session.execute(
        text(
            "SELECT data_type, character_maximum_length FROM information_schema.columns WHERE table_name='export_artifacts' AND column_name='format'"
        )
    )
    assert column.one() == ("character varying", 20)
    await connection.run_sync(lambda conn: migrate(conn, migration.upgrade))
    assert (
        await db_session.scalar(
            select(type(artifact).format).where(type(artifact).id == artifact.id)
        )
        == "coco"
    )
    artifact.format = targets
    await db_session.flush()
    await db_session.refresh(artifact)
    assert artifact.format == targets
