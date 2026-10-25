from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
import uuid

import pytest

from aap_workspace.seed_member_assignment_contract import (
    SEED_MEMBER_ASSIGNMENT_PROTOCOL,
    SeedMemberAssignmentReceipt,
    SeedMemberAssignmentUnavailable,
    decode_seed_member_assignment_reply,
    seed_member_assignment_schema,
)


def _receipt(*, member_id: uuid.UUID, found: bool, assigned_at, updated_at):
    return {
        "schema": SEED_MEMBER_ASSIGNMENT_PROTOCOL,
        "member_id": str(member_id),
        "found": found,
        "assigned_at": assigned_at,
        "updated_at": updated_at,
    }


def test_assignment_receipt_binds_member_and_requested_instant():
    member_id = uuid.uuid4()
    requested_at = datetime(2026, 10, 23, 10, 11, 12, tzinfo=UTC)
    reply = decode_seed_member_assignment_reply(
        _receipt(
            member_id=member_id,
            found=True,
            assigned_at="2026-10-23T12:11:12+02:00",
            updated_at="2026-10-23T10:11:13Z",
        ),
        member_id=member_id,
        assigned_at=requested_at,
    )

    assert isinstance(reply, SeedMemberAssignmentReceipt)
    assert reply.member_id == member_id
    assert reply.assigned_at == requested_at
    assert reply.updated_at == datetime(2026, 10, 23, 10, 11, 13, tzinfo=UTC)
    assert SeedMemberAssignmentReceipt.model_config["frozen"] is True


def test_missing_member_receipt_requires_null_timestamps():
    member_id = uuid.uuid4()
    requested_at = datetime(2026, 10, 23, 10, 11, 12, tzinfo=UTC)
    reply = decode_seed_member_assignment_reply(
        _receipt(
            member_id=member_id,
            found=False,
            assigned_at=None,
            updated_at=None,
        ),
        member_id=member_id,
        assigned_at=requested_at,
    )

    assert reply.found is False
    assert reply.assigned_at is None
    assert reply.updated_at is None


@pytest.mark.parametrize(
    "changes",
    [
        {"member_id": str(uuid.uuid4())},
        {"schema": "aap-workspace-seed-member-assignment/v2"},
        {"found": 1},
        {"assigned_at": "2026-10-23T10:11:12"},
        {"updated_at": "2026-10-23T10:11:12"},
        {"assigned_at": None},
        {"updated_at": None},
        {"extra": True},
    ],
)
def test_assignment_reply_rejects_bad_scope_or_timestamp_shape(changes):
    member_id = uuid.uuid4()
    payload = _receipt(
        member_id=member_id,
        found=True,
        assigned_at="2026-10-23T10:11:12Z",
        updated_at="2026-10-23T10:11:13Z",
    )
    payload.update(changes)

    with pytest.raises(SeedMemberAssignmentUnavailable):
        decode_seed_member_assignment_reply(
            payload,
            member_id=member_id,
            assigned_at=datetime(2026, 10, 23, 10, 11, 12, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    "payload",
    [
        b"{",
        (
            b'{"schema":"aap-workspace-seed-member-assignment/v1",'
            b'"schema":"duplicate","member_id":"'
            + str(uuid.uuid4()).encode()
            + b'","found":false,"assigned_at":null,"updated_at":null}'
        ),
        (
            b'{"schema":"aap-workspace-seed-member-assignment/v1",'
            b'"member_id":"'
            + str(uuid.uuid4()).encode()
            + b'","found":false,"assigned_at":null,"updated_at":NaN}'
        ),
    ],
)
def test_assignment_reply_rejects_malformed_json(payload):
    with pytest.raises(SeedMemberAssignmentUnavailable):
        decode_seed_member_assignment_reply(
            payload,
            member_id=uuid.uuid4(),
            assigned_at=datetime(2026, 10, 23, 10, 11, 12, tzinfo=UTC),
        )


def test_assignment_decoder_requires_an_aware_requested_timestamp():
    member_id = uuid.uuid4()
    with pytest.raises(SeedMemberAssignmentUnavailable):
        decode_seed_member_assignment_reply(
            _receipt(
                member_id=member_id,
                found=False,
                assigned_at=None,
                updated_at=None,
            ),
            member_id=member_id,
            assigned_at=datetime(2026, 10, 23, 10, 11, 12),
        )


def test_assignment_schema_freezes_receipt_native_signature_and_behavior():
    schema = seed_member_assignment_schema()
    descriptor = schema["x-native-capabilities"]
    function = descriptor["functions"][0]

    assert schema["$id"] == SEED_MEMBER_ASSIGNMENT_PROTOCOL
    assert descriptor["protocol"] == SEED_MEMBER_ASSIGNMENT_PROTOCOL
    assert descriptor["postgres_schema"] == "aap_workspace_v1"
    assert function["signature"] == (
        "aap_workspace_v1.pin_seed_member_assignment(uuid, timestamptz)"
    )
    assert [argument["name"] for argument in function["arguments"]] == [
        "p_member_id",
        "p_assigned_at",
    ]
    assert function["result_postgres_type"] == "jsonb"
    assert function["result_schema_ref"] == "#/$defs/SeedMemberAssignmentReceipt"
    assert "always set assigned_at" in function["transaction_semantics"]
    assert "stale cached value" in function["transaction_semantics"]
    assert schema["additionalProperties"] is False


def test_public_and_frozen_migration_schemas_match_contract_descriptor():
    public_schema_path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "aap_workspace"
        / "seed_member_assignment_protocol.schema.json"
    )
    migration_schema_path = (
        Path(__file__).resolve().parents[2]
        / "api"
        / "alembic"
        / "sql"
        / "0308_workspace_seed_member_assignment.schema.json"
    )

    expected = seed_member_assignment_schema()
    assert json.loads(public_schema_path.read_text(encoding="utf-8")) == expected
    assert json.loads(migration_schema_path.read_text(encoding="utf-8")) == expected
