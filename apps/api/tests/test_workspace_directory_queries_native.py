"""Directory native reads expose fixed functions and preserve stored member facts."""

from datetime import datetime, timezone
import json
import os
import uuid

import asyncpg
import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_workspace.directory_queries import DirectoryQueryManager
from aap_workspace.directory_query_contract import (
    DIRECTORY_QUERY_PROTOCOL,
    DirectoryQueryProjectId,
    DirectoryQueryUserId,
    decode_directory_query_step,
    directory_query_request,
)
from aap_workspace.directory_query_postgres import (
    DIRECTORY_QUERY_FUNCTIONS,
    DIRECTORY_QUERY_SCHEMA,
    DirectoryQueryPostgresData,
    directory_query_step_arguments,
)
from aap_workspace.directory_query_values import (
    PROJECT_MEMBER_COLUMNS,
    PROJECT_MEMBER_MENTION_COLUMNS,
    materialize_directory_query_data,
)
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.user import User
from app.services.workspace_project_query import _ApiProfiles
from tests.factory import create_membership, create_project, create_user
from tests.workspace_project_query_helpers import _dsn

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]


def _directory_dsn(test_engine):
    return _dsn(
        test_engine,
        "AAP_MODULE_DATA_WORKSPACE_DIRECTORY_QUERY_URL",
        "workspace_directory_query_client",
    )


async def _step(connection, request, action, parameters=None):
    value = await connection.fetchval(
        "SELECT aap_workspace_directory_query_v1.directory_query_step("
        "$1::jsonb,$2::text,$3::jsonb)",
        *directory_query_step_arguments(request, action, parameters),
    )
    return decode_directory_query_step(request, action, parameters, value).facts


async def test_directory_login_has_only_two_functions_and_no_data_access(test_engine):
    from aap_owner_data.postgres import verify_function_only_role

    dsn = _directory_dsn(test_engine)
    assert await DirectoryQueryPostgresData(dsn).directory_query_policy() is None
    connection = await asyncpg.connect(dsn)
    try:
        await verify_function_only_role(
            connection,
            schema=DIRECTORY_QUERY_SCHEMA,
            functions=DIRECTORY_QUERY_FUNCTIONS,
        )
        for statement in (
            "SELECT id FROM public.users LIMIT 1",
            "SELECT id FROM public.projects LIMIT 1",
            "SELECT id FROM public.project_members LIMIT 1",
            "CREATE TEMP TABLE forbidden_directory_temp(id integer)",
            "SELECT * FROM aap_identity_v1.workspace_directory_superadmins()",
            "SELECT aap_workspace_project_query_v1.project_query_policy()",
            "SELECT aap_workspace_directory_query_v1._has_exact_keys('{}',ARRAY[]::text[])",
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.execute(statement)
    finally:
        await connection.close()


async def test_native_directory_steps_and_manager_preserve_members_and_inactive_join(
    test_engine,
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    ids = []
    project_id = None
    connection = await asyncpg.connect(_directory_dsn(test_engine))
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
            inactive = await create_user(
                db,
                "employee",
                f"directory-member-{uuid.uuid4().hex}@example.test",
                "Directory Member",
            )
            inactive.is_active = False
            ids = [owner.id, member.id, inactive.id]
            project = await create_project(db, owner_id=owner.id)
            project_id = project.id
            membership = await create_membership(
                db, project_id=project_id, user_id=member.id, role="annotator"
            )
            inactive_membership = await create_membership(
                db, project_id=project_id, user_id=inactive.id, role="annotator"
            )
            membership.assigned_at = datetime(2026, 1, 2, tzinfo=timezone.utc)
            inactive_membership.assigned_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        request = directory_query_request("list_members", member.id, project_id)
        for action in ("get_row", "actor_status", "member_access"):
            assert await _step(connection, request, action)
        rows = json.loads(
            (
                await _step(
                    connection,
                    request,
                    "member_rows",
                    DirectoryQueryProjectId(project_id=project_id),
                )
            ).rows_json
        )
        assert [uuid.UUID(row["user_id"]) for row in rows] == [member.id, inactive.id]
        assert all(set(row) == set(PROJECT_MEMBER_COLUMNS) for row in rows)

        mention = directory_query_request("mention_candidates", member.id, project_id)
        account = await _step(
            connection, mention, "mention_owner", DirectoryQueryUserId(user_id=owner.id)
        )
        assert account.user.id == owner.id and account.user.is_active
        assert (await _step(connection, mention, "mention_superadmins")).accounts == []
        joined = json.loads(
            (
                await _step(
                    connection,
                    mention,
                    "mention_members",
                    DirectoryQueryProjectId(project_id=project_id),
                )
            ).rows_json
        )
        assert all(set(row) == set(PROJECT_MEMBER_MENTION_COLUMNS) for row in joined)
        assert [uuid.UUID(row["user_id"]) for row in joined] == [member.id, inactive.id]
        async with maker() as db:
            manager = DirectoryQueryManager(
                DirectoryQueryPostgresData(_directory_dsn(test_engine)),
                _ApiProfiles(db),
            )
            members = materialize_directory_query_data(await manager.execute(request))
            assert [row["user_id"] for row in members] == [member.id, inactive.id]
            candidates = materialize_directory_query_data(
                await manager.execute(mention)
            )
            assert [row["user_id"] for row in candidates] == [
                owner.id,
                member.id,
                inactive.id,
            ]
            assert [row["kind"] for row in candidates] == ["owner", "member", "member"]
    finally:
        await connection.close()
        async with maker.begin() as db:
            if project_id is not None:
                await db.execute(
                    delete(ProjectMember).where(ProjectMember.project_id == project_id)
                )
                await db.execute(delete(Project).where(Project.id == project_id))
            if ids:
                await db.execute(delete(User).where(User.id.in_(ids)))


async def test_directory_rpc_rejects_unknown_keys_and_wrong_action_scope(test_engine):
    request = directory_query_request("list_members", uuid.uuid4(), uuid.uuid4())
    payload = request.model_dump(mode="json", by_alias=True)
    cases = [
        ({**payload, "extra": True}, "get_row", None),
        ({**payload, "actor_id": None}, "get_row", None),
        ({**payload, "schema": "other"}, "get_row", None),
        (payload, "get_row", {}),
        (payload, "mention_superadmins", None),
        (payload, "member_rows", {"project_id": str(uuid.uuid4())}),
        (payload, "member_rows", {"project_id": str(request.target_id), "extra": True}),
    ]
    connection = await asyncpg.connect(_directory_dsn(test_engine))
    try:
        for command, action, parameters in cases:
            with pytest.raises(asyncpg.InvalidParameterValueError):
                await connection.fetchval(
                    "SELECT aap_workspace_directory_query_v1.directory_query_step("
                    "$1::jsonb,$2::text,$3::jsonb)",
                    json.dumps(command),
                    action,
                    json.dumps(parameters) if parameters is not None else None,
                )
        assert json.loads(
            await connection.fetchval(
                "SELECT aap_workspace_directory_query_v1.directory_query_policy()"
            )
        ) == {"schema": DIRECTORY_QUERY_PROTOCOL}
    finally:
        await connection.close()
