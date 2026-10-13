"""Project access contract: capabilities, membership identity and isolation.

These tests exercise the B1 shared authorization resolver over real
PostgreSQL.  They create employees, not legacy global staff roles, because the
capability set derives from project membership only.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.db.models.project_member import ProjectMember
from app.db.models.user import User
from tests.factory import create_membership, create_project, create_user

pytestmark = pytest.mark.asyncio


def _headers(user: User) -> dict[str, str]:
    token = create_access_token(subject=str(user.id), role=user.role)
    return {"Authorization": f"Bearer {token}"}


async def _add_member(
    db: AsyncSession, *, project_id, user: User, role: str, assigned_by
) -> ProjectMember:
    return await create_membership(
        db,
        project_id=project_id,
        user_id=user.id,
        role=role,
        assigned_by=assigned_by,
    )


async def _access(client: httpx.AsyncClient, project_id, user: User) -> httpx.Response:
    return await client.get(
        f"/api/v1/projects/{project_id}/access", headers=_headers(user)
    )


async def test_access_capability_matrix(
    httpx_client: httpx.AsyncClient, super_admin, db_session: AsyncSession
):
    admin, _ = super_admin
    project = await create_project(db_session, owner_id=admin.id, name="Access QA")
    annotator = await create_user(
        db_session, "employee", f"acc-anno-{uuid.uuid4()}@test.local", "Anno"
    )
    reviewer = await create_user(
        db_session, "employee", f"acc-rev-{uuid.uuid4()}@test.local", "Rev"
    )
    viewer = await create_user(
        db_session, "viewer", f"acc-view-{uuid.uuid4()}@test.local", "View"
    )
    await _add_member(
        db_session,
        project_id=project.id,
        user=annotator,
        role="annotator",
        assigned_by=admin.id,
    )
    await _add_member(
        db_session,
        project_id=project.id,
        user=reviewer,
        role="reviewer",
        assigned_by=admin.id,
    )
    await _add_member(
        db_session,
        project_id=project.id,
        user=viewer,
        role="viewer",
        assigned_by=admin.id,
    )

    anno = (await _access(httpx_client, project.id, annotator)).json()
    assert anno["platform_role"] == "employee"
    assert anno["project_role"] == "annotator"
    assert anno["access_kind"] == "member"
    assert anno["is_manager"] is False
    assert anno["membership_id"] is not None
    assert anno["membership_version"] == 1
    assert "annotation.write" in anno["capabilities"]
    assert "export.annotations" not in anno["capabilities"]
    assert "performance.read" not in anno["capabilities"]

    rev = (await _access(httpx_client, project.id, reviewer)).json()
    assert rev["project_role"] == "reviewer"
    assert "review.write" in rev["capabilities"]
    assert "export.annotations" in rev["capabilities"]
    assert "annotation.write" not in rev["capabilities"]
    # Performance stays owner/super-admin only.
    assert "performance.read" not in rev["capabilities"]

    view = (await _access(httpx_client, project.id, viewer)).json()
    assert view["project_role"] == "viewer"
    assert "task.read" in view["capabilities"]
    assert "annotation.write" not in view["capabilities"]
    assert "review.write" not in view["capabilities"]
    assert "export.annotations" not in view["capabilities"]

    admin_access = (await _access(httpx_client, project.id, admin)).json()
    assert admin_access["access_kind"] == "super_admin"
    assert admin_access["is_manager"] is True
    assert "performance.read" in admin_access["capabilities"]


async def test_membership_refresh_beats_stale_identity_map(
    super_admin, db_session: AsyncSession
):
    """A cached membership must never stay authoritative after a change."""

    from sqlalchemy import update

    from app.services.project_access import resolve_project_access

    admin, _ = super_admin
    project = await create_project(db_session, owner_id=admin.id, name="Scroll QA")
    employee = await create_user(
        db_session, "employee", f"acc-scroll-{uuid.uuid4()}@test.local", "Scroll"
    )
    member = await _add_member(
        db_session,
        project_id=project.id,
        user=employee,
        role="annotator",
        assigned_by=admin.id,
    )

    first = await resolve_project_access(db_session, user=employee, project=project)
    assert first.project_role == "annotator"
    assert first.membership_version == 1

    # Change the row out-of-band so the identity map still holds the old role.
    await db_session.execute(
        update(ProjectMember)
        .where(ProjectMember.id == member.id)
        .values(role="reviewer", version=2)
        .execution_options(synchronize_session=False)
    )
    await db_session.flush()

    refreshed = await resolve_project_access(
        db_session, user=employee, project=project, lock_membership=True
    )
    assert refreshed.project_role == "reviewer"
    assert refreshed.membership_version == 2
    assert "review.write" in refreshed.capabilities


async def test_non_member_access_is_hidden(
    httpx_client: httpx.AsyncClient, super_admin, db_session: AsyncSession
):
    admin, _ = super_admin
    project = await create_project(db_session, owner_id=admin.id, name="Hidden QA")
    outsider = await create_user(
        db_session, "employee", f"acc-out-{uuid.uuid4()}@test.local", "Out"
    )
    response = await _access(httpx_client, project.id, outsider)
    assert response.status_code == 404, response.text


async def test_viewer_with_work_membership_fails_closed(
    httpx_client: httpx.AsyncClient, super_admin, db_session: AsyncSession
):
    admin, _ = super_admin
    project = await create_project(db_session, owner_id=admin.id, name="Closed QA")
    viewer = await create_user(
        db_session, "viewer", f"acc-bad-{uuid.uuid4()}@test.local", "Bad"
    )
    # Inconsistent row: a platform viewer must never hold a work membership.
    await _add_member(
        db_session,
        project_id=project.id,
        user=viewer,
        role="annotator",
        assigned_by=admin.id,
    )
    response = await _access(httpx_client, project.id, viewer)
    assert response.status_code == 403, response.text


async def test_project_export_denied_for_annotator_and_viewer(
    httpx_client: httpx.AsyncClient, super_admin, db_session: AsyncSession
):
    admin, _ = super_admin
    project = await create_project(db_session, owner_id=admin.id, name="Export QA")
    annotator = await create_user(
        db_session, "employee", f"exp-anno-{uuid.uuid4()}@test.local", "Anno"
    )
    viewer = await create_user(
        db_session, "viewer", f"exp-view-{uuid.uuid4()}@test.local", "View"
    )
    await _add_member(
        db_session,
        project_id=project.id,
        user=annotator,
        role="annotator",
        assigned_by=admin.id,
    )
    await _add_member(
        db_session,
        project_id=project.id,
        user=viewer,
        role="viewer",
        assigned_by=admin.id,
    )

    for principal in (annotator, viewer):
        response = await httpx_client.post(
            f"/api/v1/projects/{project.id}/export",
            headers=_headers(principal),
        )
        assert response.status_code == 403, response.text
