from __future__ import annotations

import json
from pathlib import Path

from aap_jobs.notification_scope_contract import (
    NOTIFICATION_SCOPE_COLUMNS,
    NOTIFICATION_SCOPE_PROTOCOL,
    NOTIFICATION_SCOPE_RELATION,
    NotificationScopeFact,
    notification_scope_schema,
    notification_scope_sql,
)


def test_notification_scope_contract_and_sql_resource_are_closed():
    root = Path(__file__).resolve().parents[3]
    schema_path = root / "apps/jobs/src/aap_jobs/notification_scope.schema.json"
    resource_path = root / "apps/jobs/src/aap_jobs/sql/notification_scope_facts.sql"
    api_schema_path = (
        root / "apps/api/alembic/sql/0323_jobs_notification_scope_facts.schema.json"
    )
    api_resource_path = (
        root / "apps/api/alembic/sql/0323_jobs_notification_scope_facts.sql"
    )
    migration_path = (
        root / "apps/api/alembic/versions/0323_jobs_notification_scope_facts.py"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    sql = resource_path.read_text(encoding="utf-8")
    relation = schema["x-postgresql-relations"][NOTIFICATION_SCOPE_RELATION]

    assert schema == notification_scope_schema()
    assert schema["$id"] == NOTIFICATION_SCOPE_PROTOCOL
    assert schema["additionalProperties"] is False
    assert list(schema["properties"]) == list(NOTIFICATION_SCOPE_COLUMNS)
    assert schema["required"] == list(NOTIFICATION_SCOPE_COLUMNS)
    assert relation["column_types"] == {
        "id": "uuid",
        "kind": "character varying(40)",
        "scope_project_id": "uuid",
    }
    assert relation["nullable_columns"] == ["scope_project_id"]
    assert relation["row_key"] == ["id"]
    assert relation["read_only"] is True
    assert relation["security_barrier"] is True
    assert "CREATE VIEW aap_jobs_v1.notification_scope_facts" in sql
    assert "WITH (security_barrier = true)" in sql
    assert (
        "COALESCE(job.project_id, job.retained_project_id) AS scope_project_id" in sql
    )
    assert "FROM public.async_jobs AS job" in sql
    assert "REVOKE ALL ON aap_jobs_v1.notification_scope_facts FROM PUBLIC" in sql
    assert "SELECT *" not in sql
    assert notification_scope_sql() == sql
    assert schema_path.read_bytes() == api_schema_path.read_bytes()
    assert sql.encode() == api_resource_path.read_bytes()
    migration = migration_path.read_text(encoding="utf-8")
    assert 'revision = "0323"' in migration
    assert 'down_revision = "0322"' in migration


def test_notification_scope_keeps_unknown_job_kinds_and_null_scope():
    fact = NotificationScopeFact.model_validate_json(
        json.dumps(
            {
                "id": "3af0d5e6-fb54-45e6-ae26-c38dbf54f0cc",
                "kind": "future_unrecognized_job_kind",
                "scope_project_id": None,
            }
        )
    )

    assert fact.kind == "future_unrecognized_job_kind"
    assert fact.scope_project_id is None
