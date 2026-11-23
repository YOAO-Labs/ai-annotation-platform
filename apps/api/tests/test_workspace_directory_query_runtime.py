"""Portable Directory query wire responses over restricted owner credentials."""

import os
import uuid

import httpx
import pytest
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from yoao_workspace.directory_query_contract import (
    DirectoryQueryUnavailable,
    directory_query_request,
)
from yoao_workspace.directory_query_values import (
    decode_directory_query_reply,
    materialize_directory_query_data,
)
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.user import User
from tests.factory import create_membership, create_project, create_user
from tests.workspace_directory_query_helpers import (
    directory_query_dsn,
    execute_directory_query_contract,
)
from tests.workspace_project_query_helpers import (
    project_query_briefs_dsn,
    workspace_query_client,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]


@pytest.mark.parametrize("profiles_configured", [False, True])
async def test_directory_query_contract_isolates_profile_and_grant_failures(
    test_engine, profiles_configured
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    users, projects = [], []
    token = uuid.uuid4().hex

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

        async def read(client, project_id, operation="list_members"):
            request = directory_query_request(operation, owner.id, project_id)
            return await execute_directory_query_contract(client, token, request)

        dsn = directory_query_dsn(test_engine)
        briefs = project_query_briefs_dsn(test_engine) if profiles_configured else None
        async with workspace_query_client(
            token=token,
            directory_query_database_url=dsn,
            project_query_identity_briefs_database_url=briefs,
        ) as client:
            assert (await client.get("/ready")).status_code == 200
            empty_reply = await read(client, empty.id)
            assert materialize_directory_query_data(empty_reply) == []
            another_actor = empty_reply.request.model_copy(
                update={"actor_id": member.id}
            )
            with pytest.raises(DirectoryQueryUnavailable):
                decode_directory_query_reply(another_actor, empty_reply)
            mentions = materialize_directory_query_data(
                await read(client, full.id, "mention_candidates")
            )
            assert [row["user_id"] for row in mentions] == [owner.id, member.id]
            if profiles_configured:
                rows = materialize_directory_query_data(await read(client, full.id))
                assert [row["user_id"] for row in rows] == [member.id]
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
                        materialize_directory_query_data(await read(client, empty.id))
                        == []
                    )
                    assert (
                        materialize_directory_query_data(
                            await read(client, full.id, "mention_candidates")
                        )
                        == mentions
                    )
                    failure = await read(client, full.id)
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
            else:
                failure = await read(client, full.id)
                assert not failure.ok
                assert failure.error.status_code == 503
                assert failure.error.detail == "身份资料服务暂不可用"
            try:
                async with maker.begin() as db:
                    await db.execute(
                        text(
                            "REVOKE EXECUTE ON FUNCTION "
                            "yoao_workspace_directory_query_v1.directory_query_policy() "
                            "FROM yoao_workspace_directory_query_client"
                        )
                    )
                assert (await client.get("/ready")).status_code == 503
                with pytest.raises(httpx.HTTPStatusError) as unavailable:
                    await read(client, empty.id)
                assert unavailable.value.response.status_code == 503
                assert unavailable.value.response.json() == {
                    "detail": "Workspace directory query unavailable"
                }
            finally:
                async with maker.begin() as db:
                    await db.execute(
                        text(
                            "GRANT EXECUTE ON FUNCTION "
                            "yoao_workspace_directory_query_v1.directory_query_policy() "
                            "TO yoao_workspace_directory_query_client"
                        )
                    )
            assert (await client.get("/ready")).status_code == 200
            assert materialize_directory_query_data(await read(client, empty.id)) == []
            if profiles_configured:
                rows = materialize_directory_query_data(await read(client, full.id))
                assert rows[0]["user_id"] == member.id
    finally:
        async with maker.begin() as db:
            if projects:
                await db.execute(
                    delete(ProjectMember).where(ProjectMember.project_id.in_(projects))
                )
                await db.execute(delete(Project).where(Project.id.in_(projects)))
            if users:
                await db.execute(delete(User).where(User.id.in_(users)))
