"""Focused checks for caller-session semantics of the Workspace binding adapter."""

from __future__ import annotations

import json
from pathlib import Path
import uuid

import pytest

from app.services.workspace_backend_bindings import (
    clear_primary_backend_binding_for_pool,
)


class _Session:
    def __init__(self, *, autoflush: bool) -> None:
        self.autoflush = autoflush
        self.calls: list[tuple[str, object]] = []

    async def flush(self) -> None:
        self.calls.append(("flush", None))

    async def execute(self, statement, parameters) -> None:
        self.calls.append((str(statement), parameters))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("autoflush", "expected_calls"),
    [(True, ["flush", "execute"]), (False, ["execute"])],
)
async def test_binding_adapter_preserves_session_autoflush_order(
    autoflush: bool, expected_calls: list[str]
) -> None:
    db = _Session(autoflush=autoflush)
    pool_id = uuid.uuid4()

    await clear_primary_backend_binding_for_pool(db, pool_id)  # type: ignore[arg-type]

    assert [call[0] for call in db.calls] == expected_calls
    statement, parameters = db.calls[-1]
    assert "aap_workspace_v1.clear_primary_backend_bindings" in statement
    assert parameters == {"pool_id": pool_id}


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
