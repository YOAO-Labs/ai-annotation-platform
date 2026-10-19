"""Image cutover refuses unresolved legacy writers and retained source state."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.task import Task
from tests.factory import create_project
from tests.test_assets_catalog_native import _seed_actor

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned disposable module-data verifier",
    ),
]


def _invoke(connection, direction):
    filenames = ["0220_assets_images.py"]
    if direction == "downgrade":
        # Remove the newer empty input boundary first, as the real migration
        # chain does. Rolling back the test transaction restores both revisions.
        filenames.insert(0, "0221_accepted_inputs.py")
    for filename in filenames:
        path = Path(__file__).resolve().parents[1] / "alembic/versions" / filename
        spec = importlib.util.spec_from_file_location("image_migration_guard", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with Operations.context(MigrationContext.configure(connection)):
            getattr(module, direction)()


@pytest.mark.parametrize("legacy", ["pending", "building", "import", "ingest"])
async def test_upgrade_refuses_legacy_writers_and_restores_pre_cutover_schema(
    test_engine, legacy
):
    async with test_engine.connect() as connection:
        transaction = await connection.begin()
        try:
            task_id = uuid4()
            async with AsyncSession(bind=connection, expire_on_commit=False) as db:
                actor = await _seed_actor(db)
                project = await create_project(db, owner_id=actor)
                db.add(
                    Task(
                        id=task_id,
                        project_id=project.id,
                        display_id=f"T-GUARD-{task_id.hex[:12]}",
                        file_name="guard.png",
                        file_path=f"images/{task_id}.png",
                        file_type="image",
                        status="pending",
                    )
                )
                await db.flush()
            await connection.run_sync(_invoke, "downgrade")
            if legacy in {"pending", "building"}:
                asset = uuid4()
                await connection.execute(
                    text(
                        "INSERT INTO public.image_pyramid_assets(id,task_id,profile_version) VALUES (:id,:task,'pyramid-v1')"
                    ),
                    {"id": asset, "task": task_id},
                )
                await connection.execute(
                    text(
                        "INSERT INTO public.image_pyramid_generations(asset_id,generation,source_identity,status,normalization_version) VALUES (:id,1,'registered',:status,'exif-autorotate-srgb-v1')"
                    ),
                    {"id": asset, "status": legacy},
                )
            else:
                table, parent = (
                    ("dataset_import_deliveries", "job_id")
                    if legacy == "import"
                    else ("dataset_source_ingest_deliveries", "operation_id")
                )
                await connection.execute(
                    text(
                        f"INSERT INTO public.{table}(id,{parent},kind,target_id) VALUES (:id,:parent,'thumbnail',:target)"
                    ),
                    {"id": uuid4(), "parent": uuid4(), "target": task_id},
                )
            with pytest.raises(DBAPIError, match="Drain .*legacy"):
                async with connection.begin_nested():
                    await connection.run_sync(_invoke, "upgrade")
            assert await connection.scalar(
                text("SELECT to_regclass('public.asset_image_jobs') IS NULL")
            )
            assert (
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM information_schema.columns WHERE table_schema='public' AND table_name='tasks' AND column_name='media_source_generation'"
                    )
                )
                == 0
            )
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM public.tasks WHERE id=:id"),
                    {"id": task_id},
                )
                == 1
            )
        finally:
            await transaction.rollback()
        assert await connection.scalar(
            text("SELECT to_regclass('public.asset_image_jobs') IS NOT NULL")
        )


async def test_downgrade_preserves_retained_task_source_evidence(test_engine):
    async with test_engine.connect() as connection:
        transaction = await connection.begin()
        try:
            task_id = uuid4()
            async with AsyncSession(bind=connection, expire_on_commit=False) as db:
                actor = await _seed_actor(db)
                project = await create_project(db, owner_id=actor)
                db.add(
                    Task(
                        id=task_id,
                        project_id=project.id,
                        display_id=f"T-RETAIN-{task_id.hex[:12]}",
                        file_name="guard.png",
                        file_path=f"images/{task_id}.png",
                        file_type="image",
                        status="pending",
                        media_object_identity={"etag": "registered", "byte_size": 128},
                    )
                )
                await db.flush()
            with pytest.raises(
                RuntimeError, match="Cannot downgrade retained image media work"
            ):
                await connection.run_sync(_invoke, "downgrade")
            assert await connection.scalar(
                text("SELECT to_regclass('public.asset_image_jobs') IS NOT NULL")
            )
            assert (
                await connection.scalar(
                    text(
                        "SELECT media_object_identity->>'etag' FROM public.tasks WHERE id=:id"
                    ),
                    {"id": task_id},
                )
                == "registered"
            )
        finally:
            await transaction.rollback()
