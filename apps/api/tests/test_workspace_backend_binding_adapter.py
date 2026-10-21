"""Published schema for the fixed Workspace backend-binding command."""

from __future__ import annotations

import json
from pathlib import Path


def test_binding_native_schema_publishes_only_the_fixed_clear_operation() -> None:
    schema_path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "sql"
        / "0272_workspace_backend_bindings.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    assert schema["$id"] == "aap-workspace-backend-bindings-clear/v1"
    assert schema["x-postgresql-functions"] == [
        "aap_workspace_v1.clear_primary_backend_bindings(uuid)"
    ]
    request = schema["$defs"]["ClearPrimaryBackendBindingsRequest"]
    assert request["required"] == ["pool_id"]
    assert request["properties"]["pool_id"] == {
        "type": "string",
        "format": "uuid",
        "description": "Non-null pool UUID supplied by the registry-deletion caller.",
    }
