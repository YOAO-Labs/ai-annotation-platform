"""Portable descriptor for the fixed fixture-preferences replacement capability."""

from __future__ import annotations

from typing import Any

FIXTURE_PREFERENCES_PROTOCOL = "aap-identity-fixture-preferences/v1"
IDENTITY_SCHEMA = "aap_identity_v1"


def fixture_preferences_schema() -> dict[str, Any]:
    """Return a language-neutral input schema and the exact Native capability."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": FIXTURE_PREFERENCES_PROTOCOL,
        "$defs": {},
        "title": "Fixture user-preferences replacement",
        "type": "object",
        "properties": {
            "user_id": {
                "anyOf": [
                    {"type": "string", "format": "uuid"},
                    {"type": "null"},
                ]
            },
            "preferences": {"type": "object", "additionalProperties": True},
        },
        "required": ["user_id", "preferences"],
        "additionalProperties": False,
        "x-native-capabilities": {
            "protocol": FIXTURE_PREFERENCES_PROTOCOL,
            "postgres_schema": IDENTITY_SCHEMA,
            "functions": [
                {
                    "name": "replace_fixture_preferences",
                    "signature": (
                        "aap_identity_v1.replace_fixture_preferences(uuid,jsonb)"
                    ),
                    "arguments": [
                        {
                            "name": "p_user_id",
                            "postgres_type": "uuid",
                            "nullable": True,
                            "json_schema": {
                                "anyOf": [
                                    {"type": "string", "format": "uuid"},
                                    {"type": "null"},
                                ]
                            },
                        },
                        {
                            "name": "p_preferences",
                            "postgres_type": "jsonb",
                            "json_schema": {
                                "type": "object",
                                "additionalProperties": True,
                            },
                        },
                    ],
                    "result_postgres_type": "integer",
                    "result_json_schema": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 1,
                    },
                    "volatility": "VOLATILE",
                    "transaction_semantics": (
                        "Replace public.users.preferences for the exact supplied ID, "
                        "without merge, actor or account-state policy, journaling, "
                        "retry, or an independent transaction. A matching row counts "
                        "as one even when the JSON value is unchanged; a NULL or "
                        "missing ID counts as zero. The UPDATE uses normal row-lock "
                        "waiting. The caller owns the transaction and commit or "
                        "rollback."
                    ),
                }
            ],
            "authorization_boundary": (
                "This fixture-only capability is limited by the actual database name "
                "ending in _test or _e2e. It performs no actor authentication or "
                "account-role, activity, or email check."
            ),
            "transaction_boundary": (
                "The caller owns the active transaction, commit, and rollback."
            ),
        },
    }


__all__ = [
    "FIXTURE_PREFERENCES_PROTOCOL",
    "IDENTITY_SCHEMA",
    "fixture_preferences_schema",
]
