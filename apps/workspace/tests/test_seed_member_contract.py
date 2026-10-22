from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from aap_workspace.seed_member_contract import (
    SEED_MEMBER_PROTOCOL,
    SeedMemberInsert,
    SeedMemberWriteRequest,
    SeedMemberWriteUnavailable,
    SeedMemberUpdate,
    decode_seed_member_write_reply,
    decode_seed_member_write_request,
    seed_member_schema,
)


def _insert(*, member_id: uuid.UUID) -> SeedMemberInsert:
    return SeedMemberInsert(
        kind="insert",
        id=member_id,
        project_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        role="",
        assigned_by=uuid.uuid4(),
    )


def _request(operations) -> SeedMemberWriteRequest:
    return SeedMemberWriteRequest.model_validate(
        {"schema": SEED_MEMBER_PROTOCOL, "operations": operations}
    )


def test_seed_member_request_is_closed_ordered_and_keeps_unrestricted_role_text():
    first_id = uuid.uuid4()
    second_id = uuid.uuid4()
    request = decode_seed_member_write_request(
        {
            "schema": SEED_MEMBER_PROTOCOL,
            "operations": [
                {
                    "kind": "insert",
                    "id": str(first_id),
                    "project_id": str(uuid.uuid4()),
                    "user_id": str(uuid.uuid4()),
                    "role": "",
                    "assigned_by": str(uuid.uuid4()),
                },
                {
                    "kind": "update",
                    "id": str(second_id),
                    "role": "custom duty",
                    "assigned_by": str(uuid.uuid4()),
                    "write_role": False,
                    "write_assigned_by": True,
                },
            ],
        }
    )

    assert [operation.id for operation in request.operations] == [
        first_id,
        second_id,
    ]
    assert request.operations[0].role == ""
    assert request.operations[1].write_role is False
    assert request.operations[1].write_assigned_by is True
    assert SeedMemberWriteRequest.model_config["frozen"] is True


@pytest.mark.parametrize(
    "changes",
    [
        {"role": 7},
        {"write_role": 1},
        {"write_role": False, "write_assigned_by": False},
        {"extra": True},
        {"kind": "upsert"},
    ],
)
def test_seed_member_request_rejects_invalid_or_extra_operation_fields(changes):
    operation = {
        "kind": "update",
        "id": str(uuid.uuid4()),
        "role": "reviewer",
        "assigned_by": str(uuid.uuid4()),
        "write_role": True,
        "write_assigned_by": False,
        **changes,
    }
    with pytest.raises((SeedMemberWriteUnavailable, ValidationError)):
        decode_seed_member_write_request(
            {"schema": SEED_MEMBER_PROTOCOL, "operations": [operation]}
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"protocol": SEED_MEMBER_PROTOCOL, "operations": []},
        {"schema": "aap-workspace-seed-members/v2", "operations": []},
        {"schema": SEED_MEMBER_PROTOCOL, "operations": [], "extra": True},
        {"schema": SEED_MEMBER_PROTOCOL, "operations": None},
        b'{"schema":"aap-workspace-seed-members/v1","schema":"duplicate","operations":[]}',
        b'{"schema":"aap-workspace-seed-members/v1","operations":[NaN]}',
        b"{",
    ],
)
def test_seed_member_request_rejects_malformed_protocol_and_json(payload):
    with pytest.raises(SeedMemberWriteUnavailable):
        decode_seed_member_write_request(payload)


def test_seed_member_reply_binds_result_order_kind_id_and_timestamps():
    insert = _insert(member_id=uuid.uuid4())
    update = SeedMemberUpdate(
        kind="update",
        id=uuid.uuid4(),
        role="annotator",
        assigned_by=uuid.uuid4(),
        write_role=True,
        write_assigned_by=True,
    )
    request = _request([insert, update])
    reply = {
        "schema": SEED_MEMBER_PROTOCOL,
        "results": [
            {
                "kind": "insert",
                "id": str(insert.id),
                "assigned_at": "2026-10-23T10:11:12.123456Z",
                "updated_at": "2026-10-23T10:11:12.123456+00:00",
            },
            {
                "kind": "update",
                "id": str(update.id),
                "found": True,
                "updated_at": "2026-10-23T10:11:12.123456Z",
            },
        ],
    }

    decoded = decode_seed_member_write_reply(json.dumps(reply), request)
    assert decoded[0].assigned_at == datetime(
        2026, 10, 23, 10, 11, 12, 123456, tzinfo=UTC
    )
    assert decoded[1].found is True


def test_seed_member_empty_batch_and_missing_update_are_valid_results():
    empty = _request([])
    assert (
        decode_seed_member_write_reply(
            {"schema": SEED_MEMBER_PROTOCOL, "results": []}, empty
        )
        == []
    )

    update = SeedMemberUpdate(
        kind="update",
        id=uuid.uuid4(),
        role="viewer",
        assigned_by=uuid.uuid4(),
        write_role=True,
        write_assigned_by=False,
    )
    reply = decode_seed_member_write_reply(
        {
            "schema": SEED_MEMBER_PROTOCOL,
            "results": [
                {
                    "kind": "update",
                    "id": str(update.id),
                    "found": False,
                    "updated_at": None,
                }
            ],
        },
        request=_request([update]),
    )
    assert reply[0].found is False
    assert reply[0].updated_at is None


@pytest.mark.parametrize(
    "found,updated_at",
    [
        (True, None),
        (False, "2026-10-23T10:11:12Z"),
        (True, "2026-10-23T10:11:12"),
        (True, "infinity"),
    ],
)
def test_seed_member_update_reply_requires_a_finite_aware_timestamp_when_found(
    found, updated_at
):
    update = SeedMemberUpdate(
        kind="update",
        id=uuid.uuid4(),
        role="reviewer",
        assigned_by=uuid.uuid4(),
        write_role=True,
        write_assigned_by=False,
    )
    with pytest.raises(SeedMemberWriteUnavailable):
        decode_seed_member_write_reply(
            {
                "schema": SEED_MEMBER_PROTOCOL,
                "results": [
                    {
                        "kind": "update",
                        "id": str(update.id),
                        "found": found,
                        "updated_at": updated_at,
                    }
                ],
            },
            _request([update]),
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"id": str(uuid.uuid4())},
        {"kind": "update"},
        {"found": 1},
        {"updated_at": None},
        {"updated_at": "2026-10-23T10:11:12"},
        {"updated_at": "infinity"},
        {"extra": True},
    ],
)
def test_seed_member_reply_rejects_malformed_or_scope_mismatched_results(changes):
    insert = _insert(member_id=uuid.uuid4())
    base_result = {
        "kind": "insert",
        "id": str(insert.id),
        "assigned_at": "2026-10-23T10:11:12Z",
        "updated_at": "2026-10-23T10:11:12Z",
        **changes,
    }
    with pytest.raises(SeedMemberWriteUnavailable):
        decode_seed_member_write_reply(
            {"schema": SEED_MEMBER_PROTOCOL, "results": [base_result]},
            _request([insert]),
        )


def test_seed_member_reply_rejects_wrong_count_or_order():
    first = _insert(member_id=uuid.uuid4())
    second = _insert(member_id=uuid.uuid4())
    request = _request([first, second])
    first_result = {
        "kind": "insert",
        "id": str(first.id),
        "assigned_at": "2026-10-23T10:11:12Z",
        "updated_at": "2026-10-23T10:11:12Z",
    }
    second_result = {
        **first_result,
        "id": str(second.id),
    }
    for results in ([first_result], [second_result, first_result]):
        with pytest.raises(SeedMemberWriteUnavailable):
            decode_seed_member_write_reply(
                {"schema": SEED_MEMBER_PROTOCOL, "results": results}, request
            )


def test_seed_member_descriptor_freezes_one_function_and_transaction_rules():
    schema = seed_member_schema()
    descriptor = schema["x-native-capabilities"]
    assert descriptor["protocol"] == SEED_MEMBER_PROTOCOL
    assert descriptor["postgres_schema"] == "aap_workspace_v1"
    assert [function["signature"] for function in descriptor["functions"]] == [
        "aap_workspace_v1.write_seed_members(jsonb)"
    ]
    function = descriptor["functions"][0]
    assert [argument["name"] for argument in function["arguments"]] == ["p_request"]
    assert function["result_postgres_type"] == "jsonb"
    assert function["result_schema_ref"] == "#/$defs/SeedMemberWriteReply"
    assert set(schema["$defs"]["SeedMemberWriteReply"]["required"]) == {
        "schema",
        "results",
    }
    assert "without incrementing version" in function["transaction_semantics"]
    assert (
        "caller owns the active transaction and commit"
        in descriptor["transaction_boundary"]
    )
