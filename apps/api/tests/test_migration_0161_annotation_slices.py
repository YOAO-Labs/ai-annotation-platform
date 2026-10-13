"""Run the actual CHECK migration against the configured disposable test database."""

import asyncio
import uuid

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models.annotation_operation import AnnotationOperation
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.user import User
from tests.test_migration_0173_project_role_preparation import _replay


def test_empty_downgrade_upgrade_and_used_ledger_refuses_downgrade(
    test_db_url, apply_migrations
):
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", test_db_url)
    migration = ScriptDirectory.from_config(config).get_revision("0161").module

    async def roundtrip(*, used: bool = False):
        # Replay this revision only; never traverse irreversible 0174.
        engine = create_async_engine(test_db_url)
        try:
            async with engine.connect() as connection:
                transaction = await connection.begin()
                try:
                    await connection.run_sync(_replay, migration, "downgrade")
                    if not used:
                        await connection.run_sync(_replay, migration, "upgrade")
                finally:
                    await transaction.rollback()
        finally:
            await engine.dispose()

    asyncio.run(roundtrip())
    actor_id, project_id, task_id, operation_id = [uuid.uuid4() for _ in range(4)]

    async def inspect(action):
        engine = create_async_engine(test_db_url)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with maker() as db:
                if action == "seed":
                    db.add(
                        User(
                            id=actor_id,
                            email=f"migration-slice-{actor_id}@test.local",
                            name="Migration",
                            password_hash="unused",
                            role="super_admin",
                            is_active=True,
                        )
                    )
                    await db.flush()
                    db.add(
                        Project(
                            id=project_id,
                            owner_id=actor_id,
                            display_id=f"P-SM-{project_id.hex[:8]}",
                            name="Slice migration",
                            type_key="image-seg",
                            type_label="图像分割",
                            data_type="image",
                        )
                    )
                    await db.flush()
                    db.add(
                        Task(
                            id=task_id,
                            project_id=project_id,
                            display_id=f"T-SM-{task_id.hex[:8]}",
                            file_name="migration.png",
                            file_path="migration.png",
                            file_type="image",
                        )
                    )
                    await db.flush()
                    db.add(
                        AnnotationOperation(
                            id=operation_id,
                            task_id=task_id,
                            actor_id=actor_id,
                            kind="slice_polygon",
                            idempotency_key=uuid.uuid4().hex,
                            request_digest="a" * 64,
                            scope_fingerprint="b" * 64,
                            response_json={},
                        )
                    )
                    await db.commit()
                elif action == "verify":
                    assert (
                        await db.scalar(text("SELECT version_num FROM alembic_version"))
                        == ScriptDirectory.from_config(config).get_current_head()
                    )
                    assert (
                        await db.scalar(
                            select(AnnotationOperation.kind).where(
                                AnnotationOperation.id == operation_id
                            )
                        )
                        == "slice_polygon"
                    )
                    checks = (
                        await db.execute(
                            text(
                                "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE conname IN ('ck_annotation_operations_kind', 'ck_annotation_lineage_relation')"
                            )
                        )
                    ).all()
                    assert (
                        "restore_slice" in dict(checks)["ck_annotation_operations_kind"]
                    )
                    assert (
                        "slice_restored"
                        in dict(checks)["ck_annotation_lineage_relation"]
                    )
                else:
                    await db.execute(delete(Task).where(Task.id == task_id))
                    await db.execute(delete(Project).where(Project.id == project_id))
                    await db.execute(delete(User).where(User.id == actor_id))
                    await db.commit()
        finally:
            await engine.dispose()

    asyncio.run(inspect("seed"))
    try:
        with pytest.raises(RuntimeError, match="Slice audit data exists"):
            asyncio.run(roundtrip(used=True))
        asyncio.run(inspect("verify"))
    finally:
        asyncio.run(inspect("cleanup"))
