"""Project owner display reuses the global profile batch after visible selection."""

from __future__ import annotations

import json
import struct
import uuid
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import event

from app.services import user_brief
from tests.factory import create_membership, create_project, create_user

_FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/project_owner_profiles_legacy.json").read_text()
)
_RESPONSES = {item["phase"]: item["response"] for item in _FIXTURE["records"]}


def _persisted_response(phase):
    expected = json.loads(json.dumps(_RESPONSES[phase]))
    rows = expected if isinstance(expected, list) else expected.get("items", [])
    # The old seed session retained Python defaults in its ORM identity map.
    # Compare the persisted REAL values that a fresh public request reads.
    for row in rows:
        for field in ("box_threshold", "text_threshold"):
            row[field] = struct.unpack("!f", struct.pack("!f", row[field]))[0]
    return expected


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
    original_execute = user_brief.IdentityBriefsCommands.execute

    async def execute(commands, request):
        calls.append(set(request.payload.user_ids))
        return await original_execute(commands, request)

    def statement(_conn, _cursor, sql, *_args):
        if "aap_identity_briefs_v1.execute_briefs_command" in sql:
            native.append(1)

    monkeypatch.setattr(user_brief.IdentityBriefsCommands, "execute", execute)
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
            assert response.json() == _persisted_response(phase)
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

    original_execute = user_brief.IdentityBriefsCommands.execute

    async def execute(commands, request):
        calls.append(set(request.payload.user_ids))
        return await original_execute(commands, request)

    monkeypatch.setattr(user_brief.IdentityBriefsCommands, "execute", execute)
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

    async def forbid(*_args, **_kwargs):
        pytest.fail("An empty project page must not acquire the profile owner")

    monkeypatch.setattr(user_brief.IdentityBriefsCommands, "execute", forbid)
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
