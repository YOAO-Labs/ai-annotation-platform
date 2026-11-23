"""Portable Project query wire responses over restricted owner credentials."""

from __future__ import annotations

import os
import uuid

import httpx
import pytest
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from yoao_workspace_contract.project_query_contract import (
    ProjectQueryUnavailable,
    project_query_request,
)
from yoao_workspace_contract.project_query_values import (
    decode_project_query_reply,
    materialize_project_query_data,
)
from app.db.models.project import Project
from app.db.models.user import User
from tests.factory import create_project, create_user
from tests.workspace_project_query_helpers import (
    execute_project_query_contract,
    project_query_briefs_dsn,
    project_query_dsn,
    workspace_query_client,
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
async def test_project_query_contract_isolates_profile_and_grant_failures(
    test_engine, profiles_configured
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    actor_id = project_id = None
    token = uuid.uuid4().hex
    marker = f"runtime-project-query-{uuid.uuid4().hex}"

    try:
        async with maker.begin() as db:
            actor = await create_user(
                db, "project_admin", f"{marker}@example.test", "Query owner"
            )
            actor_id = actor.id
            project = await create_project(db, owner_id=actor_id, name=marker)
            project_id = project.id

        async def read(client, search):
            request = project_query_request(
                "list_projects", actor_id, "project_admin", filters=_filters(search)
            )
            return await execute_project_query_contract(client, token, request)

        dsn = project_query_dsn(test_engine)
        briefs = project_query_briefs_dsn(test_engine) if profiles_configured else None
        async with workspace_query_client(
            token=token,
            project_query_database_url=dsn,
            project_query_identity_briefs_database_url=briefs,
        ) as client:
            assert (await client.get("/ready")).status_code == 200
            empty = await read(client, marker + "-empty")
            assert materialize_project_query_data(empty) == []
            admitted = empty.request.admitted_actor
            another_actor = empty.request.model_copy(
                update={
                    "admitted_actor": admitted.model_copy(update={"id": uuid.uuid4()})
                }
            )
            with pytest.raises(ProjectQueryUnavailable):
                decode_project_query_reply(another_actor, empty)
            if profiles_configured:
                rows = materialize_project_query_data(await read(client, marker))
                assert [row["id"] for row in rows] == [project_id]
                try:
                    async with maker.begin() as db:
                        await db.execute(
                            text(
                                "REVOKE EXECUTE ON FUNCTION "
                                "yoao_identity_briefs_v1.briefs_policy() "
                                "FROM yoao_identity_briefs_client"
                            )
                        )
                    assert (await client.get("/ready")).status_code == 200
                    assert (
                        materialize_project_query_data(
                            await read(client, marker + "-empty")
                        )
                        == []
                    )
                    failure = await read(client, marker)
                    assert not failure.ok
                    assert failure.error.status_code == 503
                    assert failure.error.detail == "身份资料服务暂不可用"
                finally:
                    async with maker.begin() as db:
                        await db.execute(
                            text(
                                "GRANT EXECUTE ON FUNCTION "
                                "yoao_identity_briefs_v1.briefs_policy() "
                                "TO yoao_identity_briefs_client"
                            )
                        )
                rows = materialize_project_query_data(await read(client, marker))
                assert rows[0]["owner_name"] == "Query owner"
            else:
                failure = await read(client, marker)
                assert not failure.ok
                assert failure.error.status_code == 503
                assert failure.error.detail == "身份资料服务暂不可用"

            for operation, target in (
                ("project_stats", None),
                ("project_readiness", project_id),
                ("get_project_access", project_id),
            ):
                request = project_query_request(
                    operation, actor_id, "project_admin", target_id=target
                )
                reply = await execute_project_query_contract(client, token, request)
                assert materialize_project_query_data(reply)
            try:
                async with maker.begin() as db:
                    await db.execute(
                        text(
                            "REVOKE EXECUTE ON FUNCTION "
                            "yoao_workspace_project_query_v1.project_query_policy() "
                            "FROM yoao_workspace_project_query_client"
                        )
                    )
                assert (await client.get("/ready")).status_code == 503
                with pytest.raises(httpx.HTTPStatusError) as unavailable:
                    await read(client, marker + "-empty")
                assert unavailable.value.response.status_code == 503
                assert unavailable.value.response.json() == {
                    "detail": "Workspace Project query unavailable"
                }
            finally:
                async with maker.begin() as db:
                    await db.execute(
                        text(
                            "GRANT EXECUTE ON FUNCTION "
                            "yoao_workspace_project_query_v1.project_query_policy() "
                            "TO yoao_workspace_project_query_client"
                        )
                    )
            assert (await client.get("/ready")).status_code == 200
            assert (
                materialize_project_query_data(await read(client, marker + "-empty"))
                == []
            )
            if profiles_configured:
                rows = materialize_project_query_data(await read(client, marker))
                assert rows[0]["id"] == project_id
    finally:
        async with maker.begin() as db:
            if project_id is not None:
                await db.execute(delete(Project).where(Project.id == project_id))
            if actor_id is not None:
                await db.execute(delete(User).where(User.id == actor_id))
