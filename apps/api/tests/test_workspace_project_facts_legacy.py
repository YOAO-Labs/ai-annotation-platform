"""Consumer-visible project facts through the Workspace owner."""

from __future__ import annotations

from datetime import datetime
import os
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import text

from app.api.v1.training_hub import projects as training_projects
from app.application.task_dataset_backfill import list_project_ids
from app.db.models.project import Project
from app.services.annotation_comment import AnnotationCommentService
from app.services.notification import NotificationService
from app.workers.export import _scope_fingerprint, _scope_naming
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


@pytest.mark.parametrize("date_column", ["due_date", "created_at", "updated_at"])
@pytest.mark.parametrize(
    "operation",
    ["ids", "mentions", "delivery", "training", "export_naming", "export_fingerprint"],
)
async def test_partial_project_reads_ignore_unrelated_out_of_range_dates(
    test_engine, date_column, operation
):
    async with _world(test_engine) as (owner, _api, world, _url):
        project_name = f"dated-project-{uuid.uuid4().hex}"
        async with owner.begin() as change:
            kind = "DATE" if date_column == "due_date" else "TIMESTAMPTZ"
            await change.execute(
                text(
                    f"UPDATE projects SET {date_column}={kind} '10000-01-01', "
                    "name=:name WHERE id=:id"
                ),
                {"id": world["project_id"], "name": project_name},
            )
        async with owner() as db:
            if operation == "ids":
                assert await list_project_ids(db, project_id=world["project_id"]) == [world["project_id"]]
            elif operation == "mentions":
                await AnnotationCommentService(db).validate_mentions(
                    world["project_id"], [world["owner_id"]]
                )
            elif operation == "delivery":
                assert await NotificationService(db)._allowed_delivery_indices(
                    [SimpleNamespace(user_id=world["owner_id"])],
                    [(True, world["project_id"], True)],
                ) == {0}
            elif operation == "training":
                error = ValueError if date_column == "due_date" else OverflowError
                with pytest.raises(error):
                    await training_projects(
                        search=project_name, limit=20, offset=0, user=_actor(world), db=db
                    )
            elif operation == "export_naming":
                media, dataset, display = await _scope_naming(db, world["project_id"], None)
                assert (media, dataset, display.startswith("P-E2E-")) == ("image", None, True)
            elif date_column == "updated_at":
                with pytest.raises(OverflowError):
                    await _scope_fingerprint(db, world["project_id"], None)
            else:
                stamp, count = await _scope_fingerprint(db, world["project_id"], None)
                assert isinstance(stamp, datetime) and count == 0
            await db.rollback()


@pytest.mark.parametrize("autoflush", [False, True])
async def test_super_admin_total_and_page_agree_for_pending_project(test_engine, autoflush):
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
                display_id=f"P-{identity.hex[:20]}", name=name,
                type_key="image-det", type_label="Image", owner_id=world["owner_id"],
                tool_bindings={},
            )
            db.add(pending)
            db.autoflush = autoflush
            page = await training_projects(
                search=name, limit=20, offset=0, user=_actor(world, "super_admin"), db=db
            )
            assert page["total"] == (1 if autoflush else 0)
            assert [row["name"] for row in page["items"]] == ([name] if autoflush else [])
            await db.rollback()
