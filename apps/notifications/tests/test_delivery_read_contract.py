from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
import re
import uuid

import pytest
from pydantic import ValidationError

from aap_notifications.delivery_read_contract import (
    DELIVERY_READ_FUNCTIONS,
    DeliveryReadPair,
    NotificationDeliveryReadFact,
    notification_delivery_read_schema,
)


def test_delivery_read_contract_and_frozen_sql_are_identical():
    root = Path(__file__).resolve().parents[3]
    package_schema = (
        root / "apps/notifications/src/aap_notifications/delivery_read.schema.json"
    ).read_bytes()
    api_schema = (
        root / "apps/api/alembic/sql/0325_notifications_delivery_read.schema.json"
    ).read_bytes()
    package_sql = (
        root
        / "apps/notifications/src/aap_notifications/sql/0325_notifications_delivery_read.sql"
    ).read_text(encoding="utf-8")
    api_sql = (
        root / "apps/api/alembic/sql/0325_notifications_delivery_read.sql"
    ).read_text(encoding="utf-8")
    migration = (
        root / "apps/api/alembic/versions/0325_notifications_delivery_read.py"
    ).read_text(encoding="utf-8")

    schema = json.loads(package_schema)
    assert schema == notification_delivery_read_schema()
    assert package_schema == api_schema
    assert package_sql == api_sql
    assert 'revision = "0325"' in migration
    assert 'down_revision = "0324"' in migration
    assert schema["$id"] == "aap-notifications-delivery-read/v1"
    assert schema["additionalProperties"] is False
    assert schema["properties"]["payload"].get("type") is None
    assert schema["x-row-columns"] == [
        "id",
        "user_id",
        "type",
        "target_type",
        "target_id",
        "payload",
        "read_at",
        "created_at",
    ]
    assert schema["x-row-key"] == ["id"]
    assert schema["x-postgresql-functions"] == DELIVERY_READ_FUNCTIONS
    assert schema["x-pair-input"]["exact_keys"] == ["project_id", "user_id"]
    assert schema["x-pair-input"]["cardinality_cap"] is None
    assert schema["x-error-fallback"]["after_scope_read_failure"]
    assert schema["x-security"]["invalid_pair_sqlstate"] == "22023"

    function_names = [name.rsplit(".", 1)[1] for name in DELIVERY_READ_FUNCTIONS]
    for name in function_names:
        assert f"CREATE FUNCTION aap_notifications_v1.{name}(" in package_sql
        assert re.search(
            rf"REVOKE ALL ON FUNCTION\s+aap_notifications_v1\.{name}\(",
            package_sql,
        )
    assert package_sql.count("SECURITY DEFINER") == len(function_names)
    assert package_sql.count("SET search_path = pg_catalog, pg_temp") == len(
        function_names
    )
    for forbidden in (
        "public.notifications",
        "public.async_jobs",
        "public.project_members",
        "public.projects",
        "GRANT EXECUTE",
    ):
        assert forbidden not in package_sql
    assert "aap_identity_v1.account_profiles" in package_sql
    assert "aap_workspace_v1.actor_project_memberships" in package_sql
    assert "aap_workspace_project_query_v1.project_search_scope" in package_sql
    assert "aap_jobs_v1.notification_scope_facts" in package_sql
    assert (
        package_sql.count(
            "(item.value - 'project_id' - 'user_id') <> '{}'::pg_catalog.jsonb"
        )
        == 4
    )
    assert "jsonb_object_length" not in package_sql


@pytest.mark.parametrize("payload", [None, {}, [], [1, "x"], "x", 7, True])
def test_delivery_fact_keeps_native_jsonb_payload_shapes(payload):
    fact = NotificationDeliveryReadFact(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        type="task.approved",
        target_type="task",
        target_id=uuid.uuid4(),
        payload=payload,
        read_at=None,
        created_at=datetime.now(timezone.utc),
    )

    assert fact.payload == payload


def test_delivery_pair_contract_rejects_extra_fields():
    project_id = uuid.uuid4()
    user_id = uuid.uuid4()

    assert DeliveryReadPair.model_validate(
        {"project_id": project_id, "user_id": user_id}
    ) == DeliveryReadPair(project_id=project_id, user_id=user_id)
    with pytest.raises(ValidationError):
        DeliveryReadPair.model_validate(
            {"project_id": project_id, "user_id": user_id, "extra": True}
        )
