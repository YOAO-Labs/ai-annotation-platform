from __future__ import annotations

import json
from pathlib import Path
import uuid

import pytest
from pydantic import ValidationError

from aap_workspace.role_audit_contract import (
    ROLE_AUDIT_PROTOCOL,
    ROLE_AUDIT_SCHEMA,
    ROLE_AUDIT_VIEWS,
    WorkspaceRoleAuditProject,
    WorkspaceRoleAuditProjectMember,
    WorkspaceRoleAuditSchemaCapabilities,
    workspace_role_audit_schema,
)


def test_workspace_rows_keep_unknown_values_and_nullable_diagnostics():
    project_id = uuid.uuid4()
    project = WorkspaceRoleAuditProject(
        id=project_id,
        owner_id=None,
        status="historical_unknown_status",
    )
    member = WorkspaceRoleAuditProjectMember(
        id=uuid.uuid4(),
        project_id=project_id,
        user_id=None,
        role="historical_unknown_role",
    )

    assert project.owner_id is None
    assert project.status == "historical_unknown_status"
    assert member.user_id is None
    assert member.role == "historical_unknown_role"
    assert (
        WorkspaceRoleAuditSchemaCapabilities(
            member_version=False,
            member_updated_at=False,
        ).member_version
        is False
    )


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (
            WorkspaceRoleAuditProject,
            {"id": "not-a-uuid", "owner_id": None, "status": "active"},
        ),
        (
            WorkspaceRoleAuditProject,
            {"id": str(uuid.uuid4()), "owner_id": None, "status": 1},
        ),
        (
            WorkspaceRoleAuditProject,
            {
                "id": str(uuid.uuid4()),
                "owner_id": None,
                "status": "x" * 31,
            },
        ),
        (
            WorkspaceRoleAuditProjectMember,
            {
                "id": str(uuid.uuid4()),
                "project_id": None,
                "user_id": None,
                "role": "annotator",
                "private": True,
            },
        ),
        (
            WorkspaceRoleAuditProjectMember,
            {
                "id": str(uuid.uuid4()),
                "project_id": None,
                "user_id": None,
                "role": 3,
            },
        ),
        (
            WorkspaceRoleAuditSchemaCapabilities,
            {"member_version": 1, "member_updated_at": False},
        ),
    ],
)
def test_relation_rows_are_strict_and_closed(model, payload):
    with pytest.raises(ValidationError):
        model.model_validate(payload)


def test_schema_freezes_exact_views_columns_and_legacy_capabilities():
    schema = workspace_role_audit_schema()
    descriptor = schema["x-postgres-read-relations"]
    relations = descriptor["relations"]

    assert schema["$id"] == ROLE_AUDIT_PROTOCOL
    assert descriptor["protocol"] == ROLE_AUDIT_PROTOCOL
    assert [relation["name"] for relation in relations] == list(ROLE_AUDIT_VIEWS)
    assert [relation["qualified_name"] for relation in relations] == [
        f"{ROLE_AUDIT_SCHEMA}.{name}" for name in ROLE_AUDIT_VIEWS
    ]
    assert [
        (column["name"], column["postgres_type"], column["nullable"])
        for column in relations[0]["columns"]
    ] == [
        ("id", "uuid", False),
        ("owner_id", "uuid", True),
        ("status", "character varying(30)", True),
    ]
    assert [
        (column["name"], column["postgres_type"], column["nullable"])
        for column in relations[1]["columns"]
    ] == [
        ("id", "uuid", False),
        ("project_id", "uuid", True),
        ("user_id", "uuid", True),
        ("role", "character varying(30)", True),
    ]
    assert [column["name"] for column in relations[2]["columns"]] == [
        "member_version",
        "member_updated_at",
    ]
    assert all(column["nullable"] is False for column in relations[2]["columns"])
    assert "Identity joins" in relations[1]["semantics"]
    assert "without reinstalling" in relations[2]["semantics"]
    assert "One row for every" in relations[0]["semantics"]


def test_schema_files_match_the_portable_descriptor():
    import aap_workspace.role_audit_contract as contract

    expected = contract.workspace_role_audit_schema()
    package_schema = Path(contract.__file__).with_name("role_audit.schema.json")
    assert json.loads(package_schema.read_text(encoding="utf-8")) == expected

    repository = Path(__file__).resolve().parents[3]
    assert (
        json.loads(
            (
                repository / "infra" / "docker" / "workspace-role-audit.schema.json"
            ).read_text(encoding="utf-8")
        )
        == expected
    )
