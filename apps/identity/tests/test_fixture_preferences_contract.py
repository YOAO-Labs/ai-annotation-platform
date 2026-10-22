from __future__ import annotations

import json
from pathlib import Path

from aap_identity.fixture_preferences_contract import (
    FIXTURE_PREFERENCES_PROTOCOL,
    fixture_preferences_schema,
)


def test_schema_is_a_closed_language_neutral_input_object():
    schema = fixture_preferences_schema()

    assert schema["$id"] == FIXTURE_PREFERENCES_PROTOCOL
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["user_id", "preferences"]
    assert set(schema["properties"]) == {"user_id", "preferences"}
    assert schema["properties"]["user_id"] == {
        "anyOf": [
            {"type": "string", "format": "uuid"},
            {"type": "null"},
        ]
    }
    assert schema["properties"]["preferences"] == {
        "type": "object",
        "additionalProperties": True,
    }


def test_descriptor_names_one_exact_scalar_native_capability():
    capabilities = fixture_preferences_schema()["x-native-capabilities"]

    assert set(capabilities) == {
        "protocol",
        "postgres_schema",
        "functions",
        "authorization_boundary",
        "transaction_boundary",
    }
    assert capabilities["protocol"] == FIXTURE_PREFERENCES_PROTOCOL
    assert capabilities["postgres_schema"] == "aap_identity_v1"
    assert len(capabilities["functions"]) == 1

    function = capabilities["functions"][0]
    assert set(function) == {
        "name",
        "signature",
        "arguments",
        "result_postgres_type",
        "result_json_schema",
        "volatility",
        "transaction_semantics",
    }
    assert function["name"] == "replace_fixture_preferences"
    assert function["signature"] == (
        "aap_identity_v1.replace_fixture_preferences(uuid,jsonb)"
    )
    assert [argument["name"] for argument in function["arguments"]] == [
        "p_user_id",
        "p_preferences",
    ]
    assert function["arguments"][0] == {
        "name": "p_user_id",
        "postgres_type": "uuid",
        "nullable": True,
        "json_schema": {
            "anyOf": [
                {"type": "string", "format": "uuid"},
                {"type": "null"},
            ]
        },
    }
    assert function["arguments"][1] == {
        "name": "p_preferences",
        "postgres_type": "jsonb",
        "json_schema": {
            "type": "object",
            "additionalProperties": True,
        },
    }
    assert function["result_postgres_type"] == "integer"
    assert function["result_json_schema"] == {
        "type": "integer",
        "minimum": 0,
        "maximum": 1,
    }
    assert function["volatility"] == "VOLATILE"
    assert "caller owns" in function["transaction_semantics"]
    assert "normal row-lock waiting" in function["transaction_semantics"]


def test_installed_and_migration_descriptors_match_the_owner_contract():
    root = Path(__file__).resolve().parents[3]
    for relative in (
        "apps/identity/src/aap_identity/fixture_preferences.schema.json",
        "apps/api/alembic/sql/0304_identity_fixture_preferences.schema.json",
    ):
        assert json.loads((root / relative).read_text()) == fixture_preferences_schema()
