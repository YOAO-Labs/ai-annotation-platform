"""Project HTTP visibility precedes enrichment through the Identity owner."""

import pytest

from app.config import settings
from tests.factory import create_project


@pytest.mark.asyncio
async def test_project_queries_keep_identity_briefs_failure_and_actor_filtering(
    db_session, httpx_client, project_admin, annotator, monkeypatch, tmp_path
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
    _, outsider_token = annotator
    owner.avatar_ref = "preset:blue"
    owner_headers = {"Authorization": f"Bearer {owner_token}"}
    outsider_headers = {"Authorization": f"Bearer {outsider_token}"}

    async with db_session.begin_nested() as savepoint:
        project = await create_project(
            db_session, owner_id=owner.id, name="Briefs failure"
        )
        path = f"/api/v1/projects/{project.id}"
        detail = await httpx_client.get(path, headers=owner_headers)
        assert detail.status_code == 200, detail.text
        assert detail.json()["owner_name"] == owner.name
        assert detail.json()["owner_avatar_ref"] == "preset:blue"
        assert owner.email not in detail.text

        empty_dsn = tmp_path / "identity-briefs-dsn"
        empty_dsn.write_text("\n")
        monkeypatch.setattr(
            settings, "identity_briefs_database_url_file", str(empty_dsn)
        )
        hidden = await httpx_client.get(path, headers=outsider_headers)
        assert hidden.status_code == 404
        assert hidden.json() == {"detail": "项目不存在"}
        empty = await httpx_client.get(
            "/api/v1/projects",
            params={"search": project.name},
            headers=outsider_headers,
        )
        assert empty.status_code == 200
        assert empty.json() == []

        unavailable = await httpx_client.get(path, headers=owner_headers)
        assert unavailable.status_code == 503
        assert unavailable.json() == {"detail": "身份资料服务暂不可用"}
        readiness = await httpx_client.get(path + "/readiness", headers=owner_headers)
        assert readiness.status_code == 200, readiness.text
        await savepoint.rollback()

    rolled_back = await httpx_client.get(path, headers=owner_headers)
    assert rolled_back.status_code == 404
    assert rolled_back.json() == {"detail": "项目不存在"}
