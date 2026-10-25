"""Installed Directory processes fail closed and isolate optional display profiles."""

import os
import uuid

from fastapi import HTTPException
import httpx
import pytest
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.user import User
from app.services import workspace_directory_query
from tests.factory import create_membership, create_project, create_user
from tests.workspace_directory_query_helpers import (
    api_directory_query_binding,
    directory_query_dsn,
    directory_query_provider,
)
from tests.workspace_project_query_helpers import project_query_briefs_dsn

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]


@pytest.mark.parametrize("profiles_configured", [False, True])
async def test_directory_owner_isolates_profiles_data_grants_and_process_restart(
    test_engine, monkeypatch, profiles_configured
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    users, projects = [], []
    token = uuid.uuid4().hex

    def forbidden(*args, **kwargs):
        raise AssertionError(
            "A selected Directory owner must not invoke a local manager"
        )

    monkeypatch.setattr(workspace_directory_query, "DirectoryQueryManager", forbidden)
    try:
        async with maker.begin() as db:
            owner = await create_user(
                db,
                "project_admin",
                f"directory-owner-{uuid.uuid4().hex}@example.test",
                "Directory Owner",
            )
            member = await create_user(
                db,
                "employee",
                f"directory-member-{uuid.uuid4().hex}@example.test",
                "Directory Member",
            )
            users = [owner.id, member.id]
            empty = await create_project(db, owner_id=owner.id)
            full = await create_project(db, owner_id=owner.id)
            projects = [empty.id, full.id]
            await create_membership(
                db, project_id=full.id, user_id=member.id, role="annotator"
            )

        async def read(project_id, operation="list_members"):
            return await workspace_directory_query.execute_directory_query(
                object(), operation=operation, actor_id=owner.id, target_id=project_id
            )

        dsn = directory_query_dsn(test_engine)
        briefs = project_query_briefs_dsn(test_engine) if profiles_configured else None
        async with directory_query_provider(dsn, token, briefs_dsn=briefs) as origin:
            async with api_directory_query_binding(url=origin, token=token):
                async with httpx.AsyncClient(base_url=origin, timeout=5) as client:
                    assert (await client.get("/ready")).status_code == 200
                    assert await read(empty.id) == []
                    assert [
                        row["user_id"]
                        for row in await read(full.id, "mention_candidates")
                    ] == [owner.id, member.id]
                    if profiles_configured:
                        assert [row["user_id"] for row in await read(full.id)] == [
                            member.id
                        ]
                        try:
                            async with maker.begin() as db:
                                await db.execute(
                                    text(
                                        "REVOKE EXECUTE ON FUNCTION aap_identity_briefs_v1.briefs_policy() FROM identity_briefs_client"
                                    )
                                )
                            assert (await client.get("/ready")).status_code == 200
                            assert await read(empty.id) == []
                            assert await read(full.id, "mention_candidates")
                            with pytest.raises(HTTPException) as unavailable_profiles:
                                await read(full.id)
                            assert unavailable_profiles.value.status_code == 503
                            assert (
                                unavailable_profiles.value.detail
                                == "身份资料服务暂不可用"
                            )
                        finally:
                            async with maker.begin() as db:
                                await db.execute(
                                    text(
                                        "GRANT EXECUTE ON FUNCTION aap_identity_briefs_v1.briefs_policy() TO identity_briefs_client"
                                    )
                                )
                    else:
                        with pytest.raises(HTTPException) as missing_profiles:
                            await read(full.id)
                        assert missing_profiles.value.status_code == 503
                        assert missing_profiles.value.detail == "身份资料服务暂不可用"
                    try:
                        async with maker.begin() as db:
                            await db.execute(
                                text(
                                    "REVOKE EXECUTE ON FUNCTION aap_workspace_directory_query_v1.directory_query_policy() FROM workspace_directory_query_client"
                                )
                            )
                        assert (await client.get("/ready")).status_code == 503
                        with pytest.raises(HTTPException) as unavailable_data:
                            await read(empty.id)
                        assert unavailable_data.value.status_code == 503
                        assert unavailable_data.value.detail == "项目目录服务暂不可用"
                    finally:
                        async with maker.begin() as db:
                            await db.execute(
                                text(
                                    "GRANT EXECUTE ON FUNCTION aap_workspace_directory_query_v1.directory_query_policy() TO workspace_directory_query_client"
                                )
                            )
                    assert (await client.get("/ready")).status_code == 200
        async with api_directory_query_binding(url=origin, token=token, timeout=2):
            with pytest.raises(HTTPException) as stopped:
                await read(empty.id)
            assert stopped.value.status_code == 503
        async with directory_query_provider(dsn, token, briefs_dsn=briefs) as restarted:
            async with api_directory_query_binding(url=restarted, token=token):
                assert await read(empty.id) == []
                if profiles_configured:
                    assert (await read(full.id))[0]["user_id"] == member.id
    finally:
        async with maker.begin() as db:
            if projects:
                await db.execute(
                    delete(ProjectMember).where(ProjectMember.project_id.in_(projects))
                )
                await db.execute(delete(Project).where(Project.id.in_(projects)))
            if users:
                await db.execute(delete(User).where(User.id.in_(users)))
