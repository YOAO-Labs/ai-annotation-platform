from __future__ import annotations

from contextlib import asynccontextmanager
import json
from importlib.resources import files
import uuid

import pytest

from aap_workspace_contract.access import (
    ACCESS_KIND_MEMBER,
    ACCESS_KIND_OWNER,
    ACCESS_KIND_SUPER_ADMIN,
    MANAGER_CAPABILITIES,
    ProjectAccessPolicyFailure,
    project_manager_access,
    project_member_access,
    project_role_capabilities,
)
from aap_workspace.project_queries import ProjectQueryManager
from aap_workspace_contract.project_query_contract import (
    PROJECT_QUERY_PROTOCOL,
    PROJECT_ROW_COLUMNS,
    ProjectQueryInvalidRequest,
    ProjectQueryUnavailable,
    decode_project_query_step,
    decode_project_query_reply,
    project_query_request,
    project_query_schema,
)
from aap_workspace_contract.project_query_values import materialize_project_query_data

ACTOR = uuid.UUID(int=1)
PROJECT = uuid.UUID(int=2)
OWNER = uuid.UUID(int=3)
MEMBERSHIP_1 = uuid.UUID(int=4)
MEMBERSHIP_2 = uuid.UUID(int=5)


def project_row(**changes):
    value = dict.fromkeys(PROJECT_ROW_COLUMNS)
    value.update(
        id=str(PROJECT),
        owner_id=str(OWNER),
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-02T00:00:00+00:00",
        due_date=None,
    )
    return {**value, **changes}


def member_row(member_id, *, role="reviewer", version=2, **changes):
    return {
        "id": str(member_id),
        "project_id": str(PROJECT),
        "user_id": str(ACTOR),
        "role": role,
        "assigned_by": None,
        "assigned_at": "2026-01-03T00:00:00+00:00",
        "version": version,
        "updated_at": "2026-01-04T00:00:00+00:00",
        "weekly_target": None,
        **changes,
    }


def request(role="employee", *, operation="get_project_access"):
    return project_query_request(operation, ACTOR, role, target_id=PROJECT)


def actor_facts(role="employee"):
    return {
        "actor": {
            "actor_id": str(ACTOR),
            "platform_role": role,
            "is_active": True,
        }
    }


class Data:
    def __init__(self, facts):
        self.facts = facts
        self.events = []

    @asynccontextmanager
    async def transaction(self):
        self.events.append("transaction")
        yield self

    async def step(self, request, action, parameters=None, filters=None):
        self.events.append((action, parameters))
        value = self.facts[action]
        if isinstance(value, list):
            value = value.pop(0)
        return {
            "schema": PROJECT_QUERY_PROTOCOL,
            "request": request.model_dump(mode="json", by_alias=True),
            "action": action,
            "parameters": parameters,
            "facts": value,
        }


def rows_fact(*rows):
    return {"rows_json": json.dumps(list(rows))}


def access_facts(*, role="employee", member_steps=((), ())):
    return {
        "get_row": [
            {"row_json": json.dumps(project_row())},
            {"row_json": json.dumps(project_row())},
            {"row_json": json.dumps(project_row())},
        ],
        "actor_status": [
            actor_facts(role),
            actor_facts(role),
        ],
        "access_member_rows": [
            rows_fact(*member_steps[0]),
            rows_fact(*member_steps[1]),
        ],
    }


def test_manager_and_member_policy_preserve_ownership_and_failure_order():
    admin = project_manager_access(
        actor_id=ACTOR,
        platform_role="project_admin",
        project_id=PROJECT,
        owner_id=ACTOR,
    )
    assert admin is not None and admin.access_kind == ACCESS_KIND_OWNER
    assert admin.is_owner and admin.is_manager and not admin.is_super_admin

    superadmin = project_manager_access(
        actor_id=ACTOR,
        platform_role="super_admin",
        project_id=PROJECT,
        owner_id=OWNER,
    )
    assert superadmin is not None and superadmin.access_kind == ACCESS_KIND_SUPER_ADMIN
    assert superadmin.is_manager and not superadmin.is_owner
    assert superadmin.capabilities == MANAGER_CAPABILITIES

    assert (
        project_manager_access(
            actor_id=ACTOR,
            platform_role="employee",
            project_id=PROJECT,
            owner_id=ACTOR,
        )
        is None
    )
    with pytest.raises(ProjectAccessPolicyFailure) as invalid_platform:
        project_manager_access(
            actor_id=ACTOR,
            platform_role="legacy",
            project_id=PROJECT,
            owner_id=ACTOR,
        )
    assert invalid_platform.value.status_code == 403

    with pytest.raises(ProjectAccessPolicyFailure) as absent:
        project_member_access(
            actor_id=ACTOR,
            platform_role="employee",
            project_id=PROJECT,
            membership_id=None,
            project_role="legacy",
        )
    assert absent.value.status_code == 404
    with pytest.raises(ProjectAccessPolicyFailure) as bad_role:
        project_member_access(
            actor_id=ACTOR,
            platform_role="employee",
            project_id=PROJECT,
            membership_id=MEMBERSHIP_1,
            project_role="legacy",
        )
    assert bad_role.value.status_code == 403
    with pytest.raises(ProjectAccessPolicyFailure) as incompatible:
        project_member_access(
            actor_id=ACTOR,
            platform_role="viewer",
            project_id=PROJECT,
            membership_id=MEMBERSHIP_1,
            project_role="annotator",
        )
    assert incompatible.value.status_code == 403

    anomalous_owner = project_member_access(
        actor_id=ACTOR,
        platform_role="employee",
        project_id=PROJECT,
        membership_id=MEMBERSHIP_1,
        project_role="viewer",
        membership_version=9,
    )
    assert anomalous_owner.access_kind == ACCESS_KIND_MEMBER
    assert not anomalous_owner.is_owner and not anomalous_owner.is_super_admin
    nonowner_admin = project_member_access(
        actor_id=ACTOR,
        platform_role="project_admin",
        project_id=PROJECT,
        membership_id=MEMBERSHIP_2,
        project_role="reviewer",
    )
    assert nonowner_admin.access_kind == ACCESS_KIND_MEMBER
    assert not nonowner_admin.is_manager


@pytest.mark.asyncio
async def test_access_operation_keeps_visibility_then_second_current_read_sequence():
    first = member_row(MEMBERSHIP_1, role="reviewer", version=4)
    final = member_row(MEMBERSHIP_2, role="annotator", version=11)
    data = Data(access_facts(member_steps=((first,), (final,))))

    reply = await ProjectQueryManager(data, object()).execute(request())
    assert reply.ok
    assert data.events == [
        "transaction",
        ("get_row", None),
        ("actor_status", None),
        ("get_row", None),
        ("access_member_rows", None),
        ("actor_status", None),
        ("get_row", None),
        ("access_member_rows", None),
    ]
    result = materialize_project_query_data(reply)
    assert result == {
        "project_id": PROJECT,
        "user_id": ACTOR,
        "platform_role": "employee",
        "project_role": "annotator",
        "membership_id": MEMBERSHIP_2,
        "membership_version": 11,
        "access_kind": ACCESS_KIND_MEMBER,
        "is_manager": False,
        "capabilities": sorted(
            {
                "project.read",
                "member.read",
                "task.read",
                "annotation.write",
            }
        ),
    }


@pytest.mark.asyncio
async def test_access_manager_skips_full_membership_action_in_both_phases():
    data = Data(
        {
            "get_row": [
                {"row_json": json.dumps(project_row())},
                {"row_json": json.dumps(project_row())},
                {"row_json": json.dumps(project_row())},
            ],
            "actor_status": [actor_facts("super_admin"), actor_facts("super_admin")],
        }
    )
    reply = await ProjectQueryManager(data, object()).execute(request("super_admin"))
    assert reply.ok
    assert [event[0] for event in data.events[1:]] == [
        "get_row",
        "actor_status",
        "get_row",
        "actor_status",
        "get_row",
    ]
    result = materialize_project_query_data(reply)
    assert result["access_kind"] == ACCESS_KIND_SUPER_ADMIN
    assert result["membership_id"] is None and result["membership_version"] is None
    assert result["capabilities"] == sorted(MANAGER_CAPABILITIES)


@pytest.mark.asyncio
async def test_missing_membership_stops_after_first_visibility_phase():
    data = Data(access_facts(member_steps=((), ())))
    reply = await ProjectQueryManager(data, object()).execute(request())
    assert not reply.ok
    assert reply.error.status_code == 404 and reply.error.detail == "项目不存在"
    assert [event[0] for event in data.events[1:]] == [
        "get_row",
        "actor_status",
        "get_row",
        "access_member_rows",
    ]


@pytest.mark.asyncio
async def test_full_member_temporal_decode_precedes_policy_and_second_phase():
    out_of_range = member_row(MEMBERSHIP_1, assigned_at="10000-01-01T00:00:00+00:00")
    data = Data(access_facts(member_steps=((out_of_range,), ())))
    with pytest.raises(OverflowError):
        await ProjectQueryManager(data, object()).execute(request())
    assert [event[0] for event in data.events[1:]] == [
        "get_row",
        "actor_status",
        "get_row",
        "access_member_rows",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "members",
    [
        (member_row(MEMBERSHIP_1, user_id=str(OWNER)),),
        (member_row(MEMBERSHIP_1, project_id=str(OWNER)),),
        (
            member_row(MEMBERSHIP_1),
            member_row(uuid.UUID(int=6)),
        ),
    ],
)
async def test_access_member_rows_bind_target_actor_and_single_membership(members):
    data = Data(access_facts(member_steps=(members, ())))
    with pytest.raises(ProjectQueryUnavailable):
        await ProjectQueryManager(data, object()).execute(request())
    assert data.events[-1] == ("access_member_rows", None)


def test_protocol_limits_access_member_action_to_new_operation():
    access_request = request()
    payload = {
        "schema": PROJECT_QUERY_PROTOCOL,
        "request": access_request.model_dump(mode="json", by_alias=True),
        "action": "member_access",
        "parameters": None,
        "facts": {"member": None},
    }
    with pytest.raises(ProjectQueryUnavailable):
        decode_project_query_step(access_request, "member_access", None, payload)

    get_request = project_query_request(
        "get_project", ACTOR, "employee", target_id=PROJECT
    )
    payload.update(
        {
            "request": get_request.model_dump(mode="json", by_alias=True),
            "action": "access_member_rows",
            "facts": {"rows_json": "[]"},
        }
    )
    with pytest.raises(ProjectQueryUnavailable):
        decode_project_query_step(get_request, "access_member_rows", None, payload)


def test_manager_request_requires_only_a_target():
    with pytest.raises(ProjectQueryInvalidRequest):
        project_query_request("require_project_manager", ACTOR, "employee")
    with pytest.raises(ProjectQueryInvalidRequest):
        project_query_request(
            "require_project_manager",
            ACTOR,
            "employee",
            target_id=PROJECT,
            filters={},
        )

    visible = project_query_request(
        "require_project_visible", ACTOR, "employee", target_id=PROJECT
    )
    assert visible.operation == "require_project_visible"
    with pytest.raises(ProjectQueryInvalidRequest):
        project_query_request("require_project_visible", ACTOR, "employee")
    with pytest.raises(ProjectQueryInvalidRequest):
        project_query_request(
            "require_project_visible",
            ACTOR,
            "employee",
            target_id=PROJECT,
            filters={},
        )


@pytest.mark.asyncio
async def test_require_manager_returns_only_project_id_without_a_second_access_phase():
    data = Data(
        {
            "get_row": [
                {"row_json": json.dumps(project_row(owner_id=str(ACTOR)))},
                {"row_json": json.dumps(project_row(owner_id=str(ACTOR)))},
            ],
            "actor_status": [
                actor_facts("project_admin"),
            ],
        }
    )
    reply = await ProjectQueryManager(data, object()).execute(
        request("project_admin", operation="require_project_manager")
    )
    assert reply.ok and json.loads(reply.data_json) == {"project_id": str(PROJECT)}
    assert materialize_project_query_data(reply) == {"project_id": PROJECT}
    assert data.events == [
        "transaction",
        ("get_row", None),
        ("actor_status", None),
        ("get_row", None),
    ]


@pytest.mark.asyncio
async def test_require_visible_allows_member_after_one_full_access_phase():
    member = member_row(MEMBERSHIP_1, role="reviewer", version=9)
    data = Data(access_facts(member_steps=((member,), ())))

    reply = await ProjectQueryManager(data, object()).execute(
        request(operation="require_project_visible")
    )

    assert reply.ok and json.loads(reply.data_json) == {"project_id": str(PROJECT)}
    assert materialize_project_query_data(reply) == {"project_id": PROJECT}
    assert data.events == [
        "transaction",
        ("get_row", None),
        ("actor_status", None),
        ("get_row", None),
        ("access_member_rows", None),
    ]


@pytest.mark.asyncio
async def test_require_visible_manager_skips_membership_and_preserves_failure_phases():
    manager = Data(
        {
            "get_row": [
                {"row_json": json.dumps(project_row(owner_id=str(ACTOR)))},
                {"row_json": json.dumps(project_row(owner_id=str(ACTOR)))},
            ],
            "actor_status": [actor_facts("project_admin")],
        }
    )
    reply = await ProjectQueryManager(manager, object()).execute(
        request("project_admin", operation="require_project_visible")
    )
    assert reply.ok and json.loads(reply.data_json) == {"project_id": str(PROJECT)}
    assert manager.events == [
        "transaction",
        ("get_row", None),
        ("actor_status", None),
        ("get_row", None),
    ]

    disabled = Data(
        {
            "get_row": [{"row_json": json.dumps(project_row())}],
            "actor_status": [{"actor": None}],
        }
    )
    reply = await ProjectQueryManager(disabled, object()).execute(
        request(operation="require_project_visible")
    )
    assert not reply.ok and reply.error.status_code == 401
    assert [event[0] for event in disabled.events[1:]] == [
        "get_row",
        "actor_status",
    ]

    invalid_actor = Data(
        {
            "get_row": [
                {"row_json": json.dumps(project_row())},
                {"row_json": json.dumps(project_row())},
            ],
            "actor_status": [actor_facts("legacy")],
        }
    )
    reply = await ProjectQueryManager(invalid_actor, object()).execute(
        request(operation="require_project_visible")
    )
    assert not reply.ok and reply.error.status_code == 403
    assert reply.error.detail == "账号角色无效，访问被拒绝"
    assert [event[0] for event in invalid_actor.events[1:]] == [
        "get_row",
        "actor_status",
        "get_row",
    ]


@pytest.mark.asyncio
async def test_require_visible_keeps_hidden_and_full_member_decode_errors():
    missing_project = Data({"get_row": [{"row_json": None}]})
    reply = await ProjectQueryManager(missing_project, object()).execute(
        request(operation="require_project_visible")
    )
    assert not reply.ok and reply.error.status_code == 404
    assert [event[0] for event in missing_project.events[1:]] == ["get_row"]

    hidden_member = Data(access_facts(member_steps=((), ())))
    reply = await ProjectQueryManager(hidden_member, object()).execute(
        request(operation="require_project_visible")
    )
    assert not reply.ok and reply.error.status_code == 404
    assert [event[0] for event in hidden_member.events[1:]] == [
        "get_row",
        "actor_status",
        "get_row",
        "access_member_rows",
    ]

    invalid_member = Data(
        access_facts(member_steps=((member_row(MEMBERSHIP_1, role="legacy"),), ()))
    )
    reply = await ProjectQueryManager(invalid_member, object()).execute(
        request(operation="require_project_visible")
    )
    assert not reply.ok and reply.error.status_code == 403
    assert reply.error.detail == "项目职责无效，访问被拒绝"
    assert [event[0] for event in invalid_member.events[1:]] == [
        "get_row",
        "actor_status",
        "get_row",
        "access_member_rows",
    ]

    out_of_range = member_row(MEMBERSHIP_1, assigned_at="10000-01-01T00:00:00+00:00")
    data = Data(access_facts(member_steps=((out_of_range,), ())))
    with pytest.raises(OverflowError):
        await ProjectQueryManager(data, object()).execute(
            request(operation="require_project_visible")
        )
    assert [event[0] for event in data.events[1:]] == [
        "get_row",
        "actor_status",
        "get_row",
        "access_member_rows",
    ]


@pytest.mark.asyncio
async def test_require_manager_decodes_visible_member_then_returns_legacy_403():
    member = member_row(MEMBERSHIP_1, role="reviewer", version=9)
    data = Data(access_facts(member_steps=((member,), ())))
    reply = await ProjectQueryManager(data, object()).execute(
        request(operation="require_project_manager")
    )
    assert not reply.ok
    assert reply.error.status_code == 403
    assert reply.error.detail == "仅项目负责人或超级管理员可执行"
    assert data.events == [
        "transaction",
        ("get_row", None),
        ("actor_status", None),
        ("get_row", None),
        ("access_member_rows", None),
    ]


@pytest.mark.asyncio
async def test_require_manager_preserves_hidden_and_full_row_temporal_errors():
    missing = Data(access_facts(member_steps=((), ())))
    reply = await ProjectQueryManager(missing, object()).execute(
        request(operation="require_project_manager")
    )
    assert not reply.ok and reply.error.status_code == 404
    assert reply.error.detail == "项目不存在"

    overflow = member_row(MEMBERSHIP_1, assigned_at="10000-01-01T00:00:00+00:00")
    data = Data(access_facts(member_steps=((overflow,), ())))
    with pytest.raises(OverflowError):
        await ProjectQueryManager(data, object()).execute(
            request(operation="require_project_manager")
        )
    assert data.events[-1] == ("access_member_rows", None)


def test_require_manager_reply_has_exact_target_bound_shape():
    command = request(operation="require_project_manager")
    for data in (
        {},
        {"project_id": str(OWNER)},
        {"project_id": str(PROJECT), "is_manager": True},
    ):
        with pytest.raises(ProjectQueryUnavailable):
            decode_project_query_reply(
                command,
                {
                    "schema": PROJECT_QUERY_PROTOCOL,
                    "ok": True,
                    "request": command.model_dump(mode="json", by_alias=True),
                    "data_json": json.dumps(data),
                },
            )


def test_require_visible_reply_has_exact_target_bound_shape():
    command = request(operation="require_project_visible")
    for data in (
        {},
        {"project_id": str(OWNER)},
        {"project_id": str(PROJECT), "is_visible": True},
    ):
        with pytest.raises(ProjectQueryUnavailable):
            decode_project_query_reply(
                command,
                {
                    "schema": PROJECT_QUERY_PROTOCOL,
                    "ok": True,
                    "request": command.model_dump(mode="json", by_alias=True),
                    "data_json": json.dumps(data),
                },
            )


def test_project_access_protocol_schema_matches_published_file():
    schema = project_query_schema()
    published = json.loads(
        files("aap_workspace_contract")
        .joinpath("project_query_protocol.schema.json")
        .read_text()
    )
    assert published == schema
    operation_rule = next(
        condition
        for condition in schema["$defs"]["ProjectQueryStep"]["allOf"]
        if condition["if"]["properties"]
        .get("request", {})
        .get("properties", {})
        .get("operation")
        == {"const": "get_project_access"}
    )
    assert operation_rule["then"]["properties"]["action"]["enum"] == [
        "access_member_rows",
        "actor_status",
        "get_row",
    ]
    manager_rule = next(
        condition
        for condition in schema["$defs"]["ProjectQueryStep"]["allOf"]
        if condition["if"]["properties"]
        .get("request", {})
        .get("properties", {})
        .get("operation")
        == {"const": "require_project_manager"}
    )
    assert manager_rule["then"]["properties"]["action"]["enum"] == [
        "access_member_rows",
        "actor_status",
        "get_row",
    ]
    visible_rule = next(
        condition
        for condition in schema["$defs"]["ProjectQueryStep"]["allOf"]
        if condition["if"]["properties"]
        .get("request", {})
        .get("properties", {})
        .get("operation")
        == {"const": "require_project_visible"}
    )
    assert visible_rule["then"]["properties"]["action"]["enum"] == [
        "access_member_rows",
        "actor_status",
        "get_row",
    ]


@pytest.mark.parametrize(
    "platform_role,kind,project_role",
    [
        ("super_admin", "owner", None),
        ("super_admin", "member", "viewer"),
        ("viewer", "member", "annotator"),
    ],
)
def test_access_reply_rejects_inconsistent_platform_role_and_access_kind(
    platform_role, kind, project_role
):
    command = request()
    member = kind == "member"
    data = {
        "project_id": str(PROJECT),
        "user_id": str(ACTOR),
        "platform_role": platform_role,
        "project_role": project_role,
        "membership_id": str(MEMBERSHIP_1) if member else None,
        "membership_version": 1 if member else None,
        "access_kind": kind,
        "is_manager": not member,
        "capabilities": sorted(project_role_capabilities(project_role))
        if member
        else sorted(MANAGER_CAPABILITIES),
    }
    with pytest.raises(ProjectQueryUnavailable):
        decode_project_query_reply(
            command,
            {
                "schema": PROJECT_QUERY_PROTOCOL,
                "ok": True,
                "request": command.model_dump(mode="json", by_alias=True),
                "data_json": json.dumps(data),
            },
        )
