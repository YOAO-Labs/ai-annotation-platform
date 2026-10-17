"""An admitted project mutation fences every authority source until commit."""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.user import User
from app.services.project_access import resolve_project_access
from tests.factory import create_membership, create_project, create_user


@pytest.mark.parametrize(
    "revocation", ["disabled_account", "transferred_owner", "changed_member_role"]
)
async def test_final_project_authority_serializes_with_revocation(
    test_engine, revocation
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as setup:
        owner = await create_user(
            setup, "project_admin", f"owner-{uuid.uuid4()}@authority.test", "Owner"
        )
        next_owner = await create_user(
            setup, "project_admin", f"next-{uuid.uuid4()}@authority.test", "Next owner"
        )
        member = await create_user(
            setup, "employee", f"member-{uuid.uuid4()}@authority.test", "Member"
        )
        project = await create_project(setup, owner_id=owner.id)
        membership = await create_membership(
            setup, project_id=project.id, user_id=member.id, role="annotator"
        )
        user_ids, project_id, member_id = (
            [owner.id, next_owner.id, member.id],
            project.id,
            membership.id,
        )
        actor_id = member.id if revocation == "changed_member_role" else owner.id
        if revocation == "disabled_account":
            model, identity, values = User, actor_id, {"is_active": False}
        elif revocation == "transferred_owner":
            model, identity, values = Project, project_id, {"owner_id": next_owner.id}
        else:
            model, identity, values = (
                ProjectMember,
                member_id,
                {"role": "viewer", "version": 2},
            )
        await setup.commit()
    try:
        async with maker() as writer:
            actor = await writer.get(User, actor_id)
            target = await writer.get(Project, project_id)
            access = await resolve_project_access(
                writer, actor_id=actor.id, project=target, lock_membership=True
            )
            assert access.has("annotation.write")
            async with maker() as revoker:
                with pytest.raises(DBAPIError) as busy:
                    await revoker.execute(
                        select(model.id)
                        .where(model.id == identity)
                        .with_for_update(nowait=True)
                    )
                assert getattr(busy.value.orig, "sqlstate", None) == "55P03"
                await revoker.rollback()
            await writer.commit()
            async with maker() as revoker:
                await revoker.execute(
                    update(model).where(model.id == identity).values(**values)
                )
                await revoker.commit()
            if revocation == "changed_member_role":
                refreshed = await resolve_project_access(
                    writer, actor_id=actor.id, project=target, lock_membership=True
                )
                assert (
                    not refreshed.has("annotation.write")
                    and refreshed.project_role == "viewer"
                )
            else:
                with pytest.raises(HTTPException) as denied:
                    await resolve_project_access(
                        writer, actor_id=actor.id, project=target, lock_membership=True
                    )
                assert denied.value.status_code == (
                    401 if revocation == "disabled_account" else 404
                )
    finally:
        async with maker() as cleanup:
            await cleanup.execute(delete(Project).where(Project.id == project_id))
            await cleanup.execute(delete(User).where(User.id.in_(user_ids)))
            await cleanup.commit()
