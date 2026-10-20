"""Existing authorized HTTP callers read UserBrief through Identity owners."""

from __future__ import annotations

import os
import secrets
import uuid

import httpx
import pytest
from sqlalchemy import text

from tests.factory import (
    create_batch,
    create_membership,
    create_project,
    create_task,
    create_user,
)
from tests.test_identity_admission_live import (
    _dsn,
    _start_api,
    _start_container_identity,
    _token,
    _world,
)
from tests.test_identity_briefs_live import (
    _cleanup_briefs,
    _legacy_fixture,
    _seed_briefs_world,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_IDENTITY_BRIEFS_LIVE_ACCEPTANCE") != "1",
        reason="requires the explicitly enabled owned Identity briefs API verifier",
    ),
]


def _api_dsn(test_engine):
    return test_engine.url.set(drivername="postgresql+asyncpg").render_as_string(
        hide_password=False
    )


async def _caller_world(maker, actor_id, suffix):
    async with maker.begin() as db:
        project = await create_project(
            db, owner_id=actor_id, name=f"Authorized brief callers {suffix}"
        )
        task_user = await create_user(
            db,
            "employee",
            f"brief-task-user-{suffix}@example.test",
            "ßeta task assignee",
        )
        task_user.is_active = False
        task_user.avatar_ref = None
        task = await create_task(db, project_id=project.id, status="pending")
        task.assignee_id = task_user.id
        batch_user = await create_user(
            db,
            "employee",
            f"brief-batch-user-{suffix}@example.test",
            "Batch reviewer",
        )
        await create_membership(
            db,
            project_id=project.id,
            user_id=batch_user.id,
            role="reviewer",
            assigned_by=actor_id,
        )
        batch = await create_batch(db, project_id=project.id, name="Brief reviewer")
        batch.reviewer_id = batch_user.id
        batch.assigned_user_ids = [str(batch_user.id)]
    return {
        "project_id": project.id,
        "task_id": task.id,
        "task_user_id": task_user.id,
        "batch_id": batch.id,
        "batch_user_id": batch_user.id,
    }


@pytest.mark.parametrize("binding", ["remote_container", "dedicated_dsn"])
async def test_authorized_task_and_batch_routes_use_selected_briefs_owner(
    test_engine, tmp_path, binding
):
    maker, actor_id = await _world(test_engine)
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    suffix = uuid.uuid4().hex
    fixture_world = {"user_ids": [], "project_ids": []}
    caller = None
    user_ids = [actor_id]
    project_ids = []
    identity = api = None
    try:
        fixture_world = await _seed_briefs_world(maker, _legacy_fixture())
        user_ids.extend(fixture_world["user_ids"])
        project_ids.extend(fixture_world["project_ids"])
        caller = await _caller_world(maker, actor_id, suffix)
        user_ids.extend([caller["task_user_id"], caller["batch_user_id"]])
        project_ids.append(caller["project_id"])

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
                enable_briefs=True,
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
                "IDENTITY_BRIEFS_DATABASE_URL": _dsn("BRIEFS", test_engine),
            }

        api = _start_api(
            tmp_path,
            secret,
            control,
            identity_origin,
            database_url=_api_dsn(test_engine),
            configuration=configuration,
        )
        headers = {"Authorization": f"Bearer {_token(actor_id, secret)}"}
        async with httpx.AsyncClient(
            base_url=api.origin, trust_env=False, timeout=120
        ) as client:
            if binding == "dedicated_dsn":
                async with maker.begin() as db:
                    await db.execute(
                        text("GRANT SELECT ON public.users TO identity_briefs_client")
                    )
                try:
                    rejected = await client.get(
                        f"/api/v1/tasks/{caller['task_id']}", headers=headers
                    )
                    assert rejected.status_code == 503, rejected.text
                    assert secret not in rejected.text and control not in rejected.text
                finally:
                    async with maker.begin() as db:
                        await db.execute(
                            text(
                                "REVOKE SELECT ON public.users "
                                "FROM identity_briefs_client"
                            )
                        )

            task_response = await client.get(
                f"/api/v1/tasks/{caller['task_id']}", headers=headers
            )
            assert task_response.status_code == 200, task_response.text
            task_body = task_response.json()
            assignee = task_body["assignee"]
            assert set(assignee) == {
                "id",
                "name",
                "email",
                "role",
                "avatar_initial",
                "avatar_ref",
            }
            assert assignee == {
                "id": str(caller["task_user_id"]),
                "name": "ßeta task assignee",
                "email": f"brief-task-user-{suffix}@example.test",
                "role": "employee",
                "avatar_initial": "SS",
                "avatar_ref": None,
            }

            batch_response = await client.get(
                f"/api/v1/projects/{caller['project_id']}/batches/{caller['batch_id']}",
                headers=headers,
            )
            assert batch_response.status_code == 200, batch_response.text
            reviewer = batch_response.json()["reviewer"]
            assert set(reviewer) == {
                "id",
                "name",
                "email",
                "role",
                "avatar_initial",
                "avatar_ref",
            }
            assert reviewer == {
                "id": str(caller["batch_user_id"]),
                "name": "Batch reviewer",
                "email": f"brief-batch-user-{suffix}@example.test",
                "role": "reviewer",
                "avatar_initial": "B",
                "avatar_ref": None,
            }
    finally:
        if api:
            api.stop()
        if identity:
            identity.stop()
        await _cleanup_briefs(
            maker,
            user_ids=user_ids,
            project_ids=project_ids,
        )


@pytest.mark.asyncio
async def test_remote_briefs_owner_failure_does_not_fall_back_to_local_orm(
    test_engine, tmp_path
):
    maker, actor_id = await _world(test_engine)
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    suffix = uuid.uuid4().hex
    user_ids = [actor_id]
    project_ids = []
    identity = api = None
    try:
        caller = await _caller_world(maker, actor_id, suffix)
        user_ids.extend([caller["task_user_id"], caller["batch_user_id"]])
        project_ids.append(caller["project_id"])
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
            enable_briefs=False,
            enable_suspension=False,
            enable_deactivation=False,
            enable_deletions=False,
            enable_offboarding=False,
            enable_presence=False,
        )
        api = _start_api(
            tmp_path,
            secret,
            control,
            identity.origin,
            database_url=_api_dsn(test_engine),
        )
        headers = {"Authorization": f"Bearer {_token(actor_id, secret)}"}
        async with httpx.AsyncClient(
            base_url=api.origin, trust_env=False, timeout=60
        ) as client:
            response = await client.get(
                f"/api/v1/tasks/{caller['task_id']}", headers=headers
            )
        assert response.status_code == 503, response.text
        assert secret not in response.text and control not in response.text
    finally:
        if api:
            api.stop()
        if identity:
            identity.stop()
        await _cleanup_briefs(
            maker,
            user_ids=user_ids,
            project_ids=project_ids,
        )
