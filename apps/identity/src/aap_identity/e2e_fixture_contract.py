"""Portable descriptors and fixture values for guarded E2E seed operations."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import StrictBool, StrictStr, TypeAdapter

from .contract import ClosedModel

E2E_FIXTURE_PROTOCOL = "aap-identity-e2e-fixtures/v1"
IDENTITY_FIXTURE_SCHEMA = "aap_identity_fixture_v1"

ACCOUNT_PROFILE_FIELDS = (
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


class FixtureAccountProfile(ClosedModel):
    id: UUID
    email: StrictStr
    name: StrictStr
    role: StrictStr
    group_name: StrictStr | None
    group_id: UUID | None
    status: StrictStr
    is_active: StrictBool
    disabled_kind: StrictStr | None
    disabled_at: datetime | None
    disabled_by: UUID | None
    disabled_reason: StrictStr | None
    email_verified_at: datetime | None
    last_login_at: datetime | None
    last_seen_at: datetime | None
    password_admin_reset_at: datetime | None
    deactivation_requested_at: datetime | None
    deactivation_scheduled_at: datetime | None
    preferences: dict
    avatar_ref: StrictStr | None
    created_at: datetime


class FixtureInvitation(ClosedModel):
    id: UUID
    email: StrictStr
    role: StrictStr
    project_role: StrictStr | None = None
    group_name: StrictStr | None = None
    project_id: UUID | None = None
    token: StrictStr
    expires_at: datetime
    invited_by: UUID
    accepted_at: datetime | None = None
    accepted_user_id: UUID | None = None
    revoked_at: datetime | None = None


def e2e_fixture_schema() -> dict[str, Any]:
    """Return the language-neutral E2E fixture and Native-owner descriptor."""
    schema = TypeAdapter(FixtureAccountProfile | FixtureInvitation).json_schema()
    schema.update(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": E2E_FIXTURE_PROTOCOL,
            "$comment": (
                "Fixed capabilities for existing guarded E2E fixture routes. These "
                "functions accept no arbitrary SQL, table, or predicate selectors."
            ),
            "x-native-capabilities": {
                "protocol": E2E_FIXTURE_PROTOCOL,
                "postgres_schema": IDENTITY_FIXTURE_SCHEMA,
                "functions": [
                    {
                        "name": "create_account",
                        "signature": (
                            "aap_identity_fixture_v1.create_account("
                            "uuid, text, text, text, text)"
                        ),
                        "arguments": [
                            {
                                "name": "p_id",
                                "postgres_type": "uuid",
                                "json_schema": {"type": "string", "format": "uuid"},
                            },
                            {
                                "name": "p_email",
                                "postgres_type": "text",
                                "json_schema": {"type": "string"},
                            },
                            {
                                "name": "p_name",
                                "postgres_type": "text",
                                "json_schema": {"type": "string"},
                            },
                            {
                                "name": "p_password_hash",
                                "postgres_type": "text",
                                "json_schema": {"type": "string"},
                            },
                            {
                                "name": "p_role",
                                "postgres_type": "text",
                                "json_schema": {"type": "string"},
                            },
                        ],
                        "result_postgres_type": "uuid",
                        "result_json_schema": {
                            "type": "string",
                            "format": "uuid",
                        },
                        "volatility": "VOLATILE",
                        "transaction_semantics": (
                            "Insert one User with the exact fixture factory defaults: "
                            "credential_generation 0, active true, offline status, "
                            "empty preferences, and NULL for other nullable fields. "
                            "Role and supplied values are not normalized or subject "
                            "to account policy. Duplicate and native constraint errors "
                            "propagate. created_at uses the database default. The "
                            "caller owns the transaction."
                        ),
                    },
                    {
                        "name": "mark_manual_inactive",
                        "signature": (
                            "aap_identity_fixture_v1.mark_manual_inactive("
                            "uuid, timestamptz, uuid)"
                        ),
                        "arguments": [
                            {
                                "name": "p_id",
                                "postgres_type": "uuid",
                                "json_schema": {"type": "string", "format": "uuid"},
                            },
                            {
                                "name": "p_disabled_at",
                                "postgres_type": "timestamptz",
                                "json_schema": {
                                    "type": "string",
                                    "format": "date-time",
                                },
                            },
                            {
                                "name": "p_disabled_by",
                                "postgres_type": "uuid",
                                "json_schema": {"type": "string", "format": "uuid"},
                            },
                        ],
                        "result_postgres_type": "integer",
                        "result_json_schema": {
                            "type": "integer",
                            "minimum": 0,
                        },
                        "volatility": "VOLATILE",
                        "transaction_semantics": (
                            "Set only is_active=false, status=offline, "
                            "disabled_kind=manual, disabled_at, and disabled_by for "
                            "the exact ID. A matching row counts as one even when "
                            "values are unchanged; a missing ID counts as zero. The "
                            "UPDATE waits for the normal row lock. No actor policy or "
                            "other User fields are changed. The caller owns the "
                            "transaction."
                        ),
                    },
                    {
                        "name": "create_invitations",
                        "signature": (
                            "aap_identity_fixture_v1.create_invitations(jsonb)"
                        ),
                        "arguments": [
                            {
                                "name": "p_invitations",
                                "postgres_type": "jsonb",
                                "json_schema": {
                                    "type": "array",
                                    "items": {"$ref": "#/$defs/FixtureInvitation"},
                                },
                            }
                        ],
                        "result_postgres_type": "integer",
                        "result_json_schema": {
                            "type": "integer",
                            "minimum": 0,
                        },
                        "volatility": "VOLATILE",
                        "transaction_semantics": (
                            "Insert the supplied closed Invitation records without "
                            "normalizing roles or checking account/project authority. "
                            "created_at uses the database default. Invitation field, "
                            "foreign-key, uniqueness, and cast errors propagate. The "
                            "caller owns the transaction."
                        ),
                    },
                    {
                        "name": "delete_accounts",
                        "signature": (
                            "aap_identity_fixture_v1.delete_accounts(boolean, text[])"
                        ),
                        "arguments": [
                            {
                                "name": "p_shared",
                                "postgres_type": "boolean",
                                "json_schema": {"type": "boolean"},
                            },
                            {
                                "name": "p_emails",
                                "postgres_type": "text[]",
                                "json_schema": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                            },
                        ],
                        "result_postgres_type": "integer",
                        "result_json_schema": {
                            "type": "integer",
                            "minimum": 0,
                        },
                        "volatility": "VOLATILE",
                        "transaction_semantics": (
                            "When shared is true, delete Users matching the fixed "
                            "email LIKE '%@e2e.test' predicate. Otherwise delete only "
                            "the exact supplied email array. This function adds no "
                            "generic predicate or dependent-row cleanup. Native FK "
                            "errors propagate; the caller owns deletion ordering and "
                            "the transaction."
                        ),
                    },
                    {
                        "name": "delete_reset_tokens",
                        "signature": (
                            "aap_identity_fixture_v1.delete_reset_tokens(uuid[])"
                        ),
                        "arguments": [
                            {
                                "name": "p_user_ids",
                                "postgres_type": "uuid[]",
                                "json_schema": {
                                    "type": "array",
                                    "items": {"type": "string", "format": "uuid"},
                                },
                            }
                        ],
                        "result_postgres_type": "integer",
                        "result_json_schema": {
                            "type": "integer",
                            "minimum": 0,
                        },
                        "volatility": "VOLATILE",
                        "transaction_semantics": (
                            "Delete only PasswordResetToken rows whose user_id is in "
                            "the exact supplied UUID array. Return the matched row "
                            "count; the caller owns the transaction."
                        ),
                    },
                    {
                        "name": "delete_invitations",
                        "signature": (
                            "aap_identity_fixture_v1.delete_invitations(uuid[])"
                        ),
                        "arguments": [
                            {
                                "name": "p_inviter_ids",
                                "postgres_type": "uuid[]",
                                "json_schema": {
                                    "type": "array",
                                    "items": {"type": "string", "format": "uuid"},
                                },
                            }
                        ],
                        "result_postgres_type": "integer",
                        "result_json_schema": {
                            "type": "integer",
                            "minimum": 0,
                        },
                        "volatility": "VOLATILE",
                        "transaction_semantics": (
                            "Delete only UserInvitation rows whose invited_by is in "
                            "the exact supplied UUID array. Return the matched row "
                            "count; the caller owns the transaction."
                        ),
                    },
                    {
                        "name": "delete_organization_members",
                        "signature": (
                            "aap_identity_fixture_v1.delete_organization_members(uuid[])"
                        ),
                        "arguments": [
                            {
                                "name": "p_user_ids",
                                "postgres_type": "uuid[]",
                                "json_schema": {
                                    "type": "array",
                                    "items": {"type": "string", "format": "uuid"},
                                },
                            }
                        ],
                        "result_postgres_type": "integer",
                        "result_json_schema": {
                            "type": "integer",
                            "minimum": 0,
                        },
                        "volatility": "VOLATILE",
                        "transaction_semantics": (
                            "Delete only organization_members rows whose user_id is in "
                            "the exact supplied UUID array. Return the matched row "
                            "count; the caller owns the transaction and cleanup order."
                        ),
                    },
                ],
                "authorization_boundary": (
                    "These fixed fixture capabilities check only that the actual "
                    "database name ends in _test or _e2e. They add no actor, account, "
                    "project, role, activity, or email authorization policy."
                ),
                "transaction_boundary": (
                    "The caller owns the active transaction, commit, rollback, and "
                    "fixture cleanup ordering. These functions never commit or retry."
                ),
            },
            "x-postgresql-relations": {
                f"{IDENTITY_FIXTURE_SCHEMA}.account_profiles": {
                    "columns": list(ACCOUNT_PROFILE_FIELDS),
                    "row": {"$ref": "#/$defs/FixtureAccountProfile"},
                    "row_key": ["id"],
                    "read_only": True,
                    "security_barrier": True,
                }
            },
        }
    )
    return schema


__all__ = [
    "ACCOUNT_PROFILE_FIELDS",
    "E2E_FIXTURE_PROTOCOL",
    "FixtureAccountProfile",
    "FixtureInvitation",
    "IDENTITY_FIXTURE_SCHEMA",
    "e2e_fixture_schema",
]
