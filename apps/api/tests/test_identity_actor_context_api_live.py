"""Existing protected API callers use the shared admitted actor context."""

from __future__ import annotations

import os
import secrets
import uuid

import httpx
import pytest
from sqlalchemy import delete, select, text

from aap_identity.security import create_access_token
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.db.models.task_lock import TaskLock
from app.db.models.user import User
from tests.factory import create_membership, create_project, create_user
from tests.test_identity_admission_live import (
    _dsn,
    _start_api,
    _start_container_identity,
    _world,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_IDENTITY_ACTOR_CONTEXT_LIVE_ACCEPTANCE") != "1",
        reason="requires the explicitly enabled owned Identity actor-context API verifier",
    ),
]


def _api_dsn(test_engine):
    return test_engine.url.set(drivername="postgresql+asyncpg").render_as_string(
        hide_password=False
    )


async def test_actor_context_container_rejects_deep_json_with_closed_failure(
    test_engine, tmp_path
):
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    identity = await _start_container_identity(
        test_engine,
        tmp_path,
        secret,
        control,
        enable_accounts=False,
        enable_groups=False,
        enable_invitations=False,
        enable_directory=False,
        enable_admin_password=False,
        enable_platform_role=False,
        enable_suspension=False,
        enable_deactivation=False,
        enable_deletions=False,
        enable_offboarding=False,
        enable_presence=False,
    )
    try:
        async with httpx.AsyncClient(
            base_url=identity.origin, trust_env=False, timeout=30
        ) as client:
            response = await client.post(
                "/v1/identity/actor-context",
                headers={
                    "Authorization": f"Bearer {control}",
                    "Content-Type": "application/json",
                },
                content=b"[" * 2000 + b"0" + b"]" * 2000,
            )
            ready = await client.get("/ready")
        assert response.status_code == 400, response.text
        assert response.json() == {
            "schema": "aap-identity-actor/v1",
            "ok": False,
            "error": {"code": "invalid_request"},
        }
        assert ready.status_code == 200
    finally:
        identity.stop()


async def _cleanup_actor_project(maker, *, user_ids, project_ids):
    users = list(user_ids)
    projects = list(project_ids)
    async with maker.begin() as db:
        assert (await db.scalar(text("SELECT current_database()"))).endswith("_test")
        if projects:
            task_ids = select(Task.id).where(Task.project_id.in_(projects))
            await db.execute(
                delete(TaskLock).where(
                    TaskLock.user_id.in_(users) | TaskLock.task_id.in_(task_ids)
                )
            )
            await db.execute(delete(Task).where(Task.project_id.in_(projects)))
            await db.execute(
                delete(ProjectMember).where(
                    ProjectMember.project_id.in_(projects)
                    | ProjectMember.user_id.in_(users)
                )
            )
            await db.execute(delete(Project).where(Project.id.in_(projects)))
        await db.execute(delete(User).where(User.id.in_(users)))


@pytest.mark.parametrize("binding", ["remote_container", "dedicated_dsn"])
async def test_employee_dashboard_uses_actor_context_owner_binding(
    test_engine, tmp_path, binding
):
    maker, project_owner_id = await _world(test_engine)
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    suffix = uuid.uuid4().hex
    identity = api = None
    project_ids = []
    user_ids = [project_owner_id]
    try:
        async with maker.begin() as db:
            employee = await create_user(
                db,
                "employee",
                f"actor-context-dashboard-{suffix}@example.test",
                "Dashboard employee",
            )
            employee.weekly_target_default = 73
            project = await create_project(
                db, owner_id=project_owner_id, name=f"Actor context {suffix}"
            )
            project_ids.append(project.id)
            await create_membership(
                db,
                project_id=project.id,
                user_id=employee.id,
                role="annotator",
                assigned_by=project_owner_id,
            )
        user_ids.append(employee.id)

        configuration = {}
        if binding == "remote_container":
            identity = await _start_container_identity(
                test_engine,
                tmp_path,
                secret,
                control,
                enable_accounts=False,
                enable_groups=False,
                enable_invitations=False,
                enable_directory=False,
                enable_admin_password=False,
                enable_platform_role=False,
                enable_suspension=False,
                enable_deactivation=False,
                enable_deletions=False,
                enable_offboarding=False,
                enable_presence=False,
            )
            identity_origin = identity.origin
        else:
            identity_origin = ""
            configuration = {
                "IDENTITY_AUTH_DATABASE_URL": _dsn("AUTH", test_engine),
            }

        api = _start_api(
            tmp_path,
            secret,
            control,
            identity_origin,
            database_url=_api_dsn(test_engine),
            configuration=configuration,
        )
        token = create_access_token(
            str(employee.id),
            "employee",
            secret=secret,
            expire_minutes=15,
        )
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(
            base_url=api.origin, trust_env=False, timeout=60
        ) as client:
            response = await client.get("/api/v1/dashboard/annotator", headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["weekly_target"] == 73
        assert control not in response.text and secret not in response.text
    finally:
        if api:
            api.stop()
        if identity:
            identity.stop()
        await _cleanup_actor_project(
            maker,
            user_ids=user_ids,
            project_ids=project_ids,
        )
