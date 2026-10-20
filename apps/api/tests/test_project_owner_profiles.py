"""Project owner display reuses the global profile batch after visible selection."""

from __future__ import annotations

import ast
import inspect
import json
import uuid
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event

from app.api.v1 import projects as routes
from app.db.models.project import Project
from app.services import user_brief
from tests.factory import create_membership, create_project, create_user

_FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/project_owner_profiles_legacy.json").read_text()
)
_RESPONSES = {item["phase"]: item["response"] for item in _FIXTURE["records"]}


async def _seed(db):
    for item in _FIXTURE["owners"]:
        user = await create_user(db, item["role"], item["email"], item["name"])
        user.id = uuid.UUID(item["id"])
        user.is_active = item["active"]
        user.avatar_ref = item["avatar_ref"]
        if not item["active"]:
            user.disabled_kind = "suspended"
        await db.flush()
    projects = []
    for item in _FIXTURE["projects"]:
        project = await create_project(
            db, owner_id=uuid.UUID(item["owner_id"]), name=item["name"]
        )
        project.id = uuid.UUID(item["id"])
        project.display_id = item["display_id"]
        project.created_at = datetime.fromisoformat(item["created_at"])
        project.updated_at = datetime.fromisoformat(item["updated_at"])
        projects.append(project)
    await db.flush()
    return projects


async def test_owner_profiles_match_legacy_with_one_lookup_per_response(
    db_session, httpx_client, super_admin, monkeypatch
):
    _, token = super_admin
    projects = await _seed(db_session)
    calls = []
    native = []

    async def resolve(db, ids):
        ids = list(ids)
        calls.append(set(ids))
        return await user_brief.resolve_briefs(db, ids)

    def statement(_conn, _cursor, sql, *_args):
        if "aap_identity_briefs_v1.execute_briefs_command" in sql:
            native.append(1)

    monkeypatch.setattr(routes, "resolve_briefs", resolve)
    event.listen(db_session.bind.sync_engine, "before_cursor_execute", statement)
    try:
        for phase, path, params, expected_owners in (
            (
                "list",
                "/api/v1/projects",
                {"search": "project-owner-profiles-"},
                {p.owner_id for p in projects},
            ),
            (
                "page",
                "/api/v1/projects/query",
                {"search": "project-owner-profiles-", "page": 2, "page_size": 2},
                {projects[0].owner_id, projects[1].owner_id},
            ),
            (
                "detail",
                "/api/v1/projects/" + str(projects[1].id),
                {},
                {projects[1].owner_id},
            ),
        ):
            before = len(native)
            response = await httpx_client.get(
                path, params=params, headers={"Authorization": "Bearer " + token}
            )
            assert response.status_code == 200, response.text
            assert response.json() == _RESPONSES[phase]
            assert len(native) - before == 1
            assert calls[-1] == expected_owners
        assert len(calls) == 3
    finally:
        event.remove(db_session.bind.sync_engine, "before_cursor_execute", statement)


async def test_only_visible_returned_project_owners_are_requested(
    db_session, httpx_client, annotator, monkeypatch
):
    actor, token = annotator
    projects = await _seed(db_session)
    await create_membership(
        db_session, project_id=projects[2].id, user_id=actor.id, role="viewer"
    )
    calls = []

    async def resolve(db, ids):
        ids = list(ids)
        calls.append(set(ids))
        return await user_brief.resolve_briefs(db, ids)

    monkeypatch.setattr(routes, "resolve_briefs", resolve)
    visible = await httpx_client.get(
        "/api/v1/projects/query",
        params={"page_size": 1},
        headers={"Authorization": "Bearer " + token},
    )
    assert visible.status_code == 200, visible.text
    assert visible.json()["total"] == 1
    assert [item["id"] for item in visible.json()["items"]] == [str(projects[2].id)]
    assert calls == [{projects[2].owner_id}]
    calls.clear()
    hidden = await httpx_client.get(
        "/api/v1/projects/" + str(projects[1].id),
        headers={"Authorization": "Bearer " + token},
    )
    assert hidden.status_code == 404
    assert hidden.json() == {"detail": "项目不存在"}
    assert calls == []


async def test_empty_and_out_of_range_pages_never_acquire_profile_owner(
    db_session, httpx_client, super_admin, monkeypatch
):
    _, token = super_admin
    await _seed(db_session)

    def forbid(*_args, **_kwargs):
        pytest.fail("An empty project page must not acquire the profile owner")

    monkeypatch.setattr(user_brief, "IdentityBriefsCommands", forbid)
    for path, params in (
        ("/api/v1/projects", {"search": "does-not-exist"}),
        ("/api/v1/projects/query", {"page": 3, "page_size": 2}),
    ):
        response = await httpx_client.get(
            path, params=params, headers={"Authorization": "Bearer " + token}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert (body if isinstance(body, list) else body["items"]) == []


async def test_missing_profile_lookup_is_not_refetched_and_keeps_null_display(
    db_session, monkeypatch
):
    resolver = AsyncMock(return_value={})
    monkeypatch.setattr(routes, "resolve_briefs", resolver)
    project = Project(id=uuid.uuid4(), owner_id=uuid.uuid4())
    result = await routes._serialize_project(
        db_session,
        project,
        ai_completed_lookup={},
        batch_summary_lookup={},
        owner_lookup={},
    )
    assert {key: result[key] for key in ("owner_name", "owner_avatar_ref")} == _FIXTURE[
        "missing_owner_fields"
    ]
    resolver.assert_not_awaited()
    result = await routes._serialize_project(
        db_session, project, ai_completed_lookup={}, batch_summary_lookup={}
    )
    assert {key: result[key] for key in ("owner_name", "owner_avatar_ref")} == _FIXTURE[
        "missing_owner_fields"
    ]
    resolver.assert_awaited_once_with(db_session, [project.owner_id])


async def test_null_owner_does_not_trigger_profile_io(db_session, monkeypatch):
    def forbid(*_args, **_kwargs):
        pytest.fail("A missing owner ID must not acquire the profile owner")

    monkeypatch.setattr(user_brief, "IdentityBriefsCommands", forbid)
    project = Project(id=uuid.uuid4(), owner_id=None)
    result = await routes._serialize_project(
        db_session, project, ai_completed_lookup={}, batch_summary_lookup={}
    )
    assert result["owner_name"] is None and result["owner_avatar_ref"] is None


def test_project_owner_serializers_do_not_read_user_orm():
    for function in (routes._serialize_project, routes._serialize_project_list):
        tree = ast.parse(inspect.getsource(function))
        assert not any(
            isinstance(node, ast.Name) and node.id == "User" for node in ast.walk(tree)
        )
