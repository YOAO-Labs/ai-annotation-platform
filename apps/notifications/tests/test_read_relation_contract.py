from __future__ import annotations

import json
from pathlib import Path

from aap_notifications.read_relation_contract import notification_read_relation_schema


def test_read_relation_schema_and_frozen_sql_resource_are_closed_and_versioned():
    root = Path(__file__).resolve().parents[3]
    package_schema = (
        root / "apps/notifications/src/aap_notifications/read_relations.schema.json"
    ).read_bytes()
    api_schema = (
        root / "apps/api/alembic/sql/0319_notifications_read_relations.schema.json"
    ).read_bytes()
    package_sql = (
        root
        / "apps/notifications/src/aap_notifications/sql/0319_notifications_read_relations.sql"
    ).read_text(encoding="utf-8")
    api_sql = (
        root / "apps/api/alembic/sql/0319_notifications_read_relations.sql"
    ).read_bytes()

    schema = json.loads(package_schema)
    relation = schema["x-postgresql-relations"][
        "aap_notifications_v1.notification_facts"
    ]
    columns = [
        "id",
        "user_id",
        "type",
        "target_type",
        "target_id",
        "payload",
        "read_at",
        "created_at",
    ]
    assert schema["$id"] == "aap-notifications-read-relations/v1"
    assert schema["additionalProperties"] is False
    assert relation["columns"] == columns
    assert relation["row_schema"] == "#"
    assert relation["row_key"] == ["id"]
    assert relation["read_only"] is True
    assert relation["security_barrier"] is True
    assert list(schema["properties"]) == columns
    assert set(schema["required"]) == set(columns)
    assert schema == notification_read_relation_schema()
    assert package_schema == api_schema
    assert package_sql.encode() == api_sql
    assert "CREATE VIEW aap_notifications_v1.notification_facts" in package_sql
    assert "WITH (security_barrier = true)" in package_sql
    assert "FROM public.notifications AS notification" in package_sql
    assert (
        "REVOKE ALL ON aap_notifications_v1.notification_facts FROM PUBLIC"
        in package_sql
    )
    assert "SELECT *" not in package_sql
    assert "OFFSET" not in package_sql
