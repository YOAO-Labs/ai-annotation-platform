from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
import json
from importlib.resources import files
from types import SimpleNamespace
import uuid

import pytest

from aap_workspace.directory_queries import DirectoryQueryManager
from aap_workspace.directory_query_contract import (
    DIRECTORY_QUERY_PROTOCOL,
    DirectoryQueryInvalidRequest,
    DirectoryQueryUnavailable,
    decode_directory_query_request,
    decode_directory_query_step,
    directory_query_request,
    directory_query_schema,
)
from aap_workspace.directory_query_values import (
    DIRECTORY_MENTION_RESPONSE_COLUMNS,
    DIRECTORY_MEMBER_RESPONSE_COLUMNS,
    PROJECT_MEMBER_COLUMNS,
    PROJECT_MEMBER_MENTION_COLUMNS,
    decode_directory_member_rows,
    materialize_directory_query_data,
)
from aap_workspace_contract.project_query_contract import (
    PROJECT_JSON_COLUMNS,
    PROJECT_ROW_COLUMNS,
)
from aap_workspace.project_queries import ProjectQueryProfilesUnavailable

ACTOR = uuid.UUID(int=1)
PROJECT = uuid.UUID(int=2)
OWNER = uuid.UUID(int=3)
OTHER = uuid.UUID(int=4)


def project_row(*, owner_id=ACTOR):
    value = dict.fromkeys(PROJECT_ROW_COLUMNS)
    value.update({key: "{}" for key in PROJECT_JSON_COLUMNS})
    value.update(
        id=str(PROJECT),
        owner_id=str(owner_id) if owner_id is not None else None,
        display_id="P-DIRECTORY",
        name="Directory",
        type_label="Image",
        type_key="image-det",
        data_type="image",
        status="in_progress",
        total_tasks=0,
        completed_tasks=0,
        review_tasks=0,
        in_progress_tasks=0,
        iou_dedup_threshold="0.7",
        box_threshold="0.35",
        text_threshold="0.25",
        due_date=None,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-02T00:00:00Z",
    )
    return value


def member_row(
    *,
    member_id=uuid.UUID(int=10),
    user_id=uuid.UUID(int=20),
    project_id=PROJECT,
    role="annotator",
    assigned_at="2026-01-10T00:00:00Z",
    updated_at="2026-01-10T00:00:00Z",
    version=1,
    weekly_target=None,
):
    return {
        "id": str(member_id),
        "project_id": str(project_id),
        "user_id": str(user_id),
        "role": role,
        "assigned_by": str(ACTOR),
        "assigned_at": assigned_at,
        "version": version,
        "updated_at": updated_at,
        "weekly_target": weekly_target,
    }


def _member_facts(**changes):
    return json.dumps([member_row(**changes)])


def _authority_facts(*, actor_id=ACTOR, role="project_admin", active=True, row=None):
    value = project_row() if row is None else row
    return {
        "get_row": [
            {"row_json": json.dumps(value)},
            {"row_json": json.dumps(value)},
        ],
        "actor_status": {
            "actor": {
                "actor_id": str(actor_id),
                "platform_role": role,
                "is_active": active,
            }
        },
        "member_access": {"member": None},
    }


def _wire_parameter(parameters):
    return (
        parameters.model_dump(mode="json")
        if hasattr(parameters, "model_dump")
        else parameters
    )


class Data:
    def __init__(self, facts):
        self.facts = facts
        self.events = []

    @asynccontextmanager
    async def transaction(self):
        self.events.append("transaction")
        yield self

    async def step(self, request, action, parameters=None):
        parameters = _wire_parameter(parameters)
        self.events.append((action, parameters))
        facts = self.facts[action]
        if isinstance(facts, list):
            facts = facts.pop(0)
        elif callable(facts):
            facts = facts(parameters)
        return {
            "schema": DIRECTORY_QUERY_PROTOCOL,
            "request": request.model_dump(mode="json", by_alias=True),
            "action": action,
            "parameters": parameters,
            "facts": facts,
        }


class Profiles:
    def __init__(self, events, records=None, *, fail=False):
        self.events = events
        self.records = records or {}
        self.fail = fail
        self.requested = []

    async def resolve(self, user_ids):
        self.requested.append(list(user_ids))
        self.events.append(("profiles", list(user_ids)))
        if self.fail:
            raise ProjectQueryProfilesUnavailable()
        return self.records


def profile(user_id, *, name="Member", role="employee", email=None, avatar_ref=None):
    return SimpleNamespace(
        id=user_id,
        name=name,
        email=email or f"{user_id}@example.test",
        role=role,
        avatar_initial=name[:1].upper(),
        avatar_ref=avatar_ref,
    )


def directory_request(operation="list_members", *, actor_id=ACTOR, target_id=PROJECT):
    return directory_query_request(operation, actor_id, target_id)


def test_closed_request_duplicate_json_and_step_bindings():
    request = directory_request()
    wire = request.model_dump(mode="json", by_alias=True)
    with pytest.raises(DirectoryQueryInvalidRequest):
        decode_directory_query_request(
            json.dumps(wire).replace('"actor_id":', '"extra":true,"actor_id":', 1)
        )
    duplicate = (
        '{"schema":"aap-workspace-directory-query/v1",'
        '"request_id":"00000000-0000-0000-0000-000000000001",'
        '"operation":"list_members",'
        '"actor_id":"00000000-0000-0000-0000-000000000001",'
        '"actor_id":"00000000-0000-0000-0000-000000000002",'
        '"target_id":"00000000-0000-0000-0000-000000000002"}'
    )
    with pytest.raises(DirectoryQueryInvalidRequest):
        decode_directory_query_request(duplicate)

    raw = {
        "schema": DIRECTORY_QUERY_PROTOCOL,
        "request": wire,
        "action": "member_rows",
        "parameters": {"project_id": str(PROJECT)},
        "facts": {"rows_json": "[]"},
    }
    facts = decode_directory_query_step(
        request,
        "member_rows",
        {"project_id": str(PROJECT)},
        raw,
    )
    assert facts.facts.rows_json == "[]"
    for changed in (
        {"parameters": {"project_id": str(OTHER)}},
        {"action": "mention_members"},
        {"request": {**wire, "target_id": str(OTHER)}},
    ):
        with pytest.raises(DirectoryQueryUnavailable):
            decode_directory_query_step(
                request, "member_rows", {"project_id": str(PROJECT)}, {**raw, **changed}
            )


def test_step_facts_are_bound_to_actor_project_and_owner_parameters():
    request = directory_request()
    wire = request.model_dump(mode="json", by_alias=True)
    mismatches = [
        (
            "actor_status",
            None,
            {
                "actor": {
                    "actor_id": str(OTHER),
                    "platform_role": "employee",
                    "is_active": True,
                }
            },
        ),
        (
            "member_access",
            None,
            {
                "member": {
                    "id": str(uuid.UUID(int=8)),
                    "project_id": str(OTHER),
                    "user_id": str(ACTOR),
                    "role": "viewer",
                }
            },
        ),
    ]
    for action, parameters, facts in mismatches:
        with pytest.raises(DirectoryQueryUnavailable):
            decode_directory_query_step(
                request,
                action,
                parameters,
                {
                    "schema": DIRECTORY_QUERY_PROTOCOL,
                    "request": wire,
                    "action": action,
                    "parameters": parameters,
                    "facts": facts,
                },
            )

    mention_request = directory_request("mention_candidates")
    parameters = {"user_id": str(OWNER)}
    with pytest.raises(DirectoryQueryUnavailable):
        decode_directory_query_step(
            mention_request,
            "mention_owner",
            parameters,
            {
                "schema": DIRECTORY_QUERY_PROTOCOL,
                "request": mention_request.model_dump(mode="json", by_alias=True),
                "action": "mention_owner",
                "parameters": parameters,
                "facts": {
                    "user": {
                        "id": str(OTHER),
                        "name": "Wrong owner",
                        "email": "wrong@example.test",
                        "is_active": True,
                    }
                },
            },
        )


def test_published_schema_contains_the_fixed_rows_and_action_matrix():
    schema = directory_query_schema()
    saved = json.loads(
        files("aap_workspace")
        .joinpath("directory_query_protocol.schema.json")
        .read_text()
    )
    assert saved == schema
    assert schema["$defs"]["DirectoryQueryStep"]["additionalProperties"] is False
    assert schema["x-project-member-columns"] == list(PROJECT_MEMBER_COLUMNS)
    assert schema["x-project-member-mention-columns"] == list(
        PROJECT_MEMBER_MENTION_COLUMNS
    )
    assert schema["x-project-member-response-columns"] == list(
        DIRECTORY_MEMBER_RESPONSE_COLUMNS
    )
    assert schema["x-mention-response-columns"] == list(
        DIRECTORY_MENTION_RESPONSE_COLUMNS
    )


@pytest.mark.asyncio
async def test_missing_project_precedes_actor_status_and_keeps_404():
    data = Data(
        {
            "get_row": {"row_json": None},
            "actor_status": {"actor": None},
        }
    )

    reply = await DirectoryQueryManager(data).execute(directory_request())

    assert not reply.ok and reply.error.status_code == 404
    assert reply.error.detail == "项目不存在"
    assert data.events == ["transaction", ("get_row", None)]


@pytest.mark.asyncio
async def test_inactive_actor_preserves_bearer_401_before_project_refresh():
    data = Data(_authority_facts(active=False))

    reply = await DirectoryQueryManager(data).execute(directory_request())

    assert not reply.ok and reply.error.status_code == 401
    assert reply.error.headers == {"WWW-Authenticate": "Bearer"}
    assert [event[0] for event in data.events[1:]] == ["get_row", "actor_status"]


@pytest.mark.asyncio
async def test_employee_visibility_reads_current_membership_before_member_rows():
    facts = _authority_facts(role="employee", row=project_row(owner_id=OWNER))
    facts["member_access"] = {
        "member": {
            "id": str(uuid.UUID(int=8)),
            "project_id": str(PROJECT),
            "user_id": str(ACTOR),
            "role": "viewer",
        }
    }
    facts["member_rows"] = {"rows_json": "[]"}
    data = Data(facts)

    reply = await DirectoryQueryManager(data).execute(directory_request())

    assert reply.ok and materialize_directory_query_data(reply) == []
    assert [event[0] for event in data.events[1:]] == [
        "get_row",
        "actor_status",
        "get_row",
        "member_access",
        "member_rows",
    ]


@pytest.mark.asyncio
async def test_list_members_decodes_all_rows_then_resolves_once_skipping_missing_profiles():
    first, second, missing = (uuid.UUID(int=value) for value in (20, 21, 22))
    raw_members = [
        member_row(
            member_id=uuid.UUID(int=10),
            user_id=first,
            role="historical_member_role",
            assigned_at="2026-01-10T00:00:00Z",
        ),
        member_row(
            member_id=uuid.UUID(int=11),
            user_id=second,
            role="viewer",
            assigned_at="infinity",
            updated_at="-infinity",
        ),
        member_row(
            member_id=uuid.UUID(int=12),
            user_id=missing,
            role="annotator",
            assigned_at="2026-01-08T00:00:00Z",
        ),
    ]
    facts = _authority_facts()
    facts["member_rows"] = {"rows_json": json.dumps(raw_members)}
    data = Data(facts)
    profiles = Profiles(
        data.events,
        {
            str(first): profile(
                first,
                name="First",
                role="historical_staff_role",
                avatar_ref="preset:first",
            ),
            str(second): profile(second, name="Second", role=None),
        },
    )

    reply = await DirectoryQueryManager(data, profiles).execute(directory_request())
    output = materialize_directory_query_data(reply)

    assert reply.ok
    assert [item["user_id"] for item in output] == [first, second]
    assert output[0]["role"] == "historical_member_role"
    assert output[0]["platform_role"] == "historical_staff_role"
    assert output[0]["avatar_ref"] == "preset:first"
    assert output[1]["assigned_at"] == datetime.max
    assert output[1]["updated_at"] == datetime.min
    assert output[1]["updated_at"].tzinfo is None
    assert profiles.requested == [[first, second, missing]]
    assert [event[0] for event in data.events[1:]] == [
        "get_row",
        "actor_status",
        "get_row",
        "member_rows",
        "profiles",
    ]


@pytest.mark.asyncio
async def test_empty_member_list_does_not_construct_profiles_but_failure_maps_after_rows():
    facts = _authority_facts()
    facts["member_rows"] = {"rows_json": "[]"}
    data = Data(facts)

    reply = await DirectoryQueryManager(data).execute(directory_request())

    assert reply.ok and materialize_directory_query_data(reply) == []
    assert data.events[-1] == ("member_rows", {"project_id": str(PROJECT)})

    facts = _authority_facts()
    facts["member_rows"] = {"rows_json": json.dumps([member_row()])}
    data = Data(facts)
    profiles = Profiles(data.events, fail=True)
    reply = await DirectoryQueryManager(data, profiles).execute(directory_request())
    assert not reply.ok and reply.error.status_code == 503
    assert reply.error.detail == "身份资料服务暂不可用"
    assert data.events[-1][0] == "profiles"


@pytest.mark.asyncio
async def test_finite_timestamp_overflow_fails_before_profiles_and_infinity_round_trips():
    facts = _authority_facts()
    facts["member_rows"] = {
        "rows_json": json.dumps([member_row(assigned_at="10000-01-01T00:00:00Z")])
    }
    data = Data(facts)
    profiles = Profiles(
        data.events, {str(uuid.UUID(int=20)): profile(uuid.UUID(int=20))}
    )

    with pytest.raises(OverflowError):
        await DirectoryQueryManager(data, profiles).execute(directory_request())
    assert data.events[-1][0] == "member_rows"
    assert profiles.requested == []

    records = decode_directory_member_rows(
        json.dumps([member_row(assigned_at="infinity", updated_at="-infinity")]),
        target_id=PROJECT,
    )
    assert records[0].assigned_at == datetime.max
    assert records[0].updated_at == datetime.min


@pytest.mark.asyncio
async def test_mention_candidates_keep_owner_superadmin_member_order_and_first_kind():
    inactive_admin, unicode_member, inactive_member, viewer = (
        uuid.UUID(int=value) for value in (30, 31, 32, 33)
    )
    facts = _authority_facts(role="super_admin", row=project_row(owner_id=ACTOR))
    facts["mention_owner"] = {
        "user": {
            "id": str(ACTOR),
            "name": "Project Owner",
            "email": "owner@example.test",
            "is_active": True,
        }
    }
    facts["mention_superadmins"] = {
        "accounts": [
            {
                "id": str(OTHER),
                "name": "Alpha Admin",
                "email": "alpha@example.test",
            },
            {
                "id": str(ACTOR),
                "name": "Project Owner",
                "email": "owner@example.test",
            },
        ]
    }
    facts["mention_members"] = {
        "rows_json": json.dumps(
            [
                {
                    **member_row(
                        member_id=uuid.UUID(int=40),
                        user_id=inactive_admin,
                        role="historical_member_role",
                    ),
                    "user_name": "Inactive System Admin",
                    "user_email": "inactive-admin@example.test",
                },
                {
                    **member_row(
                        member_id=uuid.UUID(int=41),
                        user_id=unicode_member,
                        role="annotator",
                        assigned_at="2026-01-09T00:00:00Z",
                    ),
                    "user_name": "ßeta Annotator",
                    "user_email": "unicode@example.test",
                },
                {
                    **member_row(
                        member_id=uuid.UUID(int=42),
                        user_id=inactive_member,
                        role="annotator",
                        assigned_at="2026-01-08T00:00:00Z",
                    ),
                    "user_name": "Inactive Annotator",
                    "user_email": "inactive@example.test",
                },
                {
                    **member_row(
                        member_id=uuid.UUID(int=43),
                        user_id=viewer,
                        role="viewer",
                        assigned_at="2026-01-07T00:00:00Z",
                    ),
                    "user_name": "Project Viewer",
                    "user_email": "viewer@example.test",
                },
            ]
        )
    }
    data = Data(facts)

    reply = await DirectoryQueryManager(data).execute(
        directory_request("mention_candidates")
    )
    output = materialize_directory_query_data(reply)

    assert reply.ok
    assert [(item["user_id"], item["kind"]) for item in output] == [
        (ACTOR, "owner"),
        (OTHER, "super_admin"),
        (inactive_admin, "member"),
        (unicode_member, "member"),
        (inactive_member, "member"),
        (viewer, "member"),
    ]
    assert output[3]["user_name"] == "ßeta Annotator"
    assert [event[0] for event in data.events[1:]] == [
        "get_row",
        "actor_status",
        "get_row",
        "mention_owner",
        "mention_superadmins",
        "mention_members",
    ]


@pytest.mark.asyncio
async def test_inactive_project_owner_is_omitted_from_mention_candidates():
    facts = _authority_facts(role="super_admin", row=project_row(owner_id=ACTOR))
    facts["mention_owner"] = {
        "user": {
            "id": str(ACTOR),
            "name": "Inactive Owner",
            "email": "inactive-owner@example.test",
            "is_active": False,
        }
    }
    facts["mention_superadmins"] = {"accounts": []}
    facts["mention_members"] = {"rows_json": "[]"}
    data = Data(facts)

    reply = await DirectoryQueryManager(data).execute(
        directory_request("mention_candidates")
    )

    assert reply.ok and materialize_directory_query_data(reply) == []


@pytest.mark.asyncio
async def test_materialized_directory_roles_remain_raw_until_public_dto_validation():
    member = member_row(role="historical_role")
    data = Data(
        {
            **_authority_facts(),
            "member_rows": {"rows_json": json.dumps([member])},
        }
    )
    user_id = uuid.UUID(member["user_id"])

    reply = await DirectoryQueryManager(
        data,
        Profiles(
            data.events,
            {
                str(user_id): profile(
                    user_id,
                    name="Historical",
                    role="historical_staff_role",
                )
            },
        ),
    ).execute(directory_request())
    output = materialize_directory_query_data(reply)
    assert output[0]["role"] == "historical_role"
    assert output[0]["platform_role"] == "historical_staff_role"
