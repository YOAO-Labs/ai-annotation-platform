"""Member responses compose Workspace rows with the existing Identity profile owner."""

from __future__ import annotations

import ast
import inspect
import json
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy import event

from app.api.v1 import projects
from app.core.security import create_access_token
from app.schemas.user import UserBrief
from app.services import user_brief, workspace_project_query
from tests.factory import create_membership, create_project, create_user

_FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/project_member_profiles_legacy.json").read_text()
)


async def _seed(db, owner_id):
    project = await create_project(
        db, owner_id=owner_id, name="Member profile baseline"
    )
    project.id = uuid.UUID(_FIXTURE["project_id"])
    await db.flush()
    members = []
    for item in _FIXTURE["inputs"]:
        user = await create_user(db, item["platform_role"], item["email"], item["name"])
        user.id = uuid.UUID(item["user_id"])
        user.is_active = item["active"]
        user.avatar_ref = item["avatar_ref"]
        if not item["active"]:
            user.disabled_kind = "suspended"
        await db.flush()
        member = await create_membership(
            db,
            project_id=project.id,
            user_id=user.id,
            role=item["role"],
            assigned_by=owner_id,
        )
        member.id = uuid.UUID(item["membership_id"])
        member.version = item["version"]
        member.assigned_at = datetime.fromisoformat(item["assigned_at"])
        member.updated_at = datetime.fromisoformat(item["updated_at"])
        members.append(member)
    await db.flush()
    return project, members


async def test_member_profiles_match_legacy_and_batch_once(
    db_session, httpx_client, project_admin, monkeypatch
):
    owner, token = project_admin
    project, members = await _seed(db_session, owner.id)
    calls = []
    native = []

    async def resolve(db, ids):
        ids = list(ids)
        calls.append(ids)
        return await user_brief.resolve_briefs(db, ids)

    def statement(_conn, _cursor, sql, *_args):
        if "aap_identity_briefs_v1.execute_briefs_command" in sql:
            native.append(1)

    monkeypatch.setattr(workspace_project_query, "resolve_briefs", resolve)
    event.listen(db_session.bind.sync_engine, "before_cursor_execute", statement)
    try:
        response = await httpx_client.get(
            f"/api/v1/projects/{project.id}/members",
            headers={"Authorization": "Bearer " + token},
        )
        assert response.status_code == 200, response.text
        assert response.json() == _FIXTURE["list_response"]
        assert len(calls) == len(native) == 1
        assert set(calls[0]) == {member.user_id for member in members}
        individual = [
            (await projects._member_out(db_session, member)).model_dump(mode="json")
            for member in members
        ]
        assert individual == _FIXTURE["member_out_responses"]
    finally:
        event.remove(db_session.bind.sync_engine, "before_cursor_execute", statement)


async def test_member_list_keeps_employee_access_and_denies_outsider_before_profiles(
    db_session, httpx_client, project_admin, annotator, monkeypatch
):
    owner, _ = project_admin
    project, members = await _seed(db_session, owner.id)
    resolver = AsyncMock(wraps=user_brief.resolve_briefs)
    monkeypatch.setattr(workspace_project_query, "resolve_briefs", resolver)
    token = create_access_token(str(members[0].user_id), "employee")
    response = await httpx_client.get(
        f"/api/v1/projects/{project.id}/members",
        headers={"Authorization": "Bearer " + token},
    )
    assert response.status_code == 200, response.text
    assert response.json() == _FIXTURE["list_response"]
    resolver.assert_awaited_once()
    resolver.reset_mock()
    _, outsider_token = annotator
    denied = await httpx_client.get(
        f"/api/v1/projects/{project.id}/members",
        headers={"Authorization": "Bearer " + outsider_token},
    )
    assert denied.status_code == 404
    assert denied.json() == {"detail": "项目不存在"}
    resolver.assert_not_awaited()


async def test_empty_member_list_does_not_acquire_identity_owner(
    db_session, httpx_client, project_admin, monkeypatch
):
    owner, token = project_admin
    project = await create_project(db_session, owner_id=owner.id)

    def forbid(*_args, **_kwargs):
        pytest.fail("An empty member list must not acquire the profile owner")

    monkeypatch.setattr(user_brief, "IdentityBriefsCommands", forbid)
    response = await httpx_client.get(
        f"/api/v1/projects/{project.id}/members",
        headers={"Authorization": "Bearer " + token},
    )
    assert response.status_code == 200, response.text
    assert response.json() == []


async def test_member_list_omits_absent_profiles_like_the_old_inner_join(
    db_session, httpx_client, project_admin, monkeypatch
):
    owner, token = project_admin
    project, members = await _seed(db_session, owner.id)
    profiles = await user_brief.resolve_briefs(
        db_session, [member.user_id for member in members[:-1]]
    )
    monkeypatch.setattr(
        workspace_project_query, "resolve_briefs", AsyncMock(return_value=profiles)
    )
    response = await httpx_client.get(
        f"/api/v1/projects/{project.id}/members",
        headers={"Authorization": "Bearer " + token},
    )
    assert response.status_code == 200, response.text
    assert response.json() == _FIXTURE["list_response"][1:]


@pytest.mark.parametrize("role", [None, "reviewer"])
def test_member_platform_role_validation_is_not_widened(role):
    item = _FIXTURE["member_out_responses"][0]
    member = SimpleNamespace(
        id=uuid.UUID(item["id"]),
        user_id=uuid.UUID(item["user_id"]),
        role=item["role"],
        version=item["version"],
        assigned_at=datetime.fromisoformat(item["assigned_at"]),
        updated_at=datetime.fromisoformat(item["updated_at"]),
    )
    target = (
        None
        if role is None
        else UserBrief(
            id=member.user_id,
            name="Legacy",
            email="legacy@example.test",
            role=role,
            avatar_initial="L",
            avatar_ref=None,
        )
    )
    with pytest.raises(ValidationError) as error:
        projects._member_value(member, target)
    assert any(item["loc"] == ("platform_role",) for item in error.value.errors())


def test_member_profile_functions_do_not_read_user_orm():
    for function in (
        projects.list_members,
        projects._member_out,
        projects._member_value,
    ):
        tree = ast.parse(inspect.getsource(function))
        assert not any(
            isinstance(node, ast.Name) and node.id == "User" for node in ast.walk(tree)
        )
