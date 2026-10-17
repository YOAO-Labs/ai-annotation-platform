"""Offboarding cannot authorize from project rows retained by an earlier preview."""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.application.user_lifecycle import UserLifecycleWorkflow
from app.db.models.project import Project
from app.db.models.user import User
from app.schemas.user import OffboardingCommitRequest
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
        try:
            preview = await UserLifecycleWorkflow.preview(
                retained, target_id=target_id, actor=owner
            )
            payload = OffboardingCommitRequest(
                mode="handoff",
                reason="Stale preview",
                preview_version=preview.preview_version,
                projects=[
                    {"project_id": project_id, "annotator_receiver_id": receiver.id}
                ],
            )
            await retained.commit()
            async with maker() as transfer:
                await transfer.execute(
                    update(Project)
                    .where(Project.id == project_id)
                    .values(owner_id=next_owner.id)
                )
                await transfer.commit()
            with pytest.raises(HTTPException) as denied:
                await UserLifecycleWorkflow.offboard(
                    retained, target_id=target_id, actor=owner, payload=payload
                )
            assert denied.value.status_code == 403
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
