"""One-shot, function-only Audit maintenance commands."""

from __future__ import annotations

import argparse
import asyncio
import json

from .maintenance_config import MaintenanceConfig, load_config

MAINTENANCE_SCHEMA = "aap_audit_v1"
MAINTENANCE_FUNCTIONS = {("maintenance_command", "jsonb")}
DATABASE_CONNECT_TIMEOUT_SECONDS = 5
DATABASE_COMMAND_TIMEOUT_SECONDS = 60


async def _call_maintenance(connection, request: dict):
    encoded = json.dumps(
        request,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    reply = await connection.fetchval(
        "SELECT aap_audit_v1.maintenance_command($1::jsonb)", encoded
    )
    if isinstance(reply, str):
        return json.loads(reply)
    return reply


async def run_operation(
    command: str,
    config: MaintenanceConfig,
) -> dict:
    if command not in {"ensure-partitions", "archive-partitions", "refresh-daily"}:
        raise ValueError("unsupported Audit maintenance command")
    import asyncpg

    from aap_owner_data.postgres import verify_function_only_role

    from .maintenance_data import AuditMaintenanceData
    from .partitions import AuditPartitionManager

    storage = config.archive_storage() if command == "archive-partitions" else None
    connection = None
    try:
        connection = await asyncpg.connect(
            config.database_url,
            timeout=DATABASE_CONNECT_TIMEOUT_SECONDS,
            command_timeout=DATABASE_COMMAND_TIMEOUT_SECONDS,
            server_settings={
                "statement_timeout": str(DATABASE_COMMAND_TIMEOUT_SECONDS * 1000),
                "application_name": "aap-audit-maintenance",
            },
        )
        async with connection.transaction():
            role = await connection.fetchval("SELECT current_user")
            await verify_function_only_role(
                connection,
                schema=MAINTENANCE_SCHEMA,
                functions=MAINTENANCE_FUNCTIONS,
                role=role,
            )
            data = AuditMaintenanceData(
                lambda request: _call_maintenance(connection, request)
            )
            manager = AuditPartitionManager(data, storage)
            if command == "ensure-partitions":
                result = {"created": await manager.ensure_future_partitions()}
            elif command == "archive-partitions":
                result = await manager.archive_old_partitions()
            else:
                await data.refresh_daily()
                result = {"refreshed": True}
        return result
    finally:
        try:
            if connection is not None:
                await connection.close(timeout=DATABASE_CONNECT_TIMEOUT_SECONDS)
        finally:
            if storage is not None:
                storage.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run one explicit Audit maintenance operation"
    )
    parser.add_argument(
        "command",
        choices=("ensure-partitions", "archive-partitions", "refresh-daily"),
    )
    args = parser.parse_args()
    try:
        config = load_config(
            require_archive_storage=args.command == "archive-partitions"
        )
        result = asyncio.run(run_operation(args.command, config))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception:
        raise SystemExit(
            "Audit maintenance failed: check configuration, owner capability, or service availability"
        ) from None
    print(json.dumps(result, separators=(",", ":"), ensure_ascii=False))
