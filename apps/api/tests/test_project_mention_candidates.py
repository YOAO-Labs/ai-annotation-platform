"""Discussion @ mention candidates: owner + platform super admins + members."""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tests.factory import create_membership, create_project, create_user

pytestmark = pytest.mark.asyncio


def _headers(principal) -> dict[str, str]:
    return {"Authorization": f"Bearer {principal[1]}"}


async def test_mention_candidates_include_owner_super_admin_and_members(
    httpx_client: httpx.AsyncClient,
    db_session: AsyncSession,
    project_admin,
    annotator,
    reviewer,
):
    owner, _ = project_admin
    project = await create_project(db_session, owner_id=owner.id, name="Mention QA")
    # annotator / reviewer are project members; the owner is deliberately not a member row.
    await create_membership(
        db_session, project_id=project.id, user_id=annotator[0].id, role="annotator"
    )
    await create_membership(
        db_session, project_id=project.id, user_id=reviewer[0].id, role="reviewer"
    )
    root = await create_user(
        db_session, "super_admin", "root-mention@test.local", "Root Admin"
    )
    await db_session.flush()

    resp = await httpx_client.get(
        f"/api/v1/projects/{project.id}/mention-candidates",
        headers=_headers(annotator),
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()
    kinds = {item["user_id"]: item["kind"] for item in items}
    assert kinds[str(owner.id)] == "owner"
    assert kinds[str(root.id)] == "super_admin"
    assert kinds[str(annotator[0].id)] == "member"
    # 顺序：负责人 → 超管 → 成员。
    ids = [item["user_id"] for item in items]
    assert ids[0] == str(owner.id)
    assert ids.index(str(root.id)) < ids.index(str(annotator[0].id))


async def test_mention_candidates_dedupe_by_user_id(
    httpx_client: httpx.AsyncClient,
    db_session: AsyncSession,
    project_admin,
    super_admin,
    annotator,
):
    owner, _ = project_admin
    project = await create_project(db_session, owner_id=owner.id, name="Mention Dedupe")
    # The super admin is also a project member: one entry, labeled super_admin.
    await create_membership(
        db_session, project_id=project.id, user_id=super_admin[0].id, role="reviewer"
    )
    await create_membership(
        db_session, project_id=project.id, user_id=annotator[0].id, role="annotator"
    )
    await db_session.flush()

    resp = await httpx_client.get(
        f"/api/v1/projects/{project.id}/mention-candidates",
        headers=_headers(annotator),
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()
    super_entries = [
        item for item in items if item["user_id"] == str(super_admin[0].id)
    ]
    assert len(super_entries) == 1
    assert super_entries[0]["kind"] == "super_admin"


async def test_mention_candidates_require_project_visibility(
    httpx_client: httpx.AsyncClient,
    db_session: AsyncSession,
    project_admin,
    annotator,
):
    owner, _ = project_admin
    project = await create_project(
        db_session, owner_id=owner.id, name="Mention Private"
    )
    # annotator is not a member of this project.
    resp = await httpx_client.get(
        f"/api/v1/projects/{project.id}/mention-candidates",
        headers=_headers(annotator),
    )
    assert resp.status_code == 404, resp.text
