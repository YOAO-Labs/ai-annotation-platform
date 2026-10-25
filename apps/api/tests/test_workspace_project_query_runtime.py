"""Installed Project owners isolate data/profile failures and recover after restart."""

from __future__ import annotations

import os
from types import SimpleNamespace
import uuid

from fastapi import HTTPException
import httpx
import pytest
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.services import workspace_project_query
from app.db.models.project import Project
from app.db.models.user import User
from tests.factory import create_project, create_user
from tests.workspace_project_query_helpers import (
    api_project_query_binding,
    project_query_briefs_dsn,
    project_query_dsn,
    project_query_provider,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]


def _filters(search):
    return {
        "status": None,
        "search": search,
        "type_key": None,
        "data_type": None,
        "member_id": None,
        "project_role": None,
        "created_from": None,
        "created_to": None,
    }


@pytest.mark.parametrize("profiles_configured", [False, True])
async def test_installed_owner_isolates_profile_failure_grants_and_restart(
    test_engine, monkeypatch, profiles_configured
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    actor_id = project_id = None
    token = uuid.uuid4().hex
    marker = f"runtime-project-query-{uuid.uuid4().hex}"

    def forbidden(*args, **kwargs):
        raise AssertionError(
            "An explicitly selected owner must not use a local manager"
        )

    monkeypatch.setattr(workspace_project_query, "ProjectQueryManager", forbidden)
    try:
        async with maker.begin() as db:
            actor = await create_user(
                db, "project_admin", f"{marker}@example.test", "Query owner"
            )
            actor_id = actor.id
            project = await create_project(db, owner_id=actor_id, name=marker)
            project_id = project.id
        actor = SimpleNamespace(id=actor_id, role="project_admin")

        async def read(search):
            return await workspace_project_query.execute_project_query(
                object(),
                operation="list_projects",
                actor=actor,
                filters=_filters(search),
            )

        dsn = project_query_dsn(test_engine)
        briefs = project_query_briefs_dsn(test_engine) if profiles_configured else None
        async with project_query_provider(dsn, token, briefs_dsn=briefs) as origin:
            async with api_project_query_binding(url=origin, token=token):
                async with httpx.AsyncClient(base_url=origin, timeout=5) as client:
                    assert (await client.get("/ready")).status_code == 200
                    assert await read(marker + "-empty") == []
                    if profiles_configured:
                        assert [row["id"] for row in await read(marker)] == [project_id]
                        try:
                            async with maker.begin() as db:
                                await db.execute(
                                    text(
                                        "REVOKE EXECUTE ON FUNCTION "
                                        "aap_identity_briefs_v1.briefs_policy() "
                                        "FROM identity_briefs_client"
                                    )
                                )
                            assert (await client.get("/ready")).status_code == 200
                            assert await read(marker + "-empty") == []
                            with pytest.raises(HTTPException) as profile_failure:
                                await read(marker)
                            assert profile_failure.value.status_code == 503
                            assert (
                                profile_failure.value.detail == "身份资料服务暂不可用"
                            )
                        finally:
                            async with maker.begin() as db:
                                await db.execute(
                                    text(
                                        "GRANT EXECUTE ON FUNCTION "
                                        "aap_identity_briefs_v1.briefs_policy() "
                                        "TO identity_briefs_client"
                                    )
                                )
                        assert (await read(marker))[0]["owner_name"] == "Query owner"
                    else:
                        with pytest.raises(HTTPException) as missing_profiles:
                            await read(marker)
                        assert missing_profiles.value.status_code == 503
                        assert missing_profiles.value.detail == "身份资料服务暂不可用"

                    # Statistics and readiness never acquire owner display profiles.
                    for operation, target in (
                        ("project_stats", None),
                        ("project_readiness", project_id),
                        ("get_project_access", project_id),
                    ):
                        assert await workspace_project_query.execute_project_query(
                            object(),
                            operation=operation,
                            actor=actor,
                            target_id=target,
                        )
                    try:
                        async with maker.begin() as db:
                            await db.execute(
                                text(
                                    "REVOKE EXECUTE ON FUNCTION "
                                    "aap_workspace_project_query_v1.project_query_policy() "
                                    "FROM workspace_project_query_client"
                                )
                            )
                        assert (await client.get("/ready")).status_code == 503
                        with pytest.raises(HTTPException) as data_failure:
                            await read(marker + "-empty")
                        assert data_failure.value.status_code == 503
                        assert data_failure.value.detail == "项目查询服务暂不可用"
                    finally:
                        async with maker.begin() as db:
                            await db.execute(
                                text(
                                    "GRANT EXECUTE ON FUNCTION "
                                    "aap_workspace_project_query_v1.project_query_policy() "
                                    "TO workspace_project_query_client"
                                )
                            )
                    assert (await client.get("/ready")).status_code == 200
        async with api_project_query_binding(url=origin, token=token, timeout=2):
            with pytest.raises(HTTPException) as stopped:
                await read(marker + "-empty")
            assert stopped.value.status_code == 503
        async with project_query_provider(dsn, token, briefs_dsn=briefs) as restarted:
            async with api_project_query_binding(url=restarted, token=token):
                assert await read(marker + "-empty") == []
                if profiles_configured:
                    assert (await read(marker))[0]["id"] == project_id
    finally:
        async with maker.begin() as db:
            if project_id is not None:
                await db.execute(delete(Project).where(Project.id == project_id))
            if actor_id is not None:
                await db.execute(delete(User).where(User.id == actor_id))
