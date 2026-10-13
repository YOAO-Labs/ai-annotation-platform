from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.core.security import create_access_token
from app.db.models.project_member import ProjectMember
from tests.factory import create_project, create_user

pytestmark = pytest.mark.asyncio


def headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_project_pages_are_stable_complete_and_keep_legacy_array(
    httpx_client, db_session, super_admin
):
    admin, token = super_admin
    created_at = datetime(2026, 1, 15, tzinfo=timezone.utc)
    projects = []
    for index in range(23):
        project = await create_project(
            db_session, owner_id=admin.id, name=f"pagination-{index:02}"
        )
        project.created_at = created_at
        projects.append(project)
    await db_session.flush()

    first = await httpx_client.get("/api/v1/projects/query", headers=headers(token))
    second = await httpx_client.get(
        "/api/v1/projects/query", params={"page": 2}, headers=headers(token)
    )
    legacy = await httpx_client.get("/api/v1/projects", headers=headers(token))
    assert first.status_code == second.status_code == legacy.status_code == 200
    first_page, second_page = first.json(), second.json()
    assert (first_page["total"], first_page["page_size"], first_page["pages"]) == (
        23,
        20,
        2,
    )
    assert len(first_page["items"]) == 20
    assert len(second_page["items"]) == 3
    expected_ids = [
        str(project.id)
        for project in sorted(projects, key=lambda p: p.id, reverse=True)
    ]
    actual_ids = [item["id"] for item in first_page["items"] + second_page["items"]]
    assert actual_ids == expected_ids
    assert [item["id"] for item in legacy.json()] == expected_ids
    assert first_page["items"][0]["owner_name"] == admin.name
    assert first_page["items"][0]["member_count"] == 0


async def test_project_page_count_and_filters_share_visible_scope(
    httpx_client, db_session, project_admin, super_admin, annotator
):
    manager, manager_token = project_admin
    admin, admin_token = super_admin
    member, member_token = annotator
    now = datetime(2026, 1, 15, tzinfo=timezone.utc)
    own = await create_project(db_session, owner_id=manager.id, name="matching-car")
    own.created_at = now
    own.data_type = "video"
    own.status = "in_progress"
    another = await create_project(db_session, owner_id=manager.id, name="other")
    another.created_at = now - timedelta(days=20)
    hidden = await create_project(db_session, owner_id=admin.id, name="matching-car")
    hidden.created_at = now
    hidden.data_type = "video"
    db_session.add_all(
        [
            ProjectMember(project_id=own.id, user_id=member.id, role="annotator"),
            ProjectMember(project_id=hidden.id, user_id=member.id, role="annotator"),
        ]
    )
    await db_session.flush()

    params = [
        ("page_size", "1"),
        ("status", "in_progress"),
        ("search", "matching"),
        ("type_key", "image-det"),
        ("data_type", "image"),
        ("data_type", "video"),
        ("member_id", str(member.id)),
        ("created_from", "2026-01-01"),
        ("created_to", "2026-01-31"),
    ]
    for token, expected_total, expected_ids in [
        (manager_token, 1, {str(own.id)}),
        (admin_token, 2, {str(own.id), str(hidden.id)}),
        (member_token, 2, {str(own.id), str(hidden.id)}),
    ]:
        response = await httpx_client.get(
            "/api/v1/projects/query", params=params, headers=headers(token)
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["total"] == expected_total
        assert body["pages"] == expected_total
        assert len(body["items"]) == 1
        assert body["items"][0]["id"] in expected_ids
        legacy = await httpx_client.get(
            "/api/v1/projects", params=params[1:], headers=headers(token)
        )
        assert legacy.status_code == 200, legacy.text
        assert {item["id"] for item in legacy.json()} == expected_ids

    outsider = await create_user(
        db_session, "viewer", f"outsider-{uuid.uuid4().hex}@test.local", "Outsider"
    )
    outsider_token = create_access_token(subject=str(outsider.id), role="viewer")
    response = await httpx_client.get(
        "/api/v1/projects/query", headers=headers(outsider_token)
    )
    assert response.status_code == 200
    assert response.json()["total"] == 0
    assert response.json()["items"] == []


async def test_project_page_empty_and_out_of_range(
    httpx_client, db_session, super_admin
):
    admin, token = super_admin
    empty = await httpx_client.get("/api/v1/projects/query", headers=headers(token))
    assert empty.status_code == 200
    assert empty.json() == {
        "items": [],
        "total": 0,
        "page": 1,
        "page_size": 20,
        "pages": 0,
    }
    await create_project(db_session, owner_id=admin.id)
    beyond = await httpx_client.get(
        "/api/v1/projects/query", params={"page": 2}, headers=headers(token)
    )
    assert beyond.status_code == 200
    assert beyond.json() == {
        "items": [],
        "total": 1,
        "page": 2,
        "page_size": 20,
        "pages": 1,
    }


async def test_project_date_filter_includes_the_entire_end_date(
    httpx_client, db_session, super_admin
):
    admin, token = super_admin
    project = await create_project(db_session, owner_id=admin.id)
    project.created_at = datetime(2026, 1, 31, 23, 59, 59, tzinfo=timezone.utc)
    await db_session.flush()
    response = await httpx_client.get(
        "/api/v1/projects/query",
        params={"created_from": "2026-01-31", "created_to": "2026-01-31"},
        headers=headers(token),
    )
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1


@pytest.mark.parametrize(
    "params",
    [
        {"page": 0},
        {"page": -1},
        {"page": "bad"},
        {"page_size": 0},
        {"page_size": 101},
        {"created_from": "bad"},
        {"created_from": "2026-02-01", "created_to": "2026-01-01"},
    ],
)
async def test_project_page_rejects_invalid_pagination(
    httpx_client, super_admin, params
):
    _, token = super_admin
    response = await httpx_client.get(
        "/api/v1/projects/query", params=params, headers=headers(token)
    )
    assert response.status_code == 422
