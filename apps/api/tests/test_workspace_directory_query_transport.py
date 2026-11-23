"""Directory HTTP reads preserve caller writes and the separate Identity port."""

import pytest

from app.config import settings
from tests.factory import create_membership, create_project


@pytest.mark.asyncio
async def test_member_reads_see_pending_membership_and_keep_profile_failure_separate(
    db_session, httpx_client, project_admin, annotator, reviewer, monkeypatch, tmp_path
):
    for field in (
        "identity_url",
        "identity_auth_database_url",
        "identity_auth_database_url_file",
        "identity_briefs_database_url",
        "identity_briefs_database_url_file",
    ):
        monkeypatch.setattr(settings, field, "")
    owner, owner_token = project_admin
    member, member_token = annotator
    _, outsider_token = reviewer
    member.avatar_ref = "preset:blue"
    project = await create_project(db_session, owner_id=owner.id)
    target = f"/api/v1/projects/{project.id}"
    members = f"{target}/members"
    mentions = f"{target}/mention-candidates"
    owner_headers = {"Authorization": f"Bearer {owner_token}"}
    member_headers = {"Authorization": f"Bearer {member_token}"}
    outsider_headers = {"Authorization": f"Bearer {outsider_token}"}

    async with db_session.begin_nested() as savepoint:
        await create_membership(
            db_session, project_id=project.id, user_id=member.id, role="annotator"
        )
        visible = await httpx_client.get(members, headers=member_headers)
        assert visible.status_code == 200, visible.text
        (row,) = visible.json()
        assert set(row) == {
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
        assert row["user_name"] == member.name and row["avatar_ref"] == "preset:blue"

        empty_dsn = tmp_path / "identity-briefs-dsn"
        empty_dsn.write_text("\n")
        monkeypatch.setattr(
            settings, "identity_briefs_database_url_file", str(empty_dsn)
        )
        hidden = await httpx_client.get(members, headers=outsider_headers)
        assert hidden.status_code == 404
        assert hidden.json() == {"detail": "项目不存在"}
        unavailable = await httpx_client.get(members, headers=member_headers)
        assert unavailable.status_code == 503
        assert unavailable.json() == {"detail": "身份资料服务暂不可用"}
        candidates = await httpx_client.get(mentions, headers=member_headers)
        assert candidates.status_code == 200, candidates.text
        assert candidates.json()[0]["user_id"] == str(owner.id)
        assert any(item["user_id"] == str(member.id) for item in candidates.json()[1:])
        await savepoint.rollback()

    rolled_back = await httpx_client.get(members, headers=owner_headers)
    assert rolled_back.status_code == 200, rolled_back.text
    assert rolled_back.json() == []
    former_member = await httpx_client.get(members, headers=member_headers)
    assert former_member.status_code == 404
