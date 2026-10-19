"""Check the deployed login's effective capabilities, without touching source data."""

from __future__ import annotations

from aap_assets_media.contract import OPERATIONS
from aap_assets_video_inputs.execution import OPERATIONS as VIDEO_INPUT_OPERATIONS
from aap_assets_video_inputs.tracker import TRACKER_OPERATIONS

_IDENTITY = """
SELECT current_user = session_user
  AND NOT (r.rolsuper OR r.rolcreaterole OR r.rolcreatedb OR r.rolreplication OR r.rolbypassrls)
  AND NOT EXISTS (SELECT 1 FROM pg_auth_members WHERE member = r.oid)
  AND NOT has_database_privilege(current_user, current_database(), 'CREATE')
  AND NOT has_database_privilege(current_user, current_database(), 'TEMP')
  AND NOT EXISTS (
    SELECT 1 FROM pg_namespace n
    WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
      AND has_schema_privilege(current_user, n.oid, 'CREATE'))
  AND NOT EXISTS (
    SELECT 1 FROM pg_namespace n
    WHERE n.nspname !~ '^pg_' AND n.nspname NOT IN ('information_schema','public','aap_assets_v1')
      AND has_schema_privilege(current_user, n.oid, 'USAGE'))
  AND NOT EXISTS (
    SELECT 1 FROM pg_class t JOIN pg_namespace n ON n.oid=t.relnamespace
    WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
      AND ((t.relkind IN ('r','p','v','m','f') AND (
        has_table_privilege(current_user,t.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
        OR has_any_column_privilege(current_user,t.oid,'SELECT,INSERT,UPDATE,REFERENCES')))
        OR (t.relkind='S' AND has_sequence_privilege(current_user,t.oid,'USAGE,SELECT,UPDATE'))))
FROM pg_roles r WHERE r.rolname=current_user
"""
_FUNCTIONS = """
SELECT n.nspname || '.' || p.proname || '(' || oidvectortypes(p.proargtypes) || ')' AS signature,
       p.prosecdef
FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
WHERE n.nspname ~ '^aap_[a-z_]+_v1$'
  AND has_function_privilege(current_user,p.oid,'EXECUTE')
ORDER BY 1
"""


async def check_capabilities(pool, role: str) -> bool:
    if role not in {"control", "worker"}:
        return False
    if role == "control":
        from aap_assets_images_runtime.health import (
            check_capabilities as shared_control,
        )

        return await shared_control(pool, "control")
    wanted_roles = {"control", "system"} if role == "control" else {"worker"}
    expected = {
        "aap_assets_v1." + name + "(" + ", ".join(op.sql_types) + ")"
        for name, op in OPERATIONS.items()
        if op.role in wanted_roles
    }
    expected.update(
        "aap_assets_v1." + name + "(" + ", ".join(op.sql_types) + ")"
        for name, op in VIDEO_INPUT_OPERATIONS.items()
        if op.role in {"worker", "system"}
    )
    expected.update(
        "aap_assets_v1." + name + "(" + ", ".join(op.sql_types) + ")"
        for name, op in TRACKER_OPERATIONS.items()
        if op.namespace == "aap_assets_v1" and op.role == "worker"
    )
    try:
        async with pool.acquire(timeout=2) as connection:
            async with connection.transaction(readonly=True):
                await connection.execute("SET LOCAL statement_timeout='5s'")
                if not await connection.fetchval(_IDENTITY):
                    return False
                rows = await connection.fetch(_FUNCTIONS)
                return {row["signature"] for row in rows} == expected and all(
                    row["prosecdef"] for row in rows
                )
    except Exception:
        return False
