"""Exact capability checks for image worker and shared control logins."""

from __future__ import annotations

from aap_assets_images.contract import OPERATIONS as IMAGE_OPERATIONS
from aap_assets_media.contract import OPERATIONS as VIDEO_OPERATIONS
from aap_assets_inputs.contract import OPERATIONS as INPUT_OPERATIONS
from aap_assets_input_manifests.contract import OPERATIONS as MANIFEST_OPERATIONS
from aap_assets_video_inputs.execution import OPERATIONS as VIDEO_INPUT_OPERATIONS

_ROLE_SQL = """
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

_FUNCTION_SQL = """
SELECT p.oid, p.prosecdef
FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
WHERE n.nspname ~ '^aap_[a-z_]+_v1$'
  AND has_function_privilege(current_user,p.oid,'EXECUTE')
ORDER BY p.oid
"""


def _expected_signatures(role: str) -> tuple[str, ...]:
    if role == "worker":
        operation_sets = (
            (IMAGE_OPERATIONS, {"worker"}),
            (INPUT_OPERATIONS, {"worker"}),
        )
    elif role == "control":
        operation_sets = (
            (VIDEO_OPERATIONS, {"control", "system"}),
            (IMAGE_OPERATIONS, {"control", "system"}),
            (INPUT_OPERATIONS, {"control"}),
            (MANIFEST_OPERATIONS, {"control"}),
            (VIDEO_INPUT_OPERATIONS, {"control"}),
        )
    else:
        return ()
    return tuple(
        f"aap_assets_v1.{name}({', '.join(operation.sql_types)})"
        for operations, wanted in operation_sets
        for name, operation in operations.items()
        if operation.role in wanted
    )


async def check_capabilities(pool, role: str = "worker") -> bool:
    """Compare the effective EXECUTE set by OID and reject direct data grants."""
    signatures = _expected_signatures(role)
    if not signatures:
        return False
    try:
        async with pool.acquire(timeout=2) as connection:
            async with connection.transaction(readonly=True):
                await connection.execute("SET LOCAL statement_timeout='5s'")
                if not await connection.fetchval(_ROLE_SQL):
                    return False
                expected: set[int] = set()
                for signature in signatures:
                    oid = await connection.fetchval(
                        "SELECT to_regprocedure($1)::oid", signature
                    )
                    if type(oid) is not int:
                        return False
                    expected.add(oid)
                if len(expected) != len(signatures):
                    return False
                rows = await connection.fetch(_FUNCTION_SQL)
                actual = {row["oid"] for row in rows}
                return actual == expected and all(row["prosecdef"] for row in rows)
    except Exception:
        return False
