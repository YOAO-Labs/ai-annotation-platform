"""Direct group updates enforce the same managed-user boundary as bulk updates."""

import pytest

from app.db.models.group import Group
from tests.factory import create_user

pytestmark = pytest.mark.asyncio


async def test_direct_group_assignment_matches_bulk_managed_target_scope(
    httpx_client, project_admin, db_session
):
    _, token = project_admin
    headers = {"Authorization": f"Bearer {token}"}
    employee = await create_user(
        db_session, "employee", "group-scope-employee@test.local", "Employee"
    )
    outside_viewer = await create_user(
        db_session, "viewer", "group-scope-viewer@test.local", "Outside viewer"
    )
    group = Group(name="managed-group-scope")
    db_session.add(group)
    await db_session.flush()

    preview = await httpx_client.post(
        "/api/v1/users/groups/bulk/preview",
        json={
            "user_ids": [str(employee.id), str(outside_viewer.id)],
            "group_id": str(group.id),
        },
        headers=headers,
    )
    assert preview.status_code == 200, preview.text
    assert [item["ok"] for item in preview.json()["items"]] == [True, False]
    assert preview.json()["items"][1]["email"] is None

    allowed = await httpx_client.patch(
        f"/api/v1/users/{employee.id}/group",
        json={"group_id": str(group.id)},
        headers=headers,
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["group_id"] == str(group.id)

    denied = await httpx_client.patch(
        f"/api/v1/users/{outside_viewer.id}/group",
        json={"group_id": str(group.id)},
        headers=headers,
    )
    assert denied.status_code == 404, denied.text
    await db_session.refresh(outside_viewer)
    assert outside_viewer.group_id is None
    assert outside_viewer.group_name is None
