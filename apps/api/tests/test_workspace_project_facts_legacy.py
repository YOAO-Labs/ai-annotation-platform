"""Consumer-visible project facts through the Workspace owner."""

from __future__ import annotations

import os
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import text

from app.api.v1.training_hub import projects as training_projects
from app.application.task_dataset_backfill import list_project_ids
from app.db.models.project import Project
from tests.factory import create_project
from tests.test_workspace_project_authority_locking import _world

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires owned module-data resources",
    ),
]


def _actor(world, role="project_admin"):
    return SimpleNamespace(id=world["owner_id"], role=role, is_active=True)


async def test_training_projects_only_lists_managed_projects(test_engine):
    async with _world(test_engine) as (owner, _api, world, _url):
        search = f"training-{uuid.uuid4().hex}"
        async with owner.begin() as setup:
            other = await create_project(setup, owner_id=world["member_id"])
            world["project_ids"].append(other.id)
            own = await setup.get(Project, world["project_id"])
            own.name = f"{search}-own"
            other.name = f"{search}-other"
        async with owner() as db:
            page = await training_projects(
                search=search, limit=20, offset=0, user=_actor(world), db=db
            )
            assert page["total"] == 1
            assert [(row["id"], row["name"]) for row in page["items"]] == [
                (world["project_id"], f"{search}-own")
            ]
            await db.rollback()


async def test_id_backfill_project_filter_takes_precedence(test_engine):
    async with _world(test_engine) as (owner, _api, world, _url):
        async with owner() as db:
            assert await list_project_ids(
                db, project_id=world["project_id"], after=world["project_id"]
            ) == [world["project_id"]]
            await db.rollback()


@pytest.mark.parametrize("autoflush", [False, True])
async def test_super_admin_total_and_page_agree_for_pending_project(
    test_engine, autoflush
):
    async with _world(test_engine) as (owner, _api, world, _url):
        async with owner.begin() as setup:
            await setup.execute(
                text("UPDATE users SET role='super_admin' WHERE id=:id"),
                {"id": world["owner_id"]},
            )
        async with owner() as db:
            identity = uuid.uuid4()
            name = f"pending-catalog-{identity.hex}"
            pending = Project(
                display_id=f"P-{identity.hex[:18]}",
                name=name,
                type_key="image-det",
                type_label="Image",
                owner_id=world["owner_id"],
                tool_bindings={},
            )
            db.add(pending)
            db.autoflush = autoflush
            page = await training_projects(
                search=name,
                limit=20,
                offset=0,
                user=_actor(world, "super_admin"),
                db=db,
            )
            assert page["total"] == (1 if autoflush else 0)
            assert [row["name"] for row in page["items"]] == (
                [name] if autoflush else []
            )
            await db.rollback()
