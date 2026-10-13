"""Ownership-transfer endpoint regressions.

The transfer endpoint previously read the project and the target role without
any lock; it now serializes with platform-role changes through the shared
account -> project locking order, so a concurrent demotion and ownership
transfer cannot interleave into an unmanageable owner state (review comment:
"serialize role demotion with ownership transfer").
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from app.core.security import create_access_token
from app.db.models.project import Project
from tests.factory import create_project, create_user

pytestmark = pytest.mark.asyncio


def _headers(user) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {create_access_token(subject=str(user.id), role=user.role)}"
    }


async def _transfer(
    client: httpx.AsyncClient, project_id: uuid.UUID, new_owner_id: uuid.UUID, actor
) -> httpx.Response:
    return await client.post(
        f"/api/v1/projects/{project_id}/transfer",
        json={"new_owner_id": str(new_owner_id)},
        headers=_headers(actor),
    )


async def test_transfer_moves_ownership_to_project_admin(
    db_session, httpx_client, super_admin
):
    actor, _ = super_admin
    project = await create_project(
        db_session, owner_id=actor.id, name="Transfer Source"
    )
    target = await create_user(
        db_session,
        "project_admin",
        f"transfer-target-{uuid.uuid4().hex[:6]}@test.local",
        "PA",
    )

    response = await _transfer(httpx_client, project.id, target.id, actor)
    assert response.status_code == 200, response.text
    assert response.json()["owner_id"] == str(target.id)

    refreshed = await db_session.get(Project, project.id)
    await db_session.refresh(refreshed)
    assert refreshed.owner_id == target.id


async def test_transfer_rejects_non_project_admin_target(
    db_session, httpx_client, super_admin
):
    actor, _ = super_admin
    project = await create_project(
        db_session, owner_id=actor.id, name="Transfer Reject"
    )
    employee = await create_user(
        db_session,
        "employee",
        f"transfer-emp-{uuid.uuid4().hex[:6]}@test.local",
        "E",
    )

    response = await _transfer(httpx_client, project.id, employee.id, actor)
    assert response.status_code == 400


async def test_transfer_requires_active_target(db_session, httpx_client, super_admin):
    actor, _ = super_admin
    project = await create_project(db_session, owner_id=actor.id, name="Transfer IA")
    target = await create_user(
        db_session,
        "project_admin",
        f"transfer-inactive-{uuid.uuid4().hex[:6]}@test.local",
        "PA",
    )
    target.is_active = False
    await db_session.flush()

    response = await _transfer(httpx_client, project.id, target.id, actor)
    assert response.status_code == 404
