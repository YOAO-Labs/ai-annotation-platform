"""Run the fixed maintenance capability only in the existing disposable DB fixture."""

from datetime import date, datetime, timedelta, timezone
import json
import os
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


async def test_maintenance_login_snapshots_exact_json_and_serializes_competing_drop(
    test_db_url,
    apply_migrations,
):
    maintenance_url = os.environ.get("AAP_MODULE_DATA_AUDIT_MAINTENANCE_URL")
    if not maintenance_url:
        pytest.skip(
            "the scoped Audit maintenance LOGIN is provided by module acceptance"
        )

    import asyncpg
    from sqlalchemy.engine import make_url

    from aap_owner_data.postgres import verify_function_only_role

    def asyncpg_dsn(value: str) -> str:
        return (
            make_url(value)
            .set(drivername="postgresql")
            .render_as_string(hide_password=False)
        )

    owner = await asyncpg.connect(asyncpg_dsn(test_db_url), timeout=5)
    maintenance = await asyncpg.connect(asyncpg_dsn(maintenance_url), timeout=5)
    competing = None
    try:
        await verify_function_only_role(
            maintenance,
            schema="aap_audit_v1",
            functions={("maintenance_command", "jsonb")},
            role="audit_maintenance_client",
        )
        month = date(1999, 3, 1)
        await owner.execute(
            "SELECT aap_audit_v1.maintenance_command($1::jsonb)",
            json.dumps(
                {
                    "schema": MAINTENANCE_PROTOCOL,
                    "operation": "ensure",
                    "month": month.isoformat(),
                }
            ),
        )
        action = f"audit.maintenance_native.{uuid4().hex}"
        timestamp = datetime(1999, 3, 2, tzinfo=timezone.utc) + timedelta(
            microseconds=uuid4().int % (27 * 24 * 60 * 60 * 1_000_000)
        )
        await owner.execute(
            "INSERT INTO public.audit_logs(id,action,created_at,detail_json) "
            "VALUES($1,$2,$3,$4::jsonb)",
            9223372036854775807,
            action,
            timestamp,
            '[null, {"integer": 9007199254740993}]',
        )

        partitions = json.loads(
            await maintenance.fetchval(
                "SELECT aap_audit_v1.maintenance_command($1::jsonb)",
                json.dumps({"schema": MAINTENANCE_PROTOCOL, "operation": "partitions"}),
            )
        )
        partition = next(
            item
            for item in partitions["data"]["partitions"]
            if item["name"] == "audit_logs_y1999m03"
        )
        snapshot_request = {
            "schema": MAINTENANCE_PROTOCOL,
            "operation": "snapshot",
            "partition": partition,
        }
        drop_request = {
            "schema": MAINTENANCE_PROTOCOL,
            "operation": "drop",
            "partition": partition,
        }

        transaction = maintenance.transaction()
        await transaction.start()
        snapshot = json.loads(
            await maintenance.fetchval(
                "SELECT aap_audit_v1.maintenance_command($1::jsonb)",
                json.dumps(snapshot_request),
            )
        )
        row = next(
            item for item in snapshot["data"]["rows"] if item["action"] == action
        )
        assert row["id"] == 9223372036854775807
        assert row["detail_json"] == [None, {"integer": 9007199254740993}]

        competing = await asyncpg.connect(asyncpg_dsn(maintenance_url), timeout=5)
        with pytest.raises(asyncpg.LockNotAvailableError) as contention:
            async with competing.transaction():
                await competing.execute("SET LOCAL lock_timeout = '300ms'")
                await competing.fetchval(
                    "SELECT aap_audit_v1.maintenance_command($1::jsonb)",
                    json.dumps(snapshot_request),
                )
        assert contention.value.sqlstate == "55P03"

        dropped = json.loads(
            await maintenance.fetchval(
                "SELECT aap_audit_v1.maintenance_command($1::jsonb)",
                json.dumps(drop_request),
            )
        )
        assert dropped["ok"] is True
        await transaction.rollback()

        restored = json.loads(
            await maintenance.fetchval(
                "SELECT aap_audit_v1.maintenance_command($1::jsonb)",
                json.dumps(snapshot_request),
            )
        )
        assert (
            next(item for item in restored["data"]["rows"] if item["action"] == action)
            == row
        )
    finally:
        if competing is not None:
            await competing.close()
        await maintenance.close()
        await owner.close()
