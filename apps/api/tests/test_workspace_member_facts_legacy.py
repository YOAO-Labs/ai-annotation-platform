"""Consumer-visible membership reads through current owners."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from types import SimpleNamespace
import uuid

from fastapi import HTTPException
import pytest
from sqlalchemy import text

from app.db.models.project import Project
from app.db.models.task import Task
from app.services.annotation_comment import AnnotationCommentService
from app.services.notification import NotificationService
from app.services.project_aggregates import member_user_ids
from app.services.project_performance import ResolvedScope, _build_roster
from tests.factory import create_user
from tests.test_workspace_project_authority_locking import _world

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires owned module-data resources",
    ),
]
_STAMP = datetime(2026, 1, 1, tzinfo=timezone.utc)
_SCOPE = ResolvedScope(_STAMP, _STAMP, "UTC", _STAMP)


def _pending(world):
    identity = uuid.uuid4()
    return Task(
        id=identity,
        project_id=world["project_id"],
        display_id=f"MF-{identity.hex[:20]}",
        file_name="pending.png",
        file_path=f"member-facts/{identity}.png",
        file_type="image",
        status="pending",
    )


async def _roster(db, project):
    return await _build_roster(
        db, project, include_historical=False, account_status="all", query=None, scope=_SCOPE
    )


async def test_roster_reflects_current_membership(test_engine):
    async with _world(test_engine) as (owner, _api, world, _url):
        async with owner.begin() as seed:
            await seed.execute(
                text("UPDATE public.project_members SET assigned_at=:stamp WHERE id=:id"),
                {"id": world["membership_id"], "stamp": _STAMP},
            )
        async with owner() as db:
            project = await db.get(Project, world["project_id"])
            rows = {row.user.id: row for row in await _roster(db, project)}
            assert set(rows) == {world["owner_id"], world["member_id"]}
            assert rows[world["owner_id"]].is_owner
            member = rows[world["member_id"]]
            assert (member.project_role, member.is_current_member, member.member_since) == (
                "annotator", True, _STAMP
            )
        async with owner.begin() as change:
            await change.execute(
                text("DELETE FROM public.project_members WHERE id=:id"),
                {"id": world["membership_id"]},
            )
        async with owner() as db:
            project = await db.get(Project, world["project_id"])
            assert {row.user.id for row in await _roster(db, project)} == {world["owner_id"]}


async def test_member_ids_only_include_current_project_members(test_engine):
    async with _world(test_engine) as (owner, _api, world, _url):
        async with owner() as db:
            assert list(await db.scalars(member_user_ids(world["project_id"]))) == [world["member_id"]]
            assert list(await db.scalars(member_user_ids(uuid.uuid4()))) == []
            await db.rollback()


async def test_delivery_enforces_membership_and_export_scope(test_engine):
    async with _world(test_engine) as (owner, _api, world, _url):
        async with owner.begin() as seed:
            outsider = await create_user(
                seed, "employee", f"mf-{uuid.uuid4().hex}@example.test", "Outsider"
            )
            outsider_id = outsider.id
            world["actor_ids"].append(outsider_id)
        ids = [world["member_id"], world["member_id"], world["owner_id"], outsider_id, outsider_id]
        scopes = [(True, world["project_id"], value) for value in [False, True, True, True]]
        scopes.append((False, None, False))
        async with owner() as db:
            allowed = await NotificationService(db)._allowed_delivery_indices(
                [SimpleNamespace(user_id=identity) for identity in ids], scopes
            )
            assert allowed == {0, 2, 4}
            await db.rollback()


@pytest.mark.parametrize("date_column", ["assigned_at", "updated_at"])
@pytest.mark.parametrize("operation", ["member_ids", "mentions", "delivery", "roster"])
async def test_partial_member_reads_ignore_unrelated_out_of_range_dates(
    test_engine, date_column, operation
):
    async with _world(test_engine) as (owner, _api, world, _url):
        async with owner.begin() as change:
            await change.execute(
                text(f"UPDATE project_members SET {date_column}=TIMESTAMPTZ '10000-01-01' WHERE id=:id"),
                {"id": world["membership_id"]},
            )
        async with owner() as db:
            project = await db.get(Project, world["project_id"])
            if operation == "member_ids":
                assert list(await db.scalars(member_user_ids(project.id))) == [world["member_id"]]
            elif operation == "mentions":
                await AnnotationCommentService(db).validate_mentions(project.id, [world["member_id"]])
            elif operation == "delivery":
                assert await NotificationService(db)._allowed_delivery_indices(
                    [SimpleNamespace(user_id=world["member_id"])], [(True, project.id, False)]
                ) == {0}
            else:
                with pytest.raises(OverflowError):
                    await _roster(db, project)
            await db.rollback()


async def test_mentions_keep_any_role_inactive_and_missing_order(test_engine):
    async with _world(test_engine) as (owner, _api, world, _url):
        async with owner.begin() as change:
            await change.execute(
                text("UPDATE project_members SET role='unknown' WHERE id=:id"),
                {"id": world["membership_id"]},
            )
            await change.execute(
                text("UPDATE users SET is_active=false WHERE id IN (:member,:owner)"),
                {"member": world["member_id"], "owner": world["owner_id"]},
            )
        async with owner() as db:
            service = AnnotationCommentService(db)
            await service.validate_mentions(world["project_id"], [world["member_id"], world["owner_id"]])
            missing = [uuid.uuid4(), uuid.uuid4()]
            with pytest.raises(HTTPException) as invalid:
                await service.validate_mentions(
                    world["project_id"], [missing[1], world["member_id"], missing[0], missing[1]]
                )
            assert invalid.value.status_code == 422
            assert invalid.value.detail["error"] == "mentions_invalid"
            assert [uuid.UUID(value) for value in invalid.value.detail["non_member_user_ids"]] == [
                missing[1], missing[0], missing[1]
            ]
            await db.rollback()
