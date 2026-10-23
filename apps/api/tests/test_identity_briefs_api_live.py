"""Existing authorized HTTP callers read UserBrief through Identity owners."""

from __future__ import annotations

import os
import secrets
import uuid

import httpx
import pytest
from sqlalchemy import text

from aap_identity.security import create_access_token
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
        caller = await create_user(
            db,
            "employee",
            f"brief-caller-{suffix}@example.test",
            "Employee annotator",
        )
        await create_membership(
            db,
            project_id=project.id,
            user_id=caller.id,
            role="annotator",
            assigned_by=actor_id,
        )
        outsider = await create_user(
            db,
            "employee",
            f"brief-outsider-{suffix}@example.test",
            "Unassigned employee",
        )
        task = await create_task(db, project_id=project.id, status="pending")
        task.assignee_id = caller.id
        batch_user = await create_user(
            db,
            "employee",
            f"brief-batch-user-{suffix}@example.test",
            "Batch reviewer",
        )
        task.reviewer_id = batch_user.id
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
        "caller_id": caller.id,
        "outsider_id": outsider.id,
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
        user_ids.extend(
            [caller["caller_id"], caller["outsider_id"], caller["batch_user_id"]]
        )
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
        caller_token = create_access_token(
            str(caller["caller_id"]),
            "employee",
            secret=secret,
            expire_minutes=15,
        )
        outsider_token = create_access_token(
            str(caller["outsider_id"]),
            "employee",
            secret=secret,
            expire_minutes=15,
        )
        headers = {"Authorization": f"Bearer {caller_token}"}
        outsider_headers = {"Authorization": f"Bearer {outsider_token}"}
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
            task_reviewer = task_body["reviewer"]
            assert set(assignee) == {
                "id",
                "name",
                "email",
                "role",
                "avatar_initial",
                "avatar_ref",
            }
            assert assignee == {
                "id": str(caller["caller_id"]),
                "name": "Employee annotator",
                "email": f"brief-caller-{suffix}@example.test",
                "role": "employee",
                "avatar_initial": "E",
                "avatar_ref": None,
            }
            assert set(task_reviewer) == {
                "id",
                "name",
                "email",
                "role",
                "avatar_initial",
                "avatar_ref",
            }
            assert task_reviewer["id"] == str(caller["batch_user_id"])
            assert task_reviewer["role"] == "employee"

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

            member_response = await client.get(
                f"/api/v1/projects/{caller['project_id']}/members", headers=headers
            )
            assert member_response.status_code == 200, member_response.text
            member_by_user = {item["user_id"]: item for item in member_response.json()}
            assert set(member_by_user) == {
                str(caller["caller_id"]),
                str(caller["batch_user_id"]),
            }
            member_reviewer = member_by_user[str(caller["batch_user_id"])]
            assert set(member_reviewer) == {
                "id",
                "user_id",
                "user_name",
                "user_email",
                "role",
                "platform_role",
                "version",
                "assigned_at",
                "updated_at",
                "avatar_ref",
            }
            assert member_reviewer["role"] == "reviewer"
            assert member_reviewer["platform_role"] == "employee"
            assert member_reviewer["user_name"] == "Batch reviewer"
            assert (
                member_reviewer["user_email"]
                == f"brief-batch-user-{suffix}@example.test"
            )
            assert member_reviewer["avatar_ref"] is None

            project_response = await client.get(
                f"/api/v1/projects/{caller['project_id']}", headers=headers
            )
            project_page = await client.get(
                "/api/v1/projects/query", params={"page_size": 1}, headers=headers
            )
            project_list = await client.get(
                "/api/v1/projects",
                params={"search": f"Authorized brief callers {suffix}"},
                headers=headers,
            )
            assert project_response.status_code == 200, project_response.text
            assert project_page.status_code == 200, project_page.text
            assert project_list.status_code == 200, project_list.text
            assert project_page.json()["total"] == 1
            assert len(project_list.json()) == 1
            for project_body in (
                project_response.json(),
                project_page.json()["items"][0],
                project_list.json()[0],
            ):
                assert project_body["id"] == str(caller["project_id"])
                assert project_body["owner_id"] == str(actor_id)
                assert project_body["owner_name"] == "Live Identity user"
                assert project_body["owner_avatar_ref"] is None
                assert project_body["my_project_role"] == "annotator"

            hidden_task = await client.get(
                f"/api/v1/tasks/{caller['task_id']}", headers=outsider_headers
            )
            assert hidden_task.status_code == 404, hidden_task.text
            assert hidden_task.json()["detail"] == "项目不存在"
            assert f"brief-caller-{suffix}@example.test" not in hidden_task.text
            assert "Employee annotator" not in hidden_task.text

            hidden_batch = await client.get(
                f"/api/v1/projects/{caller['project_id']}/batches/{caller['batch_id']}",
                headers=outsider_headers,
            )
            assert hidden_batch.status_code == 404, hidden_batch.text
            assert hidden_batch.json()["detail"] == "项目不存在"
            assert f"brief-batch-user-{suffix}@example.test" not in hidden_batch.text
            assert "Batch reviewer" not in hidden_batch.text
            hidden_members = await client.get(
                f"/api/v1/projects/{caller['project_id']}/members",
                headers=outsider_headers,
            )
            assert hidden_members.status_code == 404, hidden_members.text
            assert hidden_members.json() == {"detail": "项目不存在"}
            assert "Batch reviewer" not in hidden_members.text
            assert f"brief-batch-user-{suffix}@example.test" not in hidden_members.text
            hidden_project = await client.get(
                f"/api/v1/projects/{caller['project_id']}", headers=outsider_headers
            )
            assert hidden_project.status_code == 404, hidden_project.text
            assert hidden_project.json() == {"detail": "项目不存在"}
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
        user_ids.extend(
            [caller["caller_id"], caller["outsider_id"], caller["batch_user_id"]]
        )
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
        employee_token = create_access_token(
            str(caller["caller_id"]),
            "employee",
            secret=secret,
            expire_minutes=15,
        )
        headers = {"Authorization": f"Bearer {employee_token}"}
        async with httpx.AsyncClient(
            base_url=api.origin, trust_env=False, timeout=60
        ) as client:
            response = await client.get(
                f"/api/v1/tasks/{caller['task_id']}", headers=headers
            )
            member_response = await client.get(
                f"/api/v1/projects/{caller['project_id']}/members", headers=headers
            )
            project_response = await client.get(
                f"/api/v1/projects/{caller['project_id']}", headers=headers
            )
            project_page = await client.get("/api/v1/projects/query", headers=headers)
        assert response.status_code == 503, response.text
        assert secret not in response.text and control not in response.text
        assert member_response.status_code == 503, member_response.text
        assert (
            secret not in member_response.text and control not in member_response.text
        )
        assert project_response.status_code == 503, project_response.text
        assert project_page.status_code == 503, project_page.text
        assert (
            secret not in project_response.text and control not in project_response.text
        )
        assert secret not in project_page.text and control not in project_page.text
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
