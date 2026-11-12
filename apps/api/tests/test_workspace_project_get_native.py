"""Current Project reads through the Workspace owner."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import delete, text

from app.db.models.project import Project
from app.db.models.task import Task
from app.services.labeling_core_read_relations import get_core_row, task_row
from app.services.project_access import get_workspace_project
from tests.test_workspace_project_authority_locking import _assert_private_denied, _world

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned module-data verifier",
    ),
]


@pytest.mark.parametrize("actor_present", [False, True])
async def test_project_read_observes_committed_changes_and_missing_project(
    test_engine, actor_present
):
    async with _world(test_engine) as (owner, api, world, _url):
        actor_id = world["member_id"] if actor_present else None
        async with owner.begin() as writer:
            project = await writer.get(Project, world["project_id"])
            project.name = "Current project"
        async with api() as db:
            await _assert_private_denied(db)
            project = await get_workspace_project(
                db, actor_id=actor_id, project_id=world["project_id"]
            )
            assert project.id == world["project_id"]
            assert project.name == "Current project"
            assert await get_workspace_project(
                db, actor_id=actor_id, project_id=uuid.uuid4()
            ) is None


async def test_restricted_api_task_access_uses_read_only_owner_snapshots(test_engine):
    from app.services.task_access import resolve_task_access
    from tests.factory import create_task

    async with _world(test_engine) as (owner, api, world, _url):
        async with owner.begin() as seed:
            task = await create_task(seed, project_id=world["project_id"])
            task_id = task.id
        try:
            async with api() as db:
                await db.execute(text("SET TRANSACTION READ ONLY"))
                await _assert_private_denied(db)
                before = (
                    await db.execute(text("SELECT pg_backend_pid(),txid_current()"))
                ).one()
                task = await get_core_row(db, task_row, task_id)
                access = await resolve_task_access(
                    db, task=task, actor_id=world["member_id"]
                )
                assert (
                    access.project_id == world["project_id"]
                    and access.project_role == "annotator"
                )
                assert (
                    await db.execute(text("SELECT pg_backend_pid(),txid_current()"))
                ).one() == before
        finally:
            async with owner.begin() as cleanup:
                await cleanup.execute(delete(Task).where(Task.id == task_id))


async def test_restricted_api_training_authority_keeps_existing_owner_fences(
    test_engine,
):
    from app.services.training_datasets import bind_training_authority
    from tests.test_workspace_project_access_step_native import _assert_nowait_conflict

    async with _world(test_engine) as (_owner, api, world, _url):
        async with api() as db:
            await _assert_private_denied(db)
            before = (
                await db.execute(text("SELECT pg_backend_pid(),txid_current()"))
            ).one()
            await bind_training_authority(db).assert_authority(
                project_id=world["project_id"], actor_id=world["owner_id"]
            )
            assert (
                await db.execute(text("SELECT pg_backend_pid(),txid_current()"))
            ).one() == before
            for table, identity in (
                ("users", world["owner_id"]),
                ("projects", world["project_id"]),
            ):
                await _assert_nowait_conflict(
                    test_engine,
                    f"SELECT id FROM public.{table} WHERE id=$1 FOR UPDATE NOWAIT",
                    identity,
                )
