"""Run the fixed maintenance capability only in the existing disposable DB fixture."""

from datetime import date
import json
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from aap_audit.maintenance_contract import MAINTENANCE_PROTOCOL
from aap_audit.maintenance_postgres import AuditMaintenancePostgresData


async def test_maintenance_preserves_partition_identity_lock_and_caller_rollback(
    db_session,
):
    data = AuditMaintenancePostgresData(db_session)
    month = date(1999, 3, 1)
    await data.ensure_partition(month)
    partition = next(
        item for item in await data.list_partitions() if item.range_from == month
    )
    action = f"audit.archive_probe.{uuid4().hex}"
    await db_session.execute(
        text(
            "INSERT INTO public.audit_logs (id, action, created_at, detail_json) "
            "VALUES (9223372036854775807, :action, "
            "TIMESTAMPTZ '1999-03-02 03:04:05+00', CAST(:detail AS jsonb))"
        ),
        {"action": action, "detail": '[null, {"integer": 9007199254740993}]'},
    )

    savepoint = await db_session.begin_nested()
    with pytest.raises(DBAPIError) as missing_lock:
        await data.drop_partition(partition)
    assert missing_lock.value.orig.sqlstate == "55000"
    await savepoint.rollback()

    savepoint = await db_session.begin_nested()
    with pytest.raises(DBAPIError) as wrong_identity:
        await data.snapshot_partition(
            partition.model_copy(update={"relation_oid": partition.relation_oid + 1})
        )
    assert wrong_identity.value.orig.sqlstate == "55000"
    await savepoint.rollback()

    savepoint = await db_session.begin_nested()
    with pytest.raises(DBAPIError) as extra_key:
        await db_session.scalar(
            text("SELECT aap_audit_v1.maintenance_command(CAST(:request AS jsonb))"),
            {
                "request": json.dumps(
                    {
                        "schema": MAINTENANCE_PROTOCOL,
                        "operation": "partitions",
                        "extra": True,
                    }
                )
            },
        )
    assert extra_key.value.orig.sqlstate == "22023"
    await savepoint.rollback()

    rows = await data.snapshot_partition(partition)
    row = next(item for item in rows if item["action"] == action)
    assert row["id"] == 9223372036854775807
    assert row["detail_json"] == [None, {"integer": 9007199254740993}]

    savepoint = await db_session.begin_nested()
    await data.drop_partition(partition)
    assert (
        await db_session.scalar(
            text("SELECT to_regclass(:partition)"),
            {"partition": f"public.{partition.name}"},
        )
        is None
    )
    await savepoint.rollback()

    restored = next(
        item for item in await data.list_partitions() if item.name == partition.name
    )
    assert restored == partition
    assert (
        next(
            item
            for item in await data.snapshot_partition(restored)
            if item["action"] == action
        )
        == row
    )
