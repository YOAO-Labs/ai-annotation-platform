"""Member authority must retain the original ORM timestamp decoding boundary."""

from __future__ import annotations

import os
import uuid

import httpx
import pytest
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.core.security import create_access_token
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.user import User
from app.deps import get_db
from tests.factory import create_membership, create_project, create_user

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]


@pytest.mark.parametrize(
    "column,expression,status",
    [
        ("assigned_at", "TIMESTAMPTZ '10000-01-01 00:00:00+00'", 500),
        ("updated_at", "TIMESTAMPTZ '0001-01-01 00:00:00 BC'", 500),
        ("assigned_at", "TIMESTAMPTZ 'infinity'", 200),
    ],
)
async def test_project_detail_matches_legacy_member_authority_timestamp_decoding(
    test_engine, app_module, monkeypatch, column, expression, status
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    users = []
    project_id = None

    async def request_db():
        async with maker() as db:
            yield db

    for name in (
        "workspace_project_query_url",
        "workspace_project_query_database_url",
        "workspace_project_query_database_url_file",
        "identity_url",
        "identity_briefs_database_url",
        "identity_briefs_database_url_file",
    ):
        monkeypatch.setattr(settings, name, "")
    monkeypatch.setattr(settings, "audit_async", True)
    monkeypatch.setattr("app.middleware.audit._enqueue_audit", lambda _payload: True)
    monkeypatch.setitem(app_module.dependency_overrides, get_db, request_db)
    try:
        async with maker.begin() as db:
            owner = await create_user(
                db, "project_admin", f"owner-{uuid.uuid4().hex}@example.test", "Owner"
            )
            actor = await create_user(
                db, "employee", f"member-{uuid.uuid4().hex}@example.test", "Member"
            )
            users = [owner.id, actor.id]
            project = await create_project(db, owner_id=owner.id)
            project_id = project.id
            member = await create_membership(
                db, project_id=project_id, user_id=actor.id, role="annotator"
            )
            assert column in {"assigned_at", "updated_at"}
            await db.execute(
                text(
                    f"UPDATE public.project_members SET {column}={expression} WHERE id=:id"
                ),
                {"id": member.id},
            )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app_module, raise_app_exceptions=False),
            base_url="http://member-authority",
            headers={
                "authorization": "Bearer "
                + create_access_token(str(actor.id), actor.role)
            },
        ) as client:
            # The frozen legacy full-member read decoded both timestamps before
            # permission; Directory and Project readers must retain that result.
            reference = await client.get(f"/api/v1/projects/{project_id}/members")
            assert reference.status_code == status, reference.text
            current = await client.get(f"/api/v1/projects/{project_id}")
            assert current.status_code == status, current.text
    finally:
        async with maker.begin() as db:
            if project_id is not None:
                await db.execute(
                    delete(ProjectMember).where(ProjectMember.project_id == project_id)
                )
                await db.execute(delete(Project).where(Project.id == project_id))
            if users:
                await db.execute(delete(User).where(User.id.in_(users)))
