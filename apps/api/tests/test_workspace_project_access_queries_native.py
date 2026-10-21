"""Fixed Project access reads use the existing function-only query credential."""

import json
import os
import uuid

import asyncpg
import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_workspace_contract.project_query_contract import project_query_request
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.user import User
from tests.factory import create_membership, create_project, create_user
from tests.test_workspace_project_queries_native import _step
from tests.workspace_project_query_helpers import project_query_dsn

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]


async def test_access_member_facts_bind_actor_and_project_and_preserve_all_columns(
    test_engine,
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    users, projects = [], []
    connection = await asyncpg.connect(project_query_dsn(test_engine))
    try:
        async with maker.begin() as db:
            owner = await create_user(
                db, "project_admin", f"access-{uuid.uuid4().hex}@example.test", "Owner"
            )
            actor = await create_user(
                db, "employee", f"access-{uuid.uuid4().hex}@example.test", "Member"
            )
            other = await create_user(
                db, "employee", f"access-{uuid.uuid4().hex}@example.test", "Other"
            )
            users = [owner.id, actor.id, other.id]
            project = await create_project(db, owner_id=owner.id)
            second = await create_project(db, owner_id=owner.id)
            projects = [project.id, second.id]
            member = await create_membership(
                db,
                project_id=project.id,
                user_id=actor.id,
                role="reviewer",
                assigned_by=owner.id,
            )
            member.version = 41
            member.weekly_target = 17
            await create_membership(
                db, project_id=project.id, user_id=other.id, role="annotator"
            )
            await create_membership(
                db, project_id=second.id, user_id=actor.id, role="viewer"
            )
        request = project_query_request(
            "get_project_access", actor.id, actor.role, target_id=project.id
        )
        for action in ("get_row", "actor_status"):
            assert await _step(
                connection, request.model_dump(mode="json", by_alias=True), action
            )
        facts = await _step(
            connection,
            request.model_dump(mode="json", by_alias=True),
            "access_member_rows",
        )
        rows = json.loads(facts["rows_json"])
        assert len(rows) == 1
        assert set(rows[0]) == {
            "id",
            "project_id",
            "user_id",
            "role",
            "assigned_by",
            "assigned_at",
            "version",
            "updated_at",
            "weekly_target",
        }
        assert rows[0]["id"] == str(member.id)
        assert rows[0]["project_id"] == str(project.id)
        assert rows[0]["user_id"] == str(actor.id)
        assert (rows[0]["role"], rows[0]["version"], rows[0]["weekly_target"]) == (
            "reviewer",
            41,
            17,
        )
        assert rows[0]["assigned_by"] == str(owner.id)
        request = project_query_request(
            "get_project_access", owner.id, owner.role, target_id=project.id
        )
        empty = await _step(
            connection,
            request.model_dump(mode="json", by_alias=True),
            "access_member_rows",
        )
        assert json.loads(empty["rows_json"]) == []
    finally:
        await connection.close()
        async with maker.begin() as db:
            if projects:
                await db.execute(
                    delete(ProjectMember).where(ProjectMember.project_id.in_(projects))
                )
                await db.execute(delete(Project).where(Project.id.in_(projects)))
            if users:
                await db.execute(delete(User).where(User.id.in_(users)))


async def test_native_access_operation_rejects_other_actions_and_nonempty_binds(
    test_engine,
):
    request = project_query_request(
        "get_project_access", uuid.uuid4(), "employee", target_id=uuid.uuid4()
    )
    payload = request.model_dump(mode="json", by_alias=True)
    cases = [
        ({**payload, "extra": True}, "access_member_rows", None, None),
        ({**payload, "target_id": None}, "access_member_rows", None, None),
        ({**payload, "filters_ascii": "{}"}, "access_member_rows", None, None),
        (payload, "member_access", None, None),
        (payload, "list_rows", None, None),
        (payload, "access_member_rows", {}, None),
        (payload, "access_member_rows", None, "active"),
        ({**payload, "operation": "get_project"}, "access_member_rows", None, None),
    ]
    connection = await asyncpg.connect(project_query_dsn(test_engine))
    try:
        for command, action, parameters, status in cases:
            with pytest.raises(asyncpg.InvalidParameterValueError):
                await connection.fetchval(
                    "SELECT aap_workspace_project_query_v1.project_query_step("
                    "$1::jsonb,$2::text,$3::jsonb,$4::text,NULL::text,"
                    "NULL::text[],NULL::text[],NULL::uuid,NULL::text,"
                    "NULL::timestamptz,NULL::timestamptz,NULL::bigint,NULL::integer)",
                    json.dumps(command),
                    action,
                    json.dumps(parameters) if parameters is not None else None,
                    status,
                )
    finally:
        await connection.close()
