from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
import uuid

import pytest

from aap_notifications.inbox_query_contract import (
    INBOX_GLOBAL_ASYNC_JOB_KINDS,
    INBOX_QUERY_FUNCTIONS,
    notification_inbox_query_schema,
    NotificationInboxFact,
)


def test_inbox_query_contract_and_frozen_sql_are_closed_and_identical():
    root = Path(__file__).resolve().parents[3]
    package_schema = (
        root / "apps/notifications/src/aap_notifications/inbox_query.schema.json"
    ).read_bytes()
    api_schema = (
        root / "apps/api/alembic/sql/0324_notifications_inbox_query.schema.json"
    ).read_bytes()
    migration = (
        root / "apps/api/alembic/versions/0324_notifications_inbox_query.py"
    ).read_text(encoding="utf-8")
    package_sql = (
        root
        / "apps/notifications/src/aap_notifications/sql/0324_notifications_inbox_query.sql"
    ).read_text(encoding="utf-8")
    api_sql = (
        root / "apps/api/alembic/sql/0324_notifications_inbox_query.sql"
    ).read_text(encoding="utf-8")

    schema = json.loads(package_schema)
    assert schema == notification_inbox_query_schema()
    assert package_schema == api_schema
    assert package_sql == api_sql
    assert 'revision = "0324"' in migration
    assert 'down_revision = "0323"' in migration
    assert (
        "DROP FUNCTION aap_notifications_v1._visible_inbox_rows(uuid, text) RESTRICT"
        in migration
    )
    assert schema["$id"] == "aap-notifications-inbox-query/v1"
    assert schema["additionalProperties"] is False
    assert schema["properties"]["payload"].get("type") is None
    assert (
        schema["x-global-async-job-kinds"]
        == list(INBOX_GLOBAL_ASYNC_JOB_KINDS)
        == ["audit_archive", "dataset_import"]
    )
    assert schema["x-postgresql-functions"] == {
        name: {"arguments": args, "returns": returns}
        for name, (args, returns) in INBOX_QUERY_FUNCTIONS.items()
    }
    assert schema["x-trusted-role-snapshot"] == (
        "IdentityAccountLockData.read_actor_status"
    )
    assert schema["x-security"]["public_execute"] is False
    assert schema["x-security"]["private_helper_execute"] is False

    assert package_sql.count("SECURITY DEFINER") == 4
    assert package_sql.count("SET search_path = pg_catalog, pg_temp") == 4
    assert "aap_workspace_v1.actor_project_memberships(p_actor_id)" in package_sql
    assert "aap_workspace_project_query_v1.project_search_scope(" in package_sql
    assert "aap_jobs_v1.notification_scope_facts" in package_sql
    assert (
        "REVOKE ALL ON FUNCTION aap_notifications_v1._visible_inbox_rows(uuid, text)"
        in package_sql
    )
    assert "_inbox_visible_rows" not in package_sql
    assert "notification.payload ->> 'project_id'" in package_sql
    assert "ORDER BY notification.created_at DESC" in package_sql
    for private_table in (
        "public.notifications",
        "public.async_jobs",
        "public.project_members",
        "public.projects",
    ):
        assert private_table not in package_sql
    assert "GRANT EXECUTE" not in package_sql


@pytest.mark.parametrize(
    "payload",
    [None, {}, [], ["project_id"], "project_id", 5, True],
)
def test_inbox_row_contract_preserves_native_jsonb_payload(payload):
    actor_id = uuid.uuid4()
    row = NotificationInboxFact(
        id=uuid.uuid4(),
        user_id=actor_id,
        type="inbox.test",
        target_type="task",
        target_id=uuid.uuid4(),
        payload=payload,
        read_at=None,
        created_at=datetime.now(timezone.utc),
    )

    assert row.payload == payload
