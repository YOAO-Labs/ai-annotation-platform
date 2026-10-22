from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from aap_identity.role_audit_contract import (
    ROLE_AUDIT_PROTOCOL,
    ROLE_AUDIT_SCHEMA,
    ROLE_AUDIT_VIEWS,
    RoleAuditAccount,
    RoleAuditInvitation,
    RoleAuditSchemaCapabilities,
    role_audit_schema,
)


def test_account_row_preserves_unknown_roles_and_nullable_diagnostics():
    account_id = uuid.uuid4()
    account = RoleAuditAccount(
        id=account_id,
        role="historical_unknown_role",
        is_active=None,
    )
    assert account.id == account_id
    assert account.role == "historical_unknown_role"
    assert account.is_active is None

    assert RoleAuditAccount(id=account_id, role=None, is_active=False).role is None


def test_invitation_row_keeps_legacy_missing_project_role_as_null():
    invitation = RoleAuditInvitation(
        id=uuid.uuid4(),
        project_id=None,
        role="legacy_invitation_role",
        project_role=None,
        expires_at=datetime(2027, 1, 1, tzinfo=UTC),
        accepted_at=None,
        revoked_at=None,
        invited_by=uuid.uuid4(),
    )
    assert invitation.project_role is None
    assert invitation.accepted_at is None
    assert invitation.revoked_at is None

    capability = RoleAuditSchemaCapabilities(invitation_project_role=False)
    assert capability.invitation_project_role is False


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (RoleAuditAccount, {"id": "not-a-uuid", "role": "employee", "is_active": True}),
        (RoleAuditAccount, {"id": str(uuid.uuid4()), "role": 1, "is_active": True}),
        (
            RoleAuditAccount,
            {"id": str(uuid.uuid4()), "role": "employee", "is_active": 1},
        ),
        (
            RoleAuditAccount,
            {"id": str(uuid.uuid4()), "role": "x" * 51, "is_active": True},
        ),
        (
            RoleAuditAccount,
            {
                "id": str(uuid.uuid4()),
                "role": "employee",
                "is_active": True,
                "extra": 1,
            },
        ),
        (
            RoleAuditInvitation,
            {
                "id": str(uuid.uuid4()),
                "project_id": None,
                "role": 1,
                "project_role": None,
                "expires_at": datetime(2027, 1, 1, tzinfo=UTC),
                "accepted_at": None,
                "revoked_at": None,
                "invited_by": uuid.uuid4(),
            },
        ),
        (
            RoleAuditInvitation,
            {
                "id": uuid.uuid4(),
                "project_id": None,
                "role": "employee",
                "project_role": None,
                "expires_at": datetime(2027, 1, 1),
                "accepted_at": None,
                "revoked_at": None,
                "invited_by": uuid.uuid4(),
            },
        ),
        (
            RoleAuditSchemaCapabilities,
            {"invitation_project_role": 1},
        ),
    ],
)
def test_relation_rows_are_strict_and_closed(model, payload):
    with pytest.raises(ValidationError):
        model.model_validate(payload)


def test_schema_freezes_all_three_read_relations_and_nullable_fields():
    schema = role_audit_schema()
    assert schema["$id"] == ROLE_AUDIT_PROTOCOL
    descriptor = schema["x-postgres-read-relations"]
    assert descriptor["protocol"] == ROLE_AUDIT_PROTOCOL
    relations = descriptor["relations"]
    assert [relation["name"] for relation in relations] == list(ROLE_AUDIT_VIEWS)
    assert [relation["qualified_name"] for relation in relations] == [
        f"{ROLE_AUDIT_SCHEMA}.{name}" for name in ROLE_AUDIT_VIEWS
    ]
    assert [
        (column["name"], column["postgres_type"], column["nullable"])
        for column in relations[0]["columns"]
    ] == [
        ("id", "uuid", False),
        ("role", "character varying(50)", True),
        ("is_active", "boolean", True),
    ]
    assert [
        (column["name"], column["postgres_type"], column["nullable"])
        for column in relations[1]["columns"]
    ] == [
        ("id", "uuid", False),
        ("project_id", "uuid", True),
        ("role", "character varying(32)", False),
        ("project_role", "text", True),
        ("expires_at", "timestamp with time zone", False),
        ("accepted_at", "timestamp with time zone", True),
        ("revoked_at", "timestamp with time zone", True),
        ("invited_by", "uuid", False),
    ]
    assert relations[2]["columns"][0]["name"] == "invitation_project_role"
    assert relations[2]["columns"][0]["nullable"] is False
    invitation_column_names = {column["name"] for column in relations[1]["columns"]}
    assert "invited_by" in invitation_column_names
    assert not invitation_column_names.intersection(
        {"email", "token", "accepted_user_id", "created_at"}
    )

    assert "no filtering" in relations[0]["semantics"]
    assert "Unknown role strings" in relations[0]["semantics"]
    assert "absent on legacy schemas" in relations[1]["semantics"]
    assert "without reapplying this installer" in relations[1]["semantics"]
    assert "ordinary Identity API consumer" in descriptor["authorization_boundary"]
    assert "five-function, " in descriptor["authorization_boundary"]
    assert "two-view" in descriptor["authorization_boundary"]
