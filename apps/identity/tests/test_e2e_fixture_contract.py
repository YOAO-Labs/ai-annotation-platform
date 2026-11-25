from __future__ import annotations

from datetime import UTC, datetime
import json
import re
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from yoao_identity.e2e_fixture_contract import (
    ACCOUNT_PROFILE_FIELDS,
    E2E_FIXTURE_PROTOCOL,
    FixtureAccountProfile,
    FixtureInvitation,
    e2e_fixture_schema,
)

_LEGACY_NAMESPACE = re.compile(r"\baap_(?=[a-z0-9_]*_v[0-9]+\b)")


def _current_names(value):
    """Compare frozen artifacts through the current module namespace names."""
    return json.loads(_LEGACY_NAMESPACE.sub("yoao_", json.dumps(value)))


def test_fixture_invitation_is_frozen_closed_and_preserves_legacy_role_values():
    invitation = FixtureInvitation(
        id=uuid4(),
        email="historical@example.test",
        role="reviewer",
        token="legacy-fixture-token",
        expires_at=datetime(2030, 1, 1, tzinfo=UTC),
        invited_by=uuid4(),
    )

    assert invitation.role == "reviewer"
    assert invitation.project_role is None
    assert invitation.group_name is None
    assert invitation.project_id is None
    assert invitation.accepted_at is None
    assert invitation.accepted_user_id is None
    assert invitation.revoked_at is None
    assert set(invitation.model_dump(mode="json")) == {
        "id",
        "email",
        "role",
        "project_role",
        "group_name",
        "project_id",
        "token",
        "expires_at",
        "invited_by",
        "accepted_at",
        "accepted_user_id",
        "revoked_at",
    }

    with pytest.raises(ValidationError):
        FixtureInvitation(
            id=uuid4(),
            email="extra@example.test",
            role="employee",
            token="extra-field-token",
            expires_at=datetime(2030, 1, 1, tzinfo=UTC),
            invited_by=uuid4(),
            unexpected=True,
        )
    with pytest.raises(ValidationError):
        invitation.role = "employee"


def test_account_profile_matches_all_userout_fields_in_source_order():
    assert ACCOUNT_PROFILE_FIELDS == (
        "id",
        "email",
        "name",
        "role",
        "group_name",
        "group_id",
        "status",
        "is_active",
        "disabled_kind",
        "disabled_at",
        "disabled_by",
        "disabled_reason",
        "email_verified_at",
        "last_login_at",
        "last_seen_at",
        "password_admin_reset_at",
        "deactivation_requested_at",
        "deactivation_scheduled_at",
        "preferences",
        "avatar_ref",
        "created_at",
    )
    assert tuple(FixtureAccountProfile.model_fields) == ACCOUNT_PROFILE_FIELDS


def test_schema_snapshots_match_the_owner_descriptor():
    schema = e2e_fixture_schema()
    repository_root = Path(__file__).resolve().parents[3]
    package_snapshot = (
        repository_root
        / "apps/identity/src/yoao_identity/e2e_fixture_protocol.schema.json"
    )
    bundled_snapshot = json.loads(
        (repository_root / "apps/api/alembic/sql/identity.contracts.json").read_text(
            encoding="utf-8"
        )
    )["artifacts"]["0305_identity_e2e_fixtures.schema.json"]

    assert schema["$id"] == E2E_FIXTURE_PROTOCOL
    for raw in (
        json.loads(package_snapshot.read_text(encoding="utf-8")),
        bundled_snapshot,
    ):
        assert _current_names(raw) == schema


def test_descriptor_names_only_the_seven_fixed_fixture_capabilities():
    schema = e2e_fixture_schema()
    capabilities = schema["x-native-capabilities"]
    functions = capabilities["functions"]

    assert [function["signature"] for function in functions] == [
        "yoao_identity_fixture_v1.create_account(uuid, text, text, text, text)",
        "yoao_identity_fixture_v1.mark_manual_inactive(uuid, timestamptz, uuid)",
        "yoao_identity_fixture_v1.create_invitations(jsonb)",
        "yoao_identity_fixture_v1.delete_accounts(boolean, text[])",
        "yoao_identity_fixture_v1.delete_reset_tokens(uuid[])",
        "yoao_identity_fixture_v1.delete_invitations(uuid[])",
        "yoao_identity_fixture_v1.delete_organization_members(uuid[])",
    ]
    assert [function["result_postgres_type"] for function in functions] == [
        "uuid",
        "integer",
        "integer",
        "integer",
        "integer",
        "integer",
        "integer",
    ]
    assert all(function["volatility"] == "VOLATILE" for function in functions)
    assert [argument["name"] for argument in functions[2]["arguments"]] == [
        "p_invitations"
    ]
    assert functions[2]["arguments"][0]["json_schema"] == {
        "type": "array",
        "items": {"$ref": "#/$defs/FixtureInvitation"},
    }
    assert functions[6]["arguments"][0]["name"] == "p_user_ids"
    assert functions[6]["arguments"][0]["postgres_type"] == "uuid[]"
    assert functions[6]["result_postgres_type"] == "integer"

    relations = schema["x-postgresql-relations"]
    assert list(relations) == ["yoao_identity_fixture_v1.account_profiles"]
    relation = relations["yoao_identity_fixture_v1.account_profiles"]
    assert relation["columns"] == list(ACCOUNT_PROFILE_FIELDS)
    assert relation["row"] == {"$ref": "#/$defs/FixtureAccountProfile"}
    assert relation["row_key"] == ["id"]
    assert relation["read_only"] is True
    assert relation["security_barrier"] is True
