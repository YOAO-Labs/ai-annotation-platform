"""Management list, statistics, and export filters share one visible scope."""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.user_invitation import UserInvitation
from app.db.models.user import User
from tests.factory import create_membership, create_project, create_user

pytestmark = pytest.mark.asyncio


def _headers(principal: tuple[User, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {principal[1]}"}


async def test_users_query_stats_and_export_keep_the_same_filtered_scope(
    httpx_client: httpx.AsyncClient,
    project_admin,
    super_admin,
    db_session: AsyncSession,
):
    manager, _ = project_admin
    admin, _ = super_admin
    project = await create_project(db_session, owner_id=manager.id)
    managed = await create_user(
        db_session,
        "employee",
        "contract-managed@example.test",
        "Contract Managed",
    )
    outside = await create_user(
        db_session,
        "viewer",
        "contract-outside@example.test",
        "Contract Outside",
    )
    disabled_outside = await create_user(
        db_session,
        "employee",
        "contract-disabled@example.test",
        "Contract Disabled",
    )
    disabled_outside.is_active = False
    await create_membership(
        db_session,
        project_id=project.id,
        user_id=managed.id,
        role="annotator",
        assigned_by=manager.id,
    )
    await db_session.flush()

    params = {"status": "all", "search": "Contract"}
    query = await httpx_client.get(
        "/api/v1/users/query",
        params={**params, "page": 1, "page_size": 50},
        headers=_headers(project_admin),
    )
    stats = await httpx_client.get(
        "/api/v1/users/stats", params=params, headers=_headers(project_admin)
    )
    exported = await httpx_client.get(
        "/api/v1/users/export",
        params={**params, "format": "json"},
        headers=_headers(project_admin),
    )

    assert query.status_code == stats.status_code == exported.status_code == 200
    query_body = query.json()
    stats_body = stats.json()
    export_body = json.loads(exported.text)
    assert (
        query_body["total"]
        == stats_body["total"]
        == export_body["_export_meta"]["count"]
        == 1
    )
    assert query_body["items"][0]["id"] == str(managed.id)
    assert export_body["users"][0]["id"] == str(managed.id)
    # Read visibility excludes unassigned viewers and disabled accounts...
    assert str(outside.id) not in {row["id"] for row in query_body["items"]}
    assert str(disabled_outside.id) not in {row["id"] for row in query_body["items"]}

    # ...but does include enabled unassigned annotators (issue #115).
    enabled_outside = await create_user(
        db_session,
        "employee",
        "contract-enabled@example.test",
        "Contract Enabled",
    )
    await db_session.flush()
    enabled_params = {"status": "active", "search": "Contract Enabled"}
    enabled_query = await httpx_client.get(
        "/api/v1/users/query",
        params={**enabled_params, "page": 1, "page_size": 50},
        headers=_headers(project_admin),
    )
    enabled_export = await httpx_client.get(
        "/api/v1/users/export",
        params={**enabled_params, "format": "json"},
        headers=_headers(project_admin),
    )
    assert enabled_query.status_code == enabled_export.status_code == 200
    assert enabled_query.json()["total"] == 1
    assert enabled_query.json()["items"][0]["id"] == str(enabled_outside.id)
    assert json.loads(enabled_export.text)["users"][0]["id"] == str(enabled_outside.id)

    outside_project = await create_project(db_session, owner_id=admin.id)
    scoped = await httpx_client.get(
        "/api/v1/users/query",
        params={"status": "all", "project_id": outside_project.id},
        headers=_headers(project_admin),
    )
    assert scoped.status_code == 200
    assert scoped.json()["total"] == 0


async def test_invitations_query_stats_and_export_keep_the_same_filtered_scope(
    httpx_client: httpx.AsyncClient,
    project_admin,
    super_admin,
    db_session: AsyncSession,
):
    manager, _ = project_admin
    admin, _ = super_admin
    project = await create_project(db_session, owner_id=manager.id)
    now = datetime.now(timezone.utc)
    managed = UserInvitation(
        email="invite-contract-managed@example.test",
        role="annotator",
        group_name="Contract Group",
        project_id=project.id,
        token=secrets.token_urlsafe(24),
        expires_at=now + timedelta(days=1),
        invited_by=manager.id,
    )
    outside = UserInvitation(
        email="invite-contract-outside@example.test",
        role="annotator",
        group_name="Contract Group",
        project_id=project.id,
        token=secrets.token_urlsafe(24),
        expires_at=now + timedelta(days=1),
        invited_by=admin.id,
    )
    db_session.add_all([managed, outside])
    await db_session.flush()

    params = {"scope": "me", "status": "all", "search": "invite-contract"}
    query = await httpx_client.get(
        "/api/v1/invitations/query",
        params={**params, "page": 1, "page_size": 50},
        headers=_headers(project_admin),
    )
    stats = await httpx_client.get(
        "/api/v1/invitations/stats",
        params=params,
        headers=_headers(project_admin),
    )
    exported = await httpx_client.get(
        "/api/v1/invitations/export",
        params={**params, "format": "json"},
        headers=_headers(project_admin),
    )

    assert query.status_code == stats.status_code == exported.status_code == 200
    query_body = query.json()
    stats_body = stats.json()
    export_body = json.loads(exported.text)
    assert (
        query_body["total"]
        == stats_body["total"]
        == export_body["_export_meta"]["count"]
        == 1
    )
    assert query_body["items"][0]["id"] == str(managed.id)
    assert export_body["invitations"][0]["id"] == str(managed.id)
    assert str(outside.id) not in {row["id"] for row in query_body["items"]}

    forbidden = await httpx_client.get(
        "/api/v1/invitations/query",
        params={"scope": "all", "status": "all"},
        headers=_headers(project_admin),
    )
    assert forbidden.status_code == 403
