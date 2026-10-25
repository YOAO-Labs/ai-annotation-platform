"""Offboarding cannot authorize from project rows retained by an earlier preview."""

from __future__ import annotations

import uuid

import httpx
from fastapi import FastAPI
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.v1.users import router
from app.core.security import create_access_token
from app.db.models.project import Project
from app.db.models.user import User
from app.deps import get_db
from tests.factory import create_membership, create_project, create_user


async def test_offboarding_refreshes_locked_project_after_transfer(test_engine):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as retained:
        key = uuid.uuid4().hex
        owner = await create_user(
            retained, "project_admin", f"old-{key}@snapshot.test", "Former owner"
        )
        next_owner = await create_user(
            retained, "project_admin", f"next-{key}@snapshot.test", "New owner"
        )
        target = await create_user(
            retained, "employee", f"target-{key}@snapshot.test", "Target"
        )
        receiver = await create_user(
            retained, "employee", f"receiver-{key}@snapshot.test", "Receiver"
        )
        project = await create_project(retained, owner_id=owner.id)
        for member in (target, receiver):
            await create_membership(
                retained, project_id=project.id, user_id=member.id, role="annotator"
            )
        await retained.commit()
        project_id, target_id = project.id, target.id
        user_ids = [owner.id, next_owner.id, target.id, receiver.id]
        headers = {
            "Authorization": "Bearer "
            + create_access_token(subject=str(owner.id), role=owner.role)
        }
        app = FastAPI()
        app.include_router(router, prefix="/api/v1/users")

        async def retained_database():
            yield retained

        app.dependency_overrides[get_db] = retained_database
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://snapshot.test"
            ) as client:
                preview = await client.get(
                    f"/api/v1/users/{target_id}/offboarding-preview", headers=headers
                )
                assert preview.status_code == 200, preview.text
                payload = {
                    "mode": "handoff",
                    "reason": "Stale preview",
                    "preview_version": preview.json()["preview_version"],
                    "projects": [
                        {
                            "project_id": str(project_id),
                            "annotator_receiver_id": str(receiver.id),
                        }
                    ],
                }
                await retained.commit()
                async with maker() as transfer:
                    await transfer.execute(
                        update(Project)
                        .where(Project.id == project_id)
                        .values(owner_id=user_ids[1])
                    )
                    await transfer.commit()
                assert project.owner_id == user_ids[0]
                denied = await client.post(
                    f"/api/v1/users/{target_id}/offboarding",
                    headers=headers,
                    json=payload,
                )
                assert denied.status_code == 403, denied.text
                assert denied.json()["detail"] == "该用户存在不在你管理范围内的项目"
            await retained.rollback()
            async with maker() as check:
                current_target = await check.get(User, target_id)
                current_project = await check.get(Project, project_id)
                assert current_target.is_active and current_target.disabled_kind is None
                assert current_project.owner_id == user_ids[1]
        finally:
            await retained.rollback()
            async with maker() as cleanup:
                await cleanup.execute(delete(Project).where(Project.id == project_id))
                await cleanup.execute(delete(User).where(User.id.in_(user_ids)))
                await cleanup.commit()
